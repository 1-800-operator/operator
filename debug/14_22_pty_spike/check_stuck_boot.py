"""
Live end-to-end check for the stuck-boot detection (item 1).

Unit tests cover _wait_for_ready's outcomes and _diagnose_stuck_boot's
classification in isolation. This proves the *chain* against a real
claude: spawn in an untrusted temp dir → real claude renders the
workspace-trust dialog and waits → _wait_for_ready's loop tracks the
PTY going quiet → the ceiling raise carries a diagnosis that correctly
names it.

Uses a short monkeypatched ceiling so it doesn't wait the real 180s.
Spawns a real `--dangerously-skip-permissions` claude; tears it down.

Run from the repo root:
    source venv/bin/activate
    python debug/14_22_pty_spike/check_stuck_boot.py
"""
import sys
import tempfile
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from _1_800_operator.pipeline.providers import claude_cli as cc  # noqa: E402
from _1_800_operator.pipeline.providers.claude_cli import (  # noqa: E402
    ClaudeCLIProvider,
    ClaudeCLIProtocolError,
)


def main():
    import shutil
    if shutil.which("claude") is None:
        print("SKIP: `claude` not on PATH")
        sys.exit(0)

    # A fresh temp dir claude has never seen → it will render the
    # workspace-trust dialog and block. Short ceiling so we don't wait 180s.
    cwd = tempfile.mkdtemp(prefix="operator_stuckboot_")
    cc._READY_FLAG_CEILING_SECONDS = 20.0
    print(f"untrusted cwd : {cwd}")
    print(f"ceiling       : {cc._READY_FLAG_CEILING_SECONDS}s (monkeypatched from 180s)")
    print(f"quiet window  : {cc._PTY_QUIET_BLOCKED_SECONDS}s\n")

    provider = ClaudeCLIProvider(cwd=cwd)
    t0 = time.monotonic()
    # pre_warm catches the _wait_for_ready raise and records it on _spawn_exc
    # — exactly the never-post-unprompted "record, don't surface" path.
    provider.pre_warm()
    elapsed = time.monotonic() - t0
    print(f"pre_warm returned in {elapsed:.1f}s\n")

    exc = provider._spawn_exc
    ok = True
    if exc is None:
        print("FAIL: expected pre_warm to record a _spawn_exc, got None "
              "(did this cwd happen to already be trusted?)")
        ok = False
    elif not isinstance(exc, ClaudeCLIProtocolError):
        print(f"FAIL: _spawn_exc is {type(exc).__name__}, expected ClaudeCLIProtocolError")
        ok = False
    else:
        msg = str(exc)
        print("recorded _spawn_exc:")
        print("  " + msg.split("\nPTY tail:")[0])
        structural = "blocked on an interactive prompt" in msg
        labelled = "workspace-trust" in msg
        print(f"\n  structural signal ('blocked on an interactive prompt'): {structural}")
        print(f"  soft label ('workspace-trust dialog')                  : {labelled}")
        if not structural:
            print("FAIL: the structural blocked-on-prompt signal did not fire")
            ok = False
        if not labelled:
            print("NOTE: soft trust-dialog label didn't fire — not fatal "
                  "(heuristic is best-effort), but unexpected here")

    provider.stop()
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
