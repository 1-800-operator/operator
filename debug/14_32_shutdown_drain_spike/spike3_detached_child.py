"""
Spike 3: Will a detached child subprocess survive after the parent exits
and finish its work on macOS?

Load-bearing for Option B (audio_worker subprocess must outlive main
process to drain the residual whisper backlog).

Method: parent spawns a child via subprocess.Popen with start_new_session=True
(equivalent to os.setsid in the child). Parent exits immediately. Child
writes a sentinel file with progress every 200ms for 5s, then writes "DONE"
and exits. We check from the outside whether the child completed.

Scenarios:
  (a) parent normal exit
  (b) parent SIGTERM
  (c) parent SIGKILL

In all three, child should complete and write DONE.

Also tests: orphan adoption. After parent exits, child's new parent becomes
launchd (PID 1). It should NOT be reaped by the OS.
"""
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import time


CHILD_SOURCE = textwrap.dedent(
    """
    import os, sys, time
    out_path = sys.argv[1]
    pid = os.getpid()
    pgid = os.getpgid(0)
    with open(out_path, "w") as f:
        f.write(f"pid={pid} pgid={pgid} ppid={os.getppid()}\\n")
        for i in range(25):  # 25 * 0.2s = 5s total
            time.sleep(0.2)
            f.write(f"tick {i} ppid={os.getppid()}\\n")
            f.flush()
        f.write("DONE\\n")
    """
)


def run_scenario(name: str, parent_kill_signal: int | None) -> bool:
    workdir = tempfile.mkdtemp()
    sentinel = os.path.join(workdir, "child_output.txt")
    child_script = os.path.join(workdir, "child.py")
    with open(child_script, "w") as f:
        f.write(CHILD_SOURCE)

    # The "parent" itself is a subprocess so we can SIGTERM/SIGKILL it
    # cleanly. The parent spawns the child via subprocess.Popen with
    # start_new_session=True (setsid), then exits.
    parent_source = textwrap.dedent(
        f"""
        import os, subprocess, time
        child = subprocess.Popen(
            [{sys.executable!r}, {child_script!r}, {sentinel!r}],
            start_new_session=True,
        )
        # Hang briefly so the harness can kill us if requested. If we are
        # NOT killed, exit normally.
        time.sleep(0.5)
        """
    )
    parent_proc = subprocess.Popen([sys.executable, "-c", parent_source])
    if parent_kill_signal is not None:
        time.sleep(0.1)  # give parent time to spawn child
        try:
            os.kill(parent_proc.pid, parent_kill_signal)
        except ProcessLookupError:
            pass
    parent_proc.wait()
    parent_exit_t = time.time()

    # Wait up to 8s for the child to write DONE.
    deadline = parent_exit_t + 8.0
    while time.time() < deadline:
        if os.path.exists(sentinel):
            with open(sentinel) as f:
                content = f.read()
            if "DONE" in content:
                ticks = content.count("tick")
                print(f"  {name}: PASS — child completed {ticks} ticks then DONE")
                shutil.rmtree(workdir)
                return True
        time.sleep(0.1)

    content = "(no file)"
    if os.path.exists(sentinel):
        with open(sentinel) as f:
            content = f.read()
    print(f"  {name}: FAIL — child did not complete; output: {content!r}")
    shutil.rmtree(workdir)
    return False


def main():
    print("Spike 3: detached child survival across parent-exit scenarios\n")
    results = [
        run_scenario("(a) parent normal exit", None),
        run_scenario("(b) parent SIGTERM   ", signal.SIGTERM),
        run_scenario("(c) parent SIGKILL   ", signal.SIGKILL),
    ]
    print(f"\nResult: {sum(results)}/3 scenarios passed")
    if all(results):
        print("PASS — detached child survives parent exit on macOS")
    else:
        print("FAIL — Option B is at risk; need different process-detachment approach")


if __name__ == "__main__":
    main()
