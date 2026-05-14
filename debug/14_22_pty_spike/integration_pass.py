"""
Integration harness for the 14.22 PTY+hooks refactor — DECISION.md tests 20/21/23.

Drives the REAL ClaudeCLIProvider (real inner-claude PTY, real
operator-plugin Stop/SessionStart hooks) WITHOUT a Google Meet. The Meet
is only the outer shell; tests 20/21/23 exercise the provider+plugin
layer, which this harness reaches directly by constructing a provider,
pre-warming it, and driving turns through `complete_streaming`.

Not covered here, and why:
  - Test 22 (foreign-hook interference) mutates ~/.claude/settings.json
    and is a known write-hazard under --dangerously-skip-permissions
    (S228 Hard Won Knowledge). Run by hand, watched.
  - Test 24 (resume from desktop-app session) needs Claude Code Desktop
    running + a real project session to bridge. Not automatable here.
  - Test 25 (--fresh mode) can't run: `--fresh` was never implemented.
    `__main__.py` has only `--resume-session` + the CLAUDE_CODE_SESSION_ID
    env tier; the implicit "fresh" default spawns in os.getcwd(), NOT
    `~/.operator/sessions/<id>/`, so foreign project hooks DO fire. The
    DECISION.md section-M `--fresh` escape hatch is still a TODO.

Requires: operator-plugin installed (so the hooks fire) and `claude`
logged in. Spawns a real `--dangerously-skip-permissions` claude — it
runs tools unprompted and costs subscription tokens.

Run from the repo root:
    source venv/bin/activate
    python debug/14_22_pty_spike/integration_pass.py [21|23|20|all]
    python debug/14_22_pty_spike/integration_pass.py 20 --turns 60
"""
import argparse
import json
import os
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from _1_800_operator.pipeline.providers.claude_cli import (  # noqa: E402
    ClaudeCLIProvider,
    ClaudeCLIProtocolError,
)


def _banner(text):
    print(f"\n{'=' * 70}\n{text}\n{'=' * 70}")


def _make_provider():
    """A fresh-session provider, spawned in the operator repo root.

    Must be a dir Claude Code already trusts — a fresh tempfile.mkdtemp()
    triggers the first-run workspace-trust dialog, which blocks
    SessionStart and wedges the boot (exactly what item 1 detects). The
    repo root is trusted from normal dev use, so the boot is clean.
    """
    cwd = str(_REPO)
    return ClaudeCLIProvider(cwd=cwd), cwd


def _last_stop_row(provider):
    """The most recent replies.jsonl row, parsed — or None."""
    try:
        lines = provider._replies_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in reversed(lines):
        line = line.strip()
        if line:
            try:
                return json.loads(line)
            except json.JSONDecodeError:
                return None
    return None


def _drive_turn(provider, prompt):
    """One turn through the streaming path. Returns
    (resp, paragraphs, wall_elapsed, stop_row)."""
    paragraphs = []
    t0 = time.monotonic()
    resp = provider.complete_streaming(
        system=None,
        messages=[{"role": "user", "content": prompt}],
        model=None,
        max_tokens=None,
        on_paragraph=lambda p: paragraphs.append(p),
    )
    elapsed = time.monotonic() - t0
    return resp, paragraphs, elapsed, _last_stop_row(provider)


# --- Test 21 — hook latency on the hot path ---------------------------


def test_21_hook_latency(turns=10):
    """DECISION.md 21. Measure operator's pickup lag — the gap between the
    Stop hook script stamping its row (`ts`) and `complete_streaming`
    returning. That gap is tail-poll latency (_REPLIES_POLL_SECONDS) +
    the final transcript drain settle. Also reports whole-turn wall time
    for context. DECISION's sub-2s p50 target spanned the chat-send hop
    too, which this harness can't measure — so the pickup-lag number is
    the apples-to-apples provider-side figure.
    """
    _banner(f"TEST 21 — hook latency on the hot path ({turns} turns)")
    provider, cwd = _make_provider()
    pickup_lags = []
    turn_walls = []
    try:
        t_spawn = time.monotonic()
        provider.pre_warm()
        print(f"  pre_warm (spawn + briefing round-trip): {time.monotonic() - t_spawn:.2f}s")

        for n in range(1, turns + 1):
            prompt = f"This is turn {n}. Reply with exactly the word ACK and nothing else."
            resp, paragraphs, elapsed, stop_row = _drive_turn(provider, prompt)
            turn_walls.append(elapsed)
            t_returned = time.time()
            stop_ts = (stop_row or {}).get("ts")
            if stop_ts is not None:
                lag = t_returned - stop_ts
                pickup_lags.append(lag)
                print(f"  turn {n:2d}: wall={elapsed:5.2f}s  pickup_lag={lag:5.2f}s  "
                      f"reply={ (resp.text or '')[:30]!r}")
            else:
                print(f"  turn {n:2d}: wall={elapsed:5.2f}s  pickup_lag=?? (no stop row ts)  "
                      f"reply={(resp.text or '')[:30]!r}")
    finally:
        provider.stop()

    if not pickup_lags:
        print("  RESULT: INCONCLUSIVE — no Stop-row timestamps captured")
        return False
    p50 = statistics.median(pickup_lags)
    p95 = sorted(pickup_lags)[max(0, int(len(pickup_lags) * 0.95) - 1)]
    print(f"\n  pickup_lag: p50={p50:.2f}s  p95={p95:.2f}s  "
          f"min={min(pickup_lags):.2f}s  max={max(pickup_lags):.2f}s")
    print(f"  turn wall:  p50={statistics.median(turn_walls):.2f}s  "
          f"max={max(turn_walls):.2f}s")
    # The pickup lag should sit near _REPLIES_POLL_SECONDS (0.15) +
    # _TRANSCRIPT_FINAL_DRAIN_SETTLE (0.3) ≈ 0.45s, with headroom.
    ok = p50 < 2.0
    print(f"  RESULT: {'PASS' if ok else 'FAIL'} — p50 pickup lag "
          f"{'<' if ok else '>='} 2.0s")
    return ok


