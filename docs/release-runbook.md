# Release & Self-Update Runbook

How to safely ship a code change to users. This is the **process**; for the
**design + security model** see:
- `src/_1_800_operator/pipeline/selfupdate.py` (module docstring — full security model)
- `CLAUDE.md` → `pipeline/selfupdate.py` entry
- `debug/14_35_selfupdate_spike/FINDINGS.md` (spike + the bugs the live test caught)

Validated end-to-end on real releases v0.1.39→v0.1.42 (S251, May 2026).

---

## What ships how

| Change touches… | Channel | User action |
|---|---|---|
| **Python wheel** (incl. Meet/Chat-DOM scraping fixes — the common case) | **Auto** via launch-time self-update | none |
| **Swift audio helper / aec3** (Rust) | **Manual** — `install.sh` re-run | re-run installer |
| **Plugin / skills** (`operator-plugin` repo) | `/operator:update` | run the slash command |

A wheel update **never** delivers helper/aec3 or plugin changes. Keep those on
their own channels.

Canonical repo for releases: **`1-800-operator/operator`** (the `public` remote).
`origin` (dufis1) is a dev mirror — `install.sh` and the manifest both point at
`public`, so **releases MUST go to `public`** or clients never see them.

---

## Shipping a wheel change (step by step)

1. **Make the change on `main`. Run tests** (`tests/test_selfupdate.py` at
   minimum; plus anything your change touches).
2. **Bump `__version__`** in `src/_1_800_operator/__init__.py`. *Always* bump it —
   the chat-observer version stamp is tied to it (see Gotcha 3).
3. **Build the wheel + get its sha256:**
   ```bash
   uv build --wheel --out-dir /tmp/rel .
   shasum -a 256 /tmp/rel/1_800_operator-*.whl
   ```
4. **Update `release-manifest.json`** (repo root):
   - `"ref": "vX.Y.Z"`
   - `"components.wheel": "X.Y.Z"`
   - `"wheel": { "url": "https://github.com/1-800-operator/operator/releases/download/vX.Y.Z/1_800_operator-X.Y.Z-py3-none-any.whl", "sha256": "<hex>" }`
     (byte-pinned path — preferred. Omit the `wheel` block to fall back to the
     git-ref install; identical trust, just no byte-pinning.)
   - Leave `components.helper` / `components.aec3` **unchanged** unless that
     source actually changed (Gotcha 4).
5. **Bump `install.sh`** `OPERATOR_INSTALL_REF` to `vX.Y.Z`.
6. **Commit** (code + `__init__.py` + `release-manifest.json` + `install.sh`),
   **tag `vX.Y.Z`**, **push to `public`**:
   ```bash
   git push public main && git push public vX.Y.Z
   ```
7. **Create the GitHub release on `public` WITH the wheel asset attached:**
   ```bash
   gh release create vX.Y.Z --repo 1-800-operator/operator \
     --title "vX.Y.Z — …" --notes "…" /tmp/rel/1_800_operator-X.Y.Z-py3-none-any.whl
   ```
   (The asset must be attached or the sha256-pinned path 404s and falls back to git-ref.)
7.5. **Attach the aec3 universal binary to the release** — REQUIRED until the
   CI build is enabled (see below). The GitHub Actions aec3 build
   (`.github/workflows/build-aec3.yml`) is **dormant** because `macos-13-large`
   billing isn't enabled, so **no release auto-gets the binary**. If you skip
   this, `install.sh` 404s on `aec3-darwin-universal`, falls back to building
   from Rust source, and any user without `cargo` silently installs **without
   the speaker-bleed cleaner** (this is exactly what bit v0.1.39–42; fixed by
   re-attaching in S252). Build + attach locally:
   ```bash
   scripts/build_aec3_universal.sh --upload vX.Y.Z   # builds both arches, smoke-tests, uploads (--clobber)
   ```
   Then confirm three assets on the release: the wheel, `aec3-darwin-universal`,
   `aec3-darwin-universal.sha256`. (Pre-launch item: enable `macos-13-large`
   Actions billing so this becomes automatic — see `docs/roadmap.md`.)
8. **Sync the mirror:** `git push origin main --tags`.
9. **Verify** (next section).

---

## Verification checklist

- **Manifest is live** (mind the CDN lag, Gotcha 1):
  ```bash
  python3 -c "import sys;sys.path.insert(0,'src');from _1_800_operator.pipeline import selfupdate as su;print(su.fetch_manifest())"
  ```
- **Asset sha256 == manifest sha256** (download the asset, `shasum -a 256`, compare).
- **A machine on the prior version swaps on next dial:** `operator dial <meet>` →
  `/tmp/operator.log` shows `SELFUPDATE wheel X→Y`, `sha256 verified` (if
  byte-pinned), `swap ok`, `re-exec`; installed version bumps.
