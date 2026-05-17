"""Verify the Chrome 121+ Playwright-reattach claim documented at
src/_1_800_operator/connectors/attach_adapter.py:525-531.

Claim under test:
    Chrome 121+ refuses Browser.setDownloadBehavior (which Playwright's
    connect_over_cdp always issues) against any Chrome that previously
    had a Playwright session attached and disconnected.

Method:
    1. Launch a fresh Chrome with --remote-debugging-port=9333 and a
       throwaway --user-data-dir (does NOT touch slip profile).
    2. Open 2 user-style tabs first (about:blank with a marker title)
       — simulates the "user has other tabs" scenario.
    3. First connect_over_cdp: attach, do something trivial, browser.close().
    4. Second connect_over_cdp: attach again to the SAME Chrome process.
       Record success/failure of the connect, of any post-connect
       Browser.setDownloadBehavior the user can issue, and of basic
       page DOM access on a new tab.
    5. Third connect_over_cdp: same as #4 but immediately list contexts
       and try to use an existing tab (not create a new one).
    6. Kill the Chrome we launched. Leave the user's actual slip Chrome
       untouched.

Run:
    cd /Users/jojo/Desktop/operator
    source venv/bin/activate
    python debug/14_30_cdp_reattach_spike/spike.py 2>&1 | tee debug/14_30_cdp_reattach_spike/run.log
"""
from __future__ import annotations

import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9333  # different from operator's 9222 — don't conflict with real slip
CDP_URL = f"http://localhost:{PORT}"


def log(msg: str) -> None:
    print(f"[spike] {msg}", flush=True)


def port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.2)
        try:
            s.connect(("127.0.0.1", port))
            return True
        except OSError:
            return False


