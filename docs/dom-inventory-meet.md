# Google Meet — Browser DOM Inventory

This is the canonical list of every browser-side action operator performs on Google Meet, with the page state each action assumes and the selectors used. It exists to (a) document what the Meet adapter is doing and (b) serve as the parity checklist when adding Zoom and Teams adapters.

When adding a new platform, copy each row below into a parallel inventory file (`dom-inventory-zoom.md`, `dom-inventory-teams.md`) and fill in the platform's selectors. Mark rows `N/A` when a Meet-specific concept doesn't exist on the new platform; add new rows for platform-specific concepts (e.g. Zoom's "Open in app or browser?" interstitial).

## Lifecycle phases

The adapter walks through nine phases:

1. **Auth / sign-in** — handled in the wizard (`pipeline/google_signin.py`); persists `auth_state.json`.
2. **Pre-join (green room)** — bot lands on `meet.google.com/<slug>`, classifies the page state, dismisses any device-permission modal, turns camera off, races the three join buttons.
3. **Lobby / admission** — only when `Ask to join` was clicked. Block on the waiting-room image's lifecycle.
4. **In-meeting setup** — confirm in-meeting UI, optionally enable captions, install MutationObserver.
5. **In-meeting steady state — chat** — open chat panel, install chat MutationObserver, send/read messages, scrape participant count + names.
6. **In-meeting steady state — admission queue** — poll for the "Admit N guests" pill and approve.
7. **In-meeting steady state — health** — network-loss alert poll (5s, 30s grace), page-health poll (30s).
8. **Leave / cleanup** — click `Leave call`, fall back to navigating away, drain queues, close context.
9. **Cross-cutting fallbacks** — keyboard shortcut paths used when click-based actions fail (Shift+C for captions, ArrowDown+Enter for admission tray, Escape for modals).

## State-machine notes

- **Page-state classifier** (pre-join): returns one of `pre_join` / `logged_out` / `cant_join` / `unknown`. Recovery ladder: cookie injection from `auth_state.json` → reload → re-classify. Already-in-meeting detected by presence of `Leave call` button.
- **Lobby**: two-phase wait. Phase 1 (10s) confirms the waiting-room image is visible. Phase 2 (`config.LOBBY_WAIT_SECONDS`, default 600s) watches for it to detach. If Phase 1 times out we optimistically return `admitted` (the host may have admitted us before we arrived).
- **macOS vs Linux divergence**: macOS uses real Chrome (`/Applications/Google Chrome.app`) with full chat MutationObserver, captions, and admission cooldown dedup. Linux uses Playwright Chromium, polls chat instead of observing, and lacks captions. Same Meet selectors on both.

---

## Auth / sign-in

Runs in the wizard (`pipeline/google_signin.py`). One-time setup, separate from the per-meeting flow.

| Action | Page state | Selector(s) | Extract / assert | Failure handling |
|---|---|---|---|---|
| Detect existing session | Disk: `~/.operator/auth_state.json` | JSON parse → filter `cookies[]` for `name == "SID"` && `domain == ".google.com"` | Boolean: SID cookie present | Return `(detected=False, email=None)` if file absent |
| Read email cache | Disk: `~/.operator/google_account.json` | JSON `data.email`; regex `[\w.+-]+@[\w-]+\.[\w.-]+` | Email string | Return None (non-fatal) |
| Open sign-in flow | Real Chrome on `accounts.google.com` | Persistent context + real Chrome binary | Poll `_has_google_sid(context)` every 1s for 300s | Timeout → raise; caller keeps old session |
| Navigate to myaccount | After SID confirmed | `goto("https://myaccount.google.com/")` | Wait `domcontentloaded` (15s) | Skip email capture; continue with None |
| Extract email | On myaccount.google.com | `a[aria-label*="@"]`; `div[aria-label*="@"]`; `[data-email]`; fallback to body text + regex | First regex match in `aria-label` / `data-email` / `title` / text | Return None |
| Persist session | After SID live | `context.storage_state(path=...)` | Cookies + localStorage written to `auth_state.json` | Log warning; continue |
| Persist email | After email captured | JSON write `{"email": "..."}` | File present | Skip if email is None |

---

## Pre-join (green room)

Assumes URL is `meet.google.com/<slug>`. UI shows camera + microphone toggles, join buttons.

| Action | Page state | Selector(s) | Extract / assert | Failure handling |
|---|---|---|---|---|
| Classify page state | Post-navigation | URL substring check `accounts.google.com`; text locator `"text=Sign in"`; cookie SID check | One of: `pre_join` / `logged_out` / `cant_join` / `unknown` | Default to `unknown`; proceed conservatively |
| Inject recovered cookies | After `cant_join` + valid `auth_state.json` | `context.add_cookies(filtered_cookies)` | No exception | Signal `session_expired` if injection fails |
| Reload after recovery | Post cookie injection | `page.reload(wait_until="domcontentloaded", timeout=30s)` | Re-classify | Persistent `logged_out` → signal `session_expired`; persistent `cant_join` → signal `cant_join` |
| Detect already-in-meeting | meet.new auto-join path | `page.get_by_role("button", name="Leave call")` | Visible + count > 0 | Skip pre-join actions; set `already_in_meeting = True` |
| Dismiss device-permission modal | Modal over join buttons | `page.get_by_text("Continue without microphone and camera")`; fallback `Escape` | Modal gone | Escape always safe (no-op if no modal) |
| Turn off camera | Pre-join screen, modal cleared | `page.get_by_role("button", name="Turn off camera")` | Wait 5s | Log warning; proceed |
| Confirm camera off | After toggle | `[role="button"][data-is-muted="true"][aria-label*="camera"]` | `data-is-muted="true"` | Save debug dump; continue |
| Fill guest name (Linux only) | Unauthenticated path | `page.get_by_placeholder("Your name")` | Filled with `"Operator"` | Skip on signed-in; pass on exception |
| Race join buttons | Pre-join, ready | `page.get_by_role("button", name="Join now").or_(ask_join).or_(switch_here).wait_for(timeout=10s)` | Any of three resolves | Signal `no_join_button` |
| Click `Join now` | Direct entry available | `button[name="Join now"]` | Click; navigate | Try Ask-to-join or Switch-here next |
| Click `Ask to join` | Host approval gate | `button[name="Ask to join"]` | Click; enter waiting room | Proceed to `_wait_for_admission()` |
| Click `Switch here` | Device-takeover available | `button[name="Switch here"]` | Click; takeover joins | Treat as success |

---

## Lobby / admission

Triggered by `Ask to join`. Watches the waiting-room image for visible → detached transition.

| Action | Page state | Selector(s) | Extract / assert | Failure handling |
|---|---|---|---|---|
| Wait for waiting-room image (Phase 1) | Lobby | `img[alt*="Please wait until a meeting host"]` | `wait_for_selector(state="visible", timeout=10_000)` | Timeout → return `"admitted"` (assume already let in) |
| Watch for image detach (Phase 2) | Lobby, image visible | Same selector, `state="detached"`, chunked timeouts | Detached → admitted | Re-check `_leave_event` and deadline per chunk |
| Return admission status | Loop exit | N/A | One of `"admitted"` / `"cancelled"` / `"timeout"` | `timeout` → signal `admission_timeout`; `cancelled` → signal `admission_cancelled` |

---

## In-meeting setup

After the join button click (and any admission wait). Confirms in-meeting UI, enables captions, installs observers.

| Action | Page state | Selector(s) | Extract / assert | Failure handling |
|---|---|---|---|---|
| Wait for in-meeting UI | Post-join | `button[aria-label*="Leave call"]` | `wait_for_selector(timeout=5s)` | Log warning; proceed |
| Check captions on (non-blocking) | In-meeting | `button[aria-label*="Turn off captions"]` OR `[role="region"][aria-label*="Captions"]` | Either visible | Return False; trigger enable |
| Enable captions via Shift+C | Captions off | Keyboard `Shift+C` (10 × 500ms retries) | `captions_are_on()` true after each retry | Fall back to button click |
| Enable captions via button | Shift+C failed | `button[aria-label*="Turn on captions"]` | Wait region visible (5s) | Continue without captions |
| Expose JS bridge | Browser startup, pre-nav | `page.expose_function("__onCaption", callback)` | `window.__onCaption` registered | Continue (transcript will be empty) |
| Inject caption observer | Captions confirmed on | `page.evaluate(CAPTION_OBSERVER_JS)` | Observer attached to `[role="region"][aria-label*="Captions"]`; falls back to `document.body` after 5s polling | Log; meeting continues |

### Caption observer details (`captions_js.py`)

- Region selector (primary): `[role="region"][aria-label*="Captions"]`
- Speaker badge selectors: `.NWpY1d`, `.xoMHSc`
- Dedup: `(speaker, text)` hash, 50ms minimum re-fire
- Python-side filter drops: diagnostic frames, empty text, icon ligatures, system phrases, speaker-name echoes

---

## In-meeting steady state — chat

Polled every 500ms via the chat queue. macOS uses MutationObserver; Linux polls.

| Action | Page state | Selector(s) | Extract / assert | Failure handling |
|---|---|---|---|---|
| Open chat panel | In-meeting | Already open: `textarea[aria-label="Send a message"]`; opener: `button[name="Chat with everyone"]` | Textarea visible | Save screenshot; continue |
| Send message (macOS) | Chat panel open | Textarea fill + Enter; snapshot diff over `div[data-message-id]` (20 × 50ms) | New message ID | Return None on timeout; caller dedupes by text |
| Send message (Linux) | Chat panel open | Textarea fill + Enter (no readback) | None always | Caller dedupes by text |
| Read chat (macOS) | Chat panel open | MutationObserver on `textarea.closest('[data-panel-id]')` | New `div[data-message-id]`: text from `div[jsname]` (fallback first child); sender via 4-parent walk to timestamp-regex sibling | Retry observer install on next call |
| Read chat (Linux) | Chat panel open | Poll `div[data-message-id]` | Same extraction via `evaluate()` | Skip seen IDs |
| Get participant count | In-meeting | `[data-requested-participant-id]` | `.count()` | Return 0 |
| Get participant names | In-meeting | `[data-requested-participant-id]` tiles → child `[data-self-name]` textContent → tile `aria-label` (excluding "More options" / "Menu") → trimmed textContent <60 chars | Deduped list | Return [] |

---

## In-meeting steady state — admission queue

Polled every 2s during the in-meeting hold loop.

| Action | Page state | Selector(s) | Extract / assert | Failure handling |
|---|---|---|---|---|
| Detect admission pill | Polling | `get_by_text(r"^Admit\s+\d+\s+(guest\|people)", re.I)` | Pill visible + inner text | Skip cycle |
| Cooldown dedup (macOS) | Pill detected | Tuple compare to `last_admit_attempt = (pill_text, participant_count)` | Skip if both match | Avoid spam on stale pills |
| Hover pill | Pill visible | `.hover()` | Tray appears | Continue to click path |
| Click `Admit` in tray | Tray visible | `page.get_by_role("button", name=r"^Admit$", re.I)`, click (1s) | Click fires | Fall back to keyboard path |
| Verify admission (macOS) | Post-click | Poll participant count for 3s in 150ms chunks | Count strictly increases | Treat pill as stale; reset baseline |
| Keyboard fallback | Click failed | `pill.focus()` → `keyboard.press("ArrowDown")` → `keyboard.press("Enter")` | Tray opens; Enter fires | Save debug dump |

---

## In-meeting steady state — health

| Loop | Cadence | Selectors / checks | Action |
|---|---|---|---|
| Network-loss | 5s | `[role="alert"]` text contains "lost your network" (case-insensitive) | Stamp `network_lost_at`; if elapsed ≥ 30s, exit |
| Page-health | 30s | `page.is_closed()`, `meet.google.com` substring in `page.url` | Closed → break loop; URL drift → log warning, continue |

---

## Leave / cleanup

`leave()` runs in a `finally` block. Idempotent.

| Action | Page state | Selector(s) | Extract / assert | Failure handling |
|---|---|---|---|---|
| Signal leave | Any | `self._leave_event.set()` | Event set | Idempotent |
| Click `Leave call` | In-meeting | `button[name="Leave call"]` | `wait_for(timeout=2000)` then click | Pass on exception |
| Navigate away | Leave button missing | `page.goto("about:blank", timeout=3s)` | Page unloads | Pass on exception |
| Drain chat queue | Browser closing | `self._chat_queue` peek loop | Per command: empty list / 0 / [] / None | Caller unblocks immediately |
| Close context | Post-leave | macOS: rely on `sync_playwright.__exit__`; Linux: `browser.close()` | Teardown | macOS may force-kill Playwright drivers if exit wedges |
| Wait browser thread | Main thread | `browser_thread.join(timeout=10s)` | Thread exits | Log warning |
| Clean PID file | Browser thread exit | `~/.operator/browser_profile/.operator.pid` removed | File gone | Pass on OSError |

---

## Cross-cutting fallbacks (keyboard / dismissal)

| Need | Primary | Fallback |
|---|---|---|
| Enable captions | Shift+C (10 × 500ms) | Click `button[aria-label*="Turn on captions"]` |
| Admit guest | Hover pill + click `Admit` button | Focus pill → ArrowDown → Enter |
| Dismiss pre-join modal | Click "Continue without microphone and camera" | Press Escape |

---

## Action count

- Auth / sign-in: 7
- Pre-join: 12
- Lobby: 3
- In-meeting setup: 6
- Chat: 7
- Admission queue: 6
- Health: 2 loops
- Leave: 7
- Cross-cutting fallbacks: 3 patterns

**Total: ~56 distinct browser actions.**

---

## Cross-platform extensions

When porting to Zoom or Teams, the following rows do **not** exist on Meet but will need their own probes and adapter logic:

- **App-handoff interstitial.** "Open Zoom?" / "Open Microsoft Teams?" dialog with "Cancel" / "Continue in browser" options. Detect and force browser path.
- **Mic-mute pre-join state.** Meet defaults to muted in green-room; Zoom/Teams may default to mic-on for guests. Add an explicit "ensure mic muted before joining" probe.
- **Bot-detection / sign-in gate.** "Sign in to join" or CAPTCHA interstitials when guest path is rejected. Detect and trigger account-auth fallback.
- **Kicked / host-ended modal.** Explicit modal on both Zoom and Teams; today operator detects only via network-loss / page-health proxies. Add as a real state.

These get filled in as new rows in the per-platform inventory file, not retro-fitted to this Meet doc.
