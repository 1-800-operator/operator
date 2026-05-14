"""
Live check for item 3 — ready.flag payload + boot-timing forensics.

Unit tests cover _record_ready's parsing/capture in isolation. This
proves the chain on a real boot: spawn the provider in a TRUSTED dir
(so claude actually boots), let the installed plugin's SessionStart
hook write ready.flag, and confirm _wait_for_ready → _record_ready runs
without crashing and logs the `TIMING ClaudeCLI boot_to_ready=` line on
every run.

Note: with the CURRENTLY installed plugin (pre-item-3, empty ready.flag)
this exercises the graceful-degradation path — boot_to_ready logs with
source=? / session=?. Once operator-plugin ships the item-3
session_start.sh, the same run will show source/session populated and
_transcript_path captured early. Both are correct outcomes.

Run from the repo root:
    source venv/bin/activate
    python debug/14_22_pty_spike/check_ready_payload.py
"""
import io
import logging
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO / "src"))

from _1_800_operator.pipeline.providers.claude_cli import ClaudeCLIProvider  # noqa: E402


def main():
    import shutil
    if shutil.which("claude") is None:
        print("SKIP: `claude` not on PATH")
        sys.exit(0)

    # Capture the provider's log output so we can assert the TIMING line.
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    handler.setLevel(logging.INFO)
    logging.getLogger("_1_800_operator.pipeline.providers.claude_cli").addHandler(handler)
    logging.getLogger("_1_800_operator.pipeline.providers.claude_cli").setLevel(logging.INFO)

    # The operator repo itself is a trusted dir → claude boots cleanly,
    # no workspace-trust dialog.
    provider = ClaudeCLIProvider(cwd=str(_REPO))
    print(f"cwd          : {_REPO}")
    print(f"session_dir  : {provider._session_dir}\n")

    provider.pre_warm()

    ok = True
    if provider._spawn_exc is not None:
        print(f"FAIL: pre_warm recorded a spawn error: {provider._spawn_exc}")
        ok = False
    else:
        print("pre_warm: booted cleanly (no _spawn_exc)")

    flag = provider._ready_flag_path
    if flag.exists():
        content = flag.read_text(encoding="utf-8")
        print(f"ready.flag   : {content[:200]!r}"
              f"{' (empty — pre-item-3 plugin)' if not content.strip() else ''}")
    else:
        print("FAIL: ready.flag never appeared")
        ok = False

    logs = buf.getvalue()
    timing = [ln for ln in logs.splitlines() if "boot_to_ready=" in ln]
    if timing:
        print(f"TIMING line  : {timing[0].strip()}")
    else:
        print("FAIL: no 'boot_to_ready=' TIMING line was logged")
        ok = False

    print(f"transcript captured early: {provider._transcript_path is not None} "
          f"({provider._transcript_path})")
    print(f"session captured early   : {provider._captured_session_id}")

    provider.stop()
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
