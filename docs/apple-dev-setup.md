# Apple Developer setup for system-audio capture (Phase 14.20.5+)

System audio (SCStream / ScreenCaptureKit) is the only path that doesn't work
reliably without proper Apple signing. After Phase 14.20.4 shipped slip mode
with mic-only audio, we deferred system-audio to this checklist.

The whole reason this is needed: macOS 14+ silently denies SCStream audio
callbacks for ad-hoc-signed CLI binaries even when `CGPreflightScreenCaptureAccess()`
returns OK. The only path that survives reboots, rebuilds, app updates, and
`tccutil reset` is a properly Developer-ID-signed + notarized + hardened-runtime
binary distributed inside an `.app` bundle. Granola, Otter, Krisp, all work
this way.

This is what we sign up for when we want system audio.

---

## 1. Apple Developer Program enrollment

**Cost**: $99/year. **Time**: same-day to 48 hours.

1. Go to <https://developer.apple.com/programs/enroll/>
2. Sign in with your Apple ID (`shapirojojo@gmail.com` per memory).
   - Recommend a **dedicated Apple ID** for the org/business rather than mixing with your personal one — easier to transfer or share later.
3. Choose enrollment type:
   - **Individual** (fastest, $99/yr) — your name appears on signed binaries. Fine for solo / pre-company.
   - **Organization** ($99/yr + D-U-N-S number) — company name on binaries. Required if you incorporate.
   - For 1-800-Operator pre-launch, **Individual** is the right pick now; convert to Organization post-incorporation.
4. Provide payment + identity verification. Apple may require:
   - Government ID upload
   - Phone verification
5. Wait for approval email (typically ~24h, sometimes same-day, occasionally up to 7 days).

## 2. Create signing identities

Once enrolled, sign in to <https://developer.apple.com/account/resources/certificates/>.

Create **two** certificates:

- **Developer ID Application** — used to sign apps distributed outside the Mac App Store (our case).
- **Developer ID Installer** — only needed if we ship a `.pkg` installer. Skip for now.

For each:
1. Click `+` to create.
2. Choose "Developer ID Application".
3. On your Mac, run Keychain Access → Certificate Assistant → "Request a Certificate from a Certificate Authority". Save the `.certSigningRequest` file.
4. Upload the CSR.
5. Download the resulting `.cer`, double-click to install into Keychain.

Verify by running:
```bash
security find-identity -v -p codesigning
```
You should see `Developer ID Application: Your Name (TEAMID)`.

## 3. Reserve the bundle identifier

In <https://developer.apple.com/account/resources/identifiers/list>:

1. Click `+`, choose "App IDs", click Continue.
2. Select "App", click Continue.
3. Description: `1-800-Operator audio capture helper`
4. Bundle ID: **explicit** = `com.1-800-operator.audio-capture`
   - Don't reuse `com.operator.audio-capture` from voice-preserved — too generic, may be claimed.
5. Capabilities: leave defaults. We don't need iCloud, Push, etc. for the helper.

## 4. Bundle the helper as a real `.app`

Switch the helper from a raw Mach-O binary to a minimal `.app` bundle. This makes
`tccutil reset com.1-800-operator.audio-capture` work, lets the binary appear in
System Settings with a real name + icon, and lets us declare an
`NSScreenCaptureUsageDescription` (the message macOS shows in the dialog).

Bundle structure:
```
operator-audio-capture.app/
└── Contents/
    ├── Info.plist
    └── MacOS/
        └── operator-audio-capture     # the swiftc-compiled binary
```

`Contents/Info.plist`:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleExecutable</key><string>operator-audio-capture</string>
    <key>CFBundleIdentifier</key><string>com.1-800-operator.audio-capture</string>
    <key>CFBundleName</key><string>Operator Audio Capture</string>
    <key>CFBundlePackageType</key><string>APPL</string>
    <key>CFBundleShortVersionString</key><string>1.0</string>
    <key>CFBundleVersion</key><string>1</string>
    <key>LSMinimumSystemVersion</key><string>13.0</string>
    <key>LSUIElement</key><true/>
    <key>NSScreenCaptureUsageDescription</key>
    <string>Operator transcribes meeting audio so it can join chat conversations.</string>
    <key>NSMicrophoneUsageDescription</key>
    <string>Operator transcribes your voice locally for slip-mode chat.</string>