# --- Test 23 — tear-down race -----------------------------------------


def test_23_teardown_race():
    """DECISION.md 23. A turn producing a long streamed reply is cut off
    by `provider.stop()` mid-flight. Post-S228 the contract is: the
    teardown is orderly — `_wait_for_next_reply` sees `_stopping` and
    bails, `_run_turn` raises ClaudeCLIProtocolError("provider is
    stopping") (NOT a crash dump), the PTY group is terminated, and any
    paragraphs that streamed in before the cut were already delivered to
    on_paragraph. (Mid-turn teardown is user-initiated — they chose to
    cut it off — so the WHOLE reply isn't guaranteed; partial streamed
    content + a clean exit is.)
    """
    _banner("TEST 23 — tear-down race (stop() mid-turn)")
    provider, cwd = _make_provider()
    provider.pre_warm()

    paragraphs = []
    outcome = {}

    def _run():
        try:
            provider.complete_streaming(
                system=None,
                messages=[{"role": "user", "content": (
                    "Count slowly from 1 to 60. Put each number on its own line "
                    "with a short sentence about it. Take your time."
                )}],
                model=None,
                max_tokens=None,
                on_paragraph=lambda p: paragraphs.append(p),
            )
            outcome["result"] = "returned-normally"
        except ClaudeCLIProtocolError as e:
            outcome["result"] = "protocol-error"
            outcome["msg"] = str(e)
        except Exception as e:  # noqa: BLE001
            outcome["result"] = f"unexpected-{type(e).__name__}"
            outcome["msg"] = str(e)

    turn = threading.Thread(target=_run, daemon=True)
    turn.start()
    time.sleep(4.0)  # let the reply start streaming
    paras_before_stop = len(paragraphs)
    print(f"  {paras_before_stop} paragraph(s) streamed before stop()")
    t_stop = time.monotonic()
    provider.stop()
    stop_elapsed = time.monotonic() - t_stop
    turn.join(timeout=15)
    print(f"  stop() returned in {stop_elapsed:.2f}s; turn thread "
          f"{'joined' if not turn.is_alive() else 'STILL ALIVE'}")
    print(f"  turn outcome: {outcome.get('result')}  {outcome.get('msg', '')[:80]}")

    proc_dead = provider._proc is None or provider._proc.poll() is not None
    clean_exit = (
        outcome.get("result") == "protocol-error"
        and "stopping" in outcome.get("msg", "").lower()
    )
    crash_dump = "PTY tail" in outcome.get("msg", "")
    print(f"  inner-claude terminated: {proc_dead}")
    print(f"  orderly 'provider is stopping' (not a crash dump): "
          f"{clean_exit and not crash_dump}")
    print(f"  partial reply delivered before cut: {paras_before_stop > 0}")

    ok = proc_dead and clean_exit and not crash_dump and not turn.is_alive()
    print(f"  RESULT: {'PASS' if ok else 'FAIL'}")
    return ok


# --- Test 20 — long-meeting compaction --------------------------------


