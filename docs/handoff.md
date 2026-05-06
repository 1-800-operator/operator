# Session 192 handoff (2026-05-05) — slip CDP architecture pivot, end-to-end working as decide-before-joining v1

Long session, six discrete decision points, ~20 commits on `main` (none pushed to `origin` yet — see "Don't forget" below). Slip works end-to-end as a separate-profile dedicated Chrome window. The original "claude attaches to your existing Chrome tab" vision was killed by a Chromium security restriction that turned out to be unbypassable; the pivoted shape is live and tested but does NOT support mid-meeting handoff (user must run slip BEFORE joining).

## What landed

Phases 14.19.1, 14.19.2, 14.19.3, 14.19.3-quiet (new). Sub-commits: `bridges/claude.py` constants, slip/deploy command wiring + `--yolo`, AttachAdapter (CDP-attach + Chrome lifecycle + chat methods + meeting-entry wait + first-run sign-in support via fresh profile), `_run_slip` end-to-end pipeline (LLM + MeetingRecord + ChatRunner), and the quiet-mode rewrite that closes the self-reply cascade. Key commits:

- `2296341` 14.19.1 — `bridges/claude.py`
- `0230053` 14.19.2 — slip/deploy + `--yolo`
- `17ef03d` … `8b7f9d1` — AttachAdapter iterations through the architecture pivot
- `9a7ed64` — `_run_slip` end-to-end (chat-only)
- `7813e11` — meeting-entry wait
- `8361b16` — CDP probe + room-code-strict tab match
- `8b7f9d1` — **the pivot to separate-profile model** (most important commit of the session)
- `7c1f73a` / `0666e91` — defensive eviction + UX cleanup (no shell commands in error messages)
- `3321a1c` — quiet mode + reply-prefix strip (kills self-reply cascade)

## The strategic call locked this session

**slip = "decide before joining"; mid-meeting handoff is impossible with CDP-attach.**

Why: Chrome 121+ silently disables `--remote-debugging-port` against the user's logged-in default profile (Chromium issue 40066423, security mitigation against OAuth-token harvesting). The flag is accepted into argv but the TCP listener never binds. Verified via diagnostic spike on Chrome 147.0.7727.138. Unbypassable by any flag combination, launch method, or user-data-dir trick. Even the StackOverflow "Chrome Debugger.app wrapper" pattern fails because it targets the same default profile.

Pivot: slip launches a separate Chrome window with its own profile dir at `~/.operator/slip_profile/`. Sidesteps the restriction (Chromium only blocks the default profile). The separate-profile Chrome reliably exposes 9222. Tested live. Works.

Trade-off the user explicitly accepted: if they're already in a meeting in their main Chrome and decide they want claude, **there is no way to add claude to that existing tab**. They must close their main-Chrome tab and rejoin via slip. The user's verdict: "no extension. this is fine for v1."

## Slip's user-facing semantics now

- `operator slip claude <meet-url>` opens a dedicated Chrome window (operator-owned profile)
- First run: user signs into Google in the slip window once, profile persists for future runs
- claude joins as the user's identity (room sees one participant entry "Jojo Shapiro")
- claude responds with `🤖 ` prefix to distinguish bot speech from user typing
- **Quiet mode**: no intro, no "Hold for Claude…" filler, ALWAYS requires `@claude` trigger (no 1-on-1 bypass)
- On Ctrl+C: claude detaches; slip Chrome stays running so the meeting continues
- Re-running slip while slip Chrome is still alive: instant attach (probe → reuse, no second window)
- Other Chrome on port 9222 (e.g. validation spike, dev session): silently SIGTERM'd; slip launches its own

## Functional gaps in slip v1 (deliberately deferred)

1. **No caption capture.** AttachAdapter has no caption path at all. If a user runs slip and asks `@claude what did the other person just say` — claude has zero transcript context. Dial/deploy still capture captions via the existing DOM observer (their Chrome is headless, captions invisible). Slip's Chrome is visible to the user, so DOM captions would show on screen. Either need to (a) accept visible captions in slip Chrome with a "minimize the window" UX hint, or (b) revive the cancelled Swift+ScreenCaptureKit+Whisper plan. Pick after some user testing.
2. **First-run sign-in friction.** Fresh slip profile dumps user on Meet's sign-in page. They figure it out. Friendly preflight ("first time slipping — sign in, then come back") is small polish.
3. **Mid-meeting handoff.** Architectural ceiling, not fixable without an extension. `operator handoff <url>` command would smooth the awkward switch-windows moment but not eliminate it.
4. **Default URL handler.** Registering operator as the macOS default app for meet.google.com URLs would route every Meet link through slip. ~2-3h work; meaningful UX gain.
5. **First-reply latency in slip.** User noted "noticeable beat" before claude's first reply, slower than dial. Not investigated. Possibly the `@claude` poll cadence + claude_cli cold-start adding up. Defer until it's clearly a real complaint.

## Dropped from S192's plan

- **Phase 14.19.3c.2 through 14.19.3c.6** (Swift audio capture + Whisper STT pipeline + Screen Recording / Microphone permissions + dual-stream merger). The user explicitly approved this scope earlier in the session before the pivot. Once we pivoted to the separate-profile model, the audio plan became moot — slip Chrome can run captions invisibly if minimized, and the Granola-class clean-transcript goal can be achieved via DOM captions rather than building an STT pipeline from scratch. Cancellation is documented in the roadmap Post-MVP block. Spike artifacts at `debug/resume_spike/cdp_expose_function_*.py` are kept (untracked, historical record only).

## Open in the working tree

- 20 commits ahead of `origin/main`. Nothing pushed.
- Tag `phase-14.18-frozen` from session start (pre-pivot reference) is local-only.
- Should add tag `slip-cdp-v1-foundation` on HEAD (mark current state before any extension exploration).
- `debug/resume_spike/` untracked, historical artifacts — leave alone.
- `docs/pre-launch-audit.md` still untracked from S187. Defer until 14.19.7 mass-deletion lands (Pass 4 dead-code becomes easier when half the codebase is already gone).

## Exact next step (session 193)

User wants to validate slip in a **real meeting end-to-end** before pushing further. The latest commit (`3321a1c`) changed slip's behavior meaningfully (quiet mode, prefix strip) and only had one live test BEFORE that commit. Next session should:

1. **Live-test slip with a real meeting + another participant.** Confirm: no intro fires, ambient chat from other participant doesn't trigger claude, `@claude what's the time` does work, no self-reply cascade, claude's `🤖 ` reply renders correctly in the room.
2. If green: push to `origin/main` and tag.
3. Then proceed to Phase 14.19.4 (`operator login claude`) or directly into Phase 14.19.7 (mass deletion of wizard-era code).

## Don't forget

- Nothing pushed to `origin/main` or `public/main` this session. The bridge cutover is dev-only until live-tested.
- The "captions ruin the magic" framing from earlier in the session was specifically about the visible-Chrome problem. Slip Chrome being a SEPARATE window (which the user can minimize) materially changes that calculus — DOM captions in slip Chrome are no longer magic-killing because the user need not look at slip Chrome at all. This unlocks a much simpler caption story than the Swift+Whisper plan we'd scoped. Worth re-evaluating in S193.
- Two earlier feedback memories saved this session: `feedback_substantiate_design_deviations.md` (don't propose deviations from existing design without grounded reasoning — don't repeat the Bridge-dataclass mistake) and the audit lesson (read `connectors/session.py` first when writing connector-adjacent code; existing infrastructure usually already covers the case).