def wait_for_cdp(port: int, timeout: float = 15.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if port_open(port):
            return True
        time.sleep(0.1)
    return False


def launch_chrome(profile_dir: Path, urls: list[str]) -> subprocess.Popen:
    args = [
        "open", "-na", "Google Chrome", "--args",
        f"--remote-debugging-port={PORT}",
        "--remote-allow-origins=*",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        *urls,
    ]
    log(f"launching: {' '.join(args)}")
    return subprocess.Popen(args)


def find_chrome_pid_on_port(port: int) -> int | None:
    """Best-effort: find the Chrome PID listening on `port` via lsof."""
    try:
        out = subprocess.check_output(
            ["lsof", "-iTCP", f"-i:{port}", "-sTCP:LISTEN", "-n", "-P"],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return None
    for line in out.splitlines()[1:]:
        parts = line.split()
        if len(parts) >= 2:
            try:
                return int(parts[1])
            except ValueError:
                continue
    return None


def kill_pid(pid: int) -> None:
    import os
    import signal
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(50):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.1)
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def attempt_attach(label: str) -> dict:
    """Try to connect, exercise setDownloadBehavior, return a dict of outcomes."""
    result: dict = {"label": label, "connect_ok": False, "context_count": None,
                    "set_download_ok": None, "new_page_ok": None,
                    "reuse_existing_page_ok": None, "errors": []}
    pw = sync_playwright().start()
    try:
        try:
            browser = pw.chromium.connect_over_cdp(CDP_URL)
            result["connect_ok"] = True
        except Exception as e:
            result["errors"].append(f"connect_over_cdp: {type(e).__name__}: {e}")
            return result

        try:
            contexts = browser.contexts
            result["context_count"] = len(contexts)
        except Exception as e:
            result["errors"].append(f"contexts: {type(e).__name__}: {e}")

        # Try explicit Browser.setDownloadBehavior on the default context.
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            cdp = ctx.new_cdp_session(ctx.pages[0]) if ctx.pages else None
            if cdp is None:
                # Need a page to make a session.
                tmp_page = ctx.new_page()
                cdp = ctx.new_cdp_session(tmp_page)
            cdp.send("Browser.setDownloadBehavior",
                     {"behavior": "deny"})
            result["set_download_ok"] = True
        except Exception as e:
            result["set_download_ok"] = False
            result["errors"].append(f"setDownloadBehavior: {type(e).__name__}: {e}")

        # Open a brand-new page and confirm DOM access works.
        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            p = ctx.new_page()
            p.goto("about:blank")
            p.evaluate("document.title = 'spike-new-tab'")
            assert p.title() == "spike-new-tab"
            result["new_page_ok"] = True
        except Exception as e:
            result["new_page_ok"] = False
            result["errors"].append(f"new_page: {type(e).__name__}: {e}")

        # Try to reach an existing tab (one of the pre-opened ones).
        try:
            ctx = browser.contexts[0]
            existing = [pg for pg in ctx.pages if pg.url.startswith("data:")]
            if not existing:
                # Fall back to any page that isn't our just-created one.
                existing = [pg for pg in ctx.pages
                            if pg.title() != "spike-new-tab"]
            if existing:
                pg = existing[0]
                _ = pg.title()
                result["reuse_existing_page_ok"] = True
            else:
                result["reuse_existing_page_ok"] = "no_existing_pages"
        except Exception as e:
            result["reuse_existing_page_ok"] = False
            result["errors"].append(f"reuse_existing: {type(e).__name__}: {e}")

        # Clean disconnect — this is what triggered the 121+ lockout per the comment.
        try:
            browser.close()
        except Exception as e:
            result["errors"].append(f"browser.close: {type(e).__name__}: {e}")
    finally:
        try:
            pw.stop()
        except Exception as e:
            result["errors"].append(f"pw.stop: {type(e).__name__}: {e}")
    return result


def main() -> int:
    log("Chrome version:")
    subprocess.run([CHROME, "--version"])

    with tempfile.TemporaryDirectory(prefix="spike-cdp-") as tmp:
        profile = Path(tmp) / "profile"
        profile.mkdir()
        marker_a = "data:text/html,<title>USER-TAB-A</title><h1>tab A</h1>"
        marker_b = "data:text/html,<title>USER-TAB-B</title><h1>tab B</h1>"

        launch_chrome(profile, [marker_a, marker_b])
        if not wait_for_cdp(PORT):
            log("FAIL: CDP port never opened")
            return 1
        log(f"CDP up on {PORT}")
        chrome_pid = find_chrome_pid_on_port(PORT)
        log(f"Chrome PID on port: {chrome_pid}")

        # First attach: this is what would happen in the FIRST operator slip session.
        time.sleep(1.0)
        r1 = attempt_attach("attach #1 (fresh chrome)")
        log(f"attach #1 → {r1}")

        # Verify Chrome is still alive (browser.close() must not kill the process).
        if chrome_pid is not None:
            import os
            try:
                os.kill(chrome_pid, 0)
                log(f"Chrome PID {chrome_pid} still alive after attach #1 close")
            except ProcessLookupError:
                log(f"Chrome PID {chrome_pid} died after browser.close() — "
                    "would have been kill-the-process anyway")
                return 2

        # Second attach: this is the operator-to-operator handoff scenario.
        time.sleep(1.0)
        r2 = attempt_attach("attach #2 (after disconnect — the claim)")
        log(f"attach #2 → {r2}")

        # Third attach: same again, to see if it's consistently broken or flaky.
        time.sleep(1.0)
        r3 = attempt_attach("attach #3 (second re-attach)")
        log(f"attach #3 → {r3}")

        log("--- VERDICT ---")
        for r in (r1, r2, r3):
            log(f"  {r['label']}: connect={r['connect_ok']} "
                f"setDownload={r['set_download_ok']} new_page={r['new_page_ok']} "
                f"reuse={r['reuse_existing_page_ok']} errors={len(r['errors'])}")
            for e in r["errors"]:
                log(f"      ! {e}")

        if chrome_pid is not None:
            log(f"killing spike Chrome pid={chrome_pid}")
            kill_pid(chrome_pid)
        else:
            # Fall back: re-find and kill.
            pid = find_chrome_pid_on_port(PORT)
            if pid:
                kill_pid(pid)

    return 0


if __name__ == "__main__":
    sys.exit(main())