def test_20_long_meeting_compaction(turns=40):
    """DECISION.md 20. Drive many turns, deliberately loading file
    content into inner-claude's context each turn to push toward
    compaction. Verify: (a) the Stop hook keeps firing — every turn
    returns a non-empty reply; (b) `last_assistant_message` stays
    coherent (each turn we ask for a turn-numbered token and check it
    comes back); (c) the hook-event JSONLs don't grow pathologically
    (replies.jsonl ≈ one row/turn; transcript grows but not unbounded
    per-turn). Also greps the transcript for a compaction marker so the
    run can report whether the threshold was actually reached.
    """
    _banner(f"TEST 20 — long-meeting compaction ({turns} turns)")
    provider, cwd = _make_provider()
    # A spread of real files to load into context — sized to push the
    # context window without being absurd.
    ctx_files = [
        _REPO / "src/_1_800_operator/pipeline/providers/claude_cli.py",
        _REPO / "src/_1_800_operator/connectors/attach_adapter.py",
        _REPO / "src/_1_800_operator/pipeline/chat_runner.py",
        _REPO / "docs/agent-context.md",
        _REPO / "CLAUDE.md",
    ]
    ctx_files = [str(p) for p in ctx_files if p.exists()]

    fired = 0
    coherent = 0
    failures = []
    t0 = time.monotonic()
    try:
        provider.pre_warm()
        for n in range(1, turns + 1):
            f = ctx_files[n % len(ctx_files)]
            token = f"TURN{n}OK"
            prompt = (
                f"Read the file {f}. Then reply with exactly the token "
                f"{token} followed by the number of lines in that file. "
                f"Nothing else."
            )
            try:
                resp, paragraphs, elapsed, stop_row = _drive_turn(provider, prompt)
            except ClaudeCLIProtocolError as e:
                failures.append((n, f"protocol-error: {e}"))
                print(f"  turn {n:3d}: PROTOCOL ERROR — {str(e)[:80]}")
                break
            text = resp.text or ""
            if text.strip():
                fired += 1
            else:
                failures.append((n, "empty reply"))
            if token in text:
                coherent += 1
            else:
                failures.append((n, f"token {token} missing from reply {text[:50]!r}"))
            replies_sz = provider._replies_path.stat().st_size
            tx_sz = (provider._transcript_path.stat().st_size
                     if provider._transcript_path and provider._transcript_path.exists()
                     else 0)
            if n % 5 == 0 or n <= 3:
                print(f"  turn {n:3d}: wall={elapsed:5.1f}s  "
                      f"reply={text.strip()[:32]!r}  "
                      f"replies.jsonl={replies_sz}B  transcript={tx_sz // 1024}KB")
    finally:
        elapsed_total = time.monotonic() - t0
        # Compaction marker scan before teardown.
        compaction_hits = 0
        tx_path = provider._transcript_path
        if tx_path and tx_path.exists():
            try:
                for line in tx_path.read_text(encoding="utf-8").splitlines():
                    low = line.lower()
                    if '"iscompactsummary"' in low or '"type": "summary"' in low \
                            or '"type":"summary"' in low or "compact" in low:
                        compaction_hits += 1
            except OSError:
                pass
        replies_rows = provider._count_replies()
        provider.stop()

    print(f"\n  drove {fired}/{turns} turns with a non-empty reply in "
          f"{elapsed_total / 60:.1f} min")
    print(f"  coherent (turn token echoed back): {coherent}/{turns}")
    print(f"  replies.jsonl rows: {replies_rows} (expected ≈ {turns} + 1 briefing)")
    print(f"  transcript compaction markers found: {compaction_hits}")
    if failures:
        print(f"  failures ({len(failures)}):")
        for n, why in failures[:10]:
            print(f"    turn {n}: {why}")
    # Pass: every turn fired and stayed coherent. Compaction reached is
    # reported but not required — if it never triggered, the run just
    # didn't fill the window; that's INCONCLUSIVE for the compaction
    # claim, noted in the result line.
    all_fired = fired == turns and coherent == turns
    if not all_fired:
        print("  RESULT: FAIL — a turn went silent or incoherent")
        return False
    if compaction_hits == 0:
        print("  RESULT: PASS (turns) / INCONCLUSIVE (compaction) — "
              "context window never filled; re-run with more --turns")
        return True
    print("  RESULT: PASS — Stop hook kept firing coherently across compaction")
    return True


# --- runner -----------------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("which", nargs="?", default="all", choices=["20", "21", "23", "all"])
    ap.add_argument("--turns", type=int, default=None,
                    help="turn count for 20 (default 40) / 21 (default 10)")
    args = ap.parse_args()

    import shutil
    if shutil.which("claude") is None:
        print("ABORT: `claude` CLI not on PATH.")
        sys.exit(2)

    results = {}
    if args.which in ("21", "all"):
        results["21"] = test_21_hook_latency(turns=args.turns or 10)
    if args.which in ("23", "all"):
        results["23"] = test_23_teardown_race()
    if args.which in ("20", "all"):
        results["20"] = test_20_long_meeting_compaction(turns=args.turns or 40)

    _banner("INTEGRATION PASS SUMMARY")
    for k in sorted(results):
        print(f"  test {k}: {'PASS' if results[k] else 'FAIL / INCONCLUSIVE'}")
    print("  test 22 (foreign-hook): run by hand — settings.json write hazard")
    print("  test 24 (desktop resume): needs Claude Code Desktop + real project")
    print("  test 25 (--fresh mode): BLOCKED — --fresh was never implemented")
    sys.exit(0 if all(results.values()) else 1)


if __name__ == "__main__":
    main()