- **For DOM / observer changes, test BOTH page states** (this is the one that
  bit us — see Gotcha 3):
  - **Fresh page:** fully quit dial Chrome, then dial.
    `pkill -f "operator/dial_profile"` (matches only dial Chrome, not your main one).
  - **Reused page:** `operator hangup` but **leave Chrome open**, then re-dial.
    The reused page must still pick up the new observer (version-stamped).
- **Confirm it runs installed code, not the dev tree:** from a neutral dir with
  `PYTHONPATH` unset, check the package resolves under the uv-tool venv, not `src/`.

---

## Rollback

Self-update is **roll-forward-only** (clients never downgrade — anti-downgrade
protection). So you **cannot** undo a release by pointing the manifest back at an
older tag; clients ignore a lower version.

To undo a bad release:
1. Cut a **new higher version** with the change reverted, build + attach its wheel.
2. Bump `release-manifest.json` on `main` to that new version.
3. Clients roll forward into the fix on their next launch.

(The manifest on `main` is the live lever. There is no client-side auto-rollback
yet — see Follow-ups.)

---

## Gotchas (learned the hard way — S251)

1. **CDN propagation lag.** `raw.githubusercontent.com` caches the manifest for
   ~minutes and does *not* honor a no-cache request header reliably. A manifest
   change is not instant — poll `fetch_manifest()` until it flips before
   expecting any client to pick it up.
2. **Wheel filename must be PEP 427.** Self-update downloads the asset under its
   real filename because `uv tool install <file.whl>` derives the package name
   from the filename. Don't rename release wheel assets.
3. **Observer / injected-page-JS changes only apply on a refreshed observer.**
   The re-exec replaces the entire Python process, so the *only* code that
   survives an update is **injected page JS** (chat observers in `chat_dom_js.py`)
   and the separately-versioned helper/aec3. The observers are **version-stamped
   against `__version__`**: a reused dial-Chrome page (reconnect, or a relaunch
   into a still-open meet tab) tears down + replaces an observer only if the
   version differs. **So always bump `__version__` when changing observer JS** —
   otherwise the fix silently never applies on reused pages.
4. **helper/aec3 don't auto-ship.** A wheel bump never delivers Swift-helper or
   aec3 changes. Only bump `components.helper`/`components.aec3` in the manifest
   when that source actually changed, and keep them **≤ any installed wheel
   version** — legacy installs without `~/.operator/.components.json` fall back to
   `helper=aec3=wheel`, so a higher value falsely nags everyone to re-run
   `install.sh`. A helper/aec3 bump only logs a notice; the reinstall stays manual.
5. **Test "installed, not dev."** When validating a release on this machine, run
   from a neutral dir with `PYTHONPATH` unset and confirm `operator` loads from
   the uv-tool venv, not the working tree (`PYTHONPATH=src` or an editable install
   would silently run dev code and invalidate the test).
6. **`uv build` packages the WORKING TREE, not HEAD — unrelated uncommitted work
   leaks into the wheel.** This machine often has in-progress work from a parallel
   session sitting uncommitted in `src/` (e.g. a TCC-warmup change in `__main__.py`
   during the v0.1.43 release). `uv build .` snapshots the tree, so that work ships
   silently and the tag no longer reproduces the wheel. **When other uncommitted
   changes are present, build from a clean HEAD checkout instead of the tree:**
   ```bash
   git status --porcelain        # if anything beyond your release is dirty…
   mkdir -p /tmp/relbuild && git archive HEAD | tar -x -C /tmp/relbuild
   (cd /tmp/relbuild && uv build --wheel --out-dir /tmp/rel)   # builds HEAD, not the tree
   ```
   Then the manifest sha256 matches what the tag actually contains. (Verify either
   way: a clean build's sha should equal a from-tree build's sha — if they differ,
   the tree has uncommitted changes you're about to ship.)

---

## Safety properties — do not weaken

(Full rationale in `selfupdate.py`.) Auto-update must stay **exactly as
trustworthy as a fresh `install.sh` run, never weaker**: HTTPS + canonical-host
pinned manifest, strict-redirect (no off-allowlist/scheme-downgrade), roll-forward
only, `ref` shape-validated (vX.Y.Z tag or 40-hex SHA, no shell), optional sha256
byte-pinning, single-flight flock, **fail-safe to installed code on any error**,
**never mid-meeting**, opt-out via `OPERATOR_NO_SELFUPDATE=1`.

---

## Known follow-ups (deferred)

- **Client-side health-check rollback** (N failed joins → last-known-good wheel).
  Today's lever is the server-side manifest roll-forward above.
- **Out-of-band code signing** (offline key) — the only defense against a full
  GitHub-repo compromise; a whole-product hardening, not self-update-specific
  (the initial `install.sh` doesn't have it either).