</dict>
</plist>
```

Key bits:
- `LSUIElement = true` — no Dock icon, runs as background helper (matches our subprocess model).
- `LSMinimumSystemVersion = 13.0` — SCK requires macOS 13+ anyway; keep it low.
- `NSScreenCaptureUsageDescription` — the text shown in the SR dialog. Without this, macOS may silently deny instead of prompting. **This is load-bearing.**
- `NSMicrophoneUsageDescription` — same for mic.

Update `install.sh` to build the `.app` instead of a raw binary, and update
`AttachAdapter._resolve_audio_helper` + `pipeline/doctor.py` to point at
`operator-audio-capture.app/Contents/MacOS/operator-audio-capture`.

## 5. Sign with Developer-ID + hardened runtime

Replace the current ad-hoc codesign step in `install.sh` with:

```bash
codesign --force --deep --options runtime \
  --sign "Developer ID Application: Your Name (TEAMID)" \
  --identifier com.1-800-operator.audio-capture \
  --entitlements helper.entitlements \
  operator-audio-capture.app
```

Where `helper.entitlements` is a tiny plist:
```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>com.apple.security.device.audio-input</key><true/>
    <!-- screen-recording is implicit via NSScreenCaptureUsageDescription -->
</dict>
</plist>
```

Keep `--options runtime` — hardened runtime is what makes Apple's TCC entries
robust. Without it, signing alone doesn't help.

## 6. Notarize

Without notarization, macOS Gatekeeper flags the binary at first launch and
some TCC paths still misbehave. Notarization is a one-time-per-build automated
upload to Apple.

```bash
# After signing, zip the .app and submit:
ditto -c -k --keepParent operator-audio-capture.app operator-audio-capture.zip

xcrun notarytool submit operator-audio-capture.zip \
  --apple-id shapirojojo@gmail.com \
  --team-id TEAMID \
  --password "@keychain:notarytool-password" \
  --wait

# On success, staple the ticket so it works offline:
xcrun stapler staple operator-audio-capture.app
```

Setup tip: store the notarytool app-specific password in Keychain so the
above command runs without interactive prompts:
```bash
xcrun notarytool store-credentials notarytool-password \
  --apple-id shapirojojo@gmail.com \
  --team-id TEAMID
```

## 7. Distribution shape

Once signed + notarized + stapled, ship the `.app` either:
- **Inside the wheel**, copied to `~/.operator/bin/operator-audio-capture.app` by `install.sh`.
- **Separate download**, fetched by `install.sh` from a CDN URL on first install.

Inside-the-wheel is simpler for v1. Separate download lets us update the helper
independently of the Python package — useful once we're shipping multiple OS versions.

## 8. Re-test the slip path

After all the above, the previously-failing test should pass:

```bash
tccutil reset ScreenCapture com.1-800-operator.audio-capture
cd /Users/jojo/Desktop/operator && source venv/bin/activate
python tests/_helper_smoke_12s.py
```

Expected:
- macOS shows a Screen Recording dialog with the description from Info.plist.
- User clicks Allow.
- Helper logs `[S] callback #1, #2, #3` within ~2s.
- Stats lines show `[S]=N cb / NB` going up.
- `[S]_buf > 0` at end of smoke.

The grant survives:
- Reboots
- Helper rebuilds (because cdhash isn't load-bearing — Developer-ID team is)
- App updates that change the binary version

That's the productized end state.

## 9. Things that go away once this is done

- The `_disclaimed_spawn.py` ctypes wrapper — disclaim becomes optional, not load-bearing. Keep it as belt-and-suspenders, document why.
- The "manually drag binary into System Settings" workaround.
- The `tccutil reset ScreenCapture` advice in error messages.
- The intermittent-callbacks bug.
- The 10s no-callback FATAL — once we know SCK actually works, the watchdog can be a soft-warn forever instead of recovery-trigger.

## 10. Costs + timeline

- **$99/yr** (Apple Developer Program — recurring).
- **0–7 days wait** for Developer Program approval.
- **~3–4h of one engineering day** to do steps 4–8 once approved.
- **No ongoing maintenance** beyond renewing the membership annually and re-notarizing on releases.

This is the entry fee for system audio that works on macOS in 2026. There's no
free alternative that's not also a maintenance disaster (BlackHole-style virtual
audio drivers, kernel extensions, private API hacks).
