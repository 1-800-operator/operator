# Session 224 handoff (2026-05-13) — [S] speaker attribution + lazy chat-panel send

## What landed this session

Three commits on `main`, pushed to both `origin` and `public`:

1. **`528587c` — Exclude local tile from [S] speaker attribution.** The DOM speaking-indicator (BlxGDf class) fires for the local tile whenever the runner's mic is active. Room audio constantly bleeds into the mic (when listening on speakers), so the local tile is frequently "speaking" — and `[S]` utterances (which only contain remote participants) were getting tagged with the runner's name. Fix: `INSTALL_SPEAKING_OBSERVER_JS` now identifies the local tile via the same predicate as `GET_SELF_NAME_JS` and skips installing a MutationObserver on it. `_drain_speaking_queue` defensively filters events whose pid matches the cached local participant id, in case the tile DOM re-renders.

2. **`8a8734d` — Stale truncation-notice assertions in transcript MCP tests.** Three assertions still matched the pre-`8b4260e` wording. Brought them in line with the current "showing the most recent N of …" / "omitted to fit response size" wording.

3. **`<new>` — Lazy chat-panel send + drop stale `_chat_panel_open` cache.** See "Chat-panel architecture overhaul" below.

## Corrected reading of the S223 "Jojo in [S]" observation

S223 closed with the hypothesis that the runner's voice was leaking into `[S]`. Today we verified that's not what's happening — `/tmp/operator_audio_debug/S/` WAVs contain only remote voices. The architecture is fine. The bug was attribution, not audio capture. Fixed in commit 1 above.

## Chat-panel architecture overhaul (commit 3)

Live tests in `UvVMjhH-lXwB` confirmed three properties:

- **Q1: messages flow into the DOM while panel is closed** — `delta: 1`, full message text intact (`text length: 493`, `ends with @claude: true`, no ellipsis). Tested with a 500-char message ending in the trigger phrase.
- **Q2: the `[data-panel-id]` container persists across close→reopen** — `same node? true` and `isConnected: true`. Meet uses a CSS-only toggle.
- **Send button is disabled while panel is closed** — `button: true disabled: true`. Meet hard-gates send on panel visibility, so no console-event hack can post a message with the panel closed.

Practical implications:

- **Receive-side works panel-closed** because the observer stays bound to the same container and Meet keeps inserting messages into it.
- **Send-side requires the panel open** at the moment of send.
- **The seed-loop race** (`project_chat_observer_seed_loop_drops_pre_install_messages`) still mandates that the observer install BEFORE any chat messages arrive, otherwise the seed-mark step silently swallows the first `@claude` mention.

Net design:

1. Keep the **join-time `_ensure_chat_open`** — establishes the chat DOM so the observer can attach before any messages land.
2. **Drop `_ensure_chat_open` from the read path** — once attached, the observer survives panel toggles and keeps firing.
3. **Rewrite `_ensure_chat_open`** to check Meet's own send-button `disabled` state instead of a cached `_chat_panel_open` flag. The button is the authoritative signal: enabled ⇒ panel is open enough to send, disabled ⇒ click the toggle.
4. **Drop `self._chat_panel_open`** entirely. The flag was a stale cache — once `True` it never went back to `False` when the user closed the panel manually, so `_ensure_chat_open` short-circuited and the send path tried to fill an invisible textarea.

This fixes the silent-reply bug observed in the live test today. Five `send_chat failed: Locator.wait_for: Timeout 5000ms exceeded` warnings between 12:55 and 12:58 — claude generated each reply, but operator couldn't deliver because the textarea was in DOM but invisible (panel closed by the user, stale flag, no re-open). After this change: each send checks the button state live, opens if needed, no staleness window.

## Bonus observation worth flagging (not in this PR)

When `send_chat` fails, the claude subprocess has no feedback channel — it sees its own streamed reply land in stdout and assumes it was delivered. In today's test, when asked "why didn't you reply to the earlier messages?", claude said "I did reply to each one" — a confident hallucination. Wiring send_chat failures back into the claude session as a tool-result error would close that loop. Worth considering for a future PR; out of scope here.

## State of the repo

All commits pushed to `origin` and `public`. `uv tool install --reinstall .` needed before live validation.

The `README.md` is still dirty (user-owned billing-protection wording) — not committed by convention.

## Open follow-ups

- **`[M]` bleed problem** — the mic leg picks up remote audio via room playback. Many "user"-tagged captions in `sqr-vyex-wob.jsonl` are actually echoes of Kyle/Michael/Matt. The bleed-suppressor isn't catching them. Independent from the `[S]` attribution work. Mitigations to investigate: tighter VAD threshold on the mic leg, wider far-end-activity window, or just accept that listening on speakers degrades `[M]` quality.
- **Claude reply-delivery feedback loop** — see bonus observation above.
- **Skip the join-time panel open entirely** — possible but requires either a heuristic seed (push the most-recent `@claude` to the queue if it's within ~3s) or moving the observer to a wider attach point (`document.body` subtree). Punted today; 1-second join blip is the cleaner tradeoff.

## How to verify in a live meeting

1. `cd /Users/jojo/Desktop/operator && uv tool install --reinstall .`
2. `OPERATOR_AUDIO_DEBUG=1 operator slip claude <multi-party-meet-url>`
3. Confirm join-time panel open works: see `AttachAdapter: chat panel open` in `/tmp/operator.log`.
4. Manually close the chat panel.
5. From another participant, send `@claude hello`. Claude should reply with the panel still closed (the panel auto-pops open at send time).
6. No `send_chat failed: Locator.wait_for` warnings should appear in `/tmp/operator.log`.
7. Cross-check `[S]` attribution: no caption in `~/.operator/history/<slug>.jsonl` should have `sender` equal to your display name with text clearly from a remote participant.
