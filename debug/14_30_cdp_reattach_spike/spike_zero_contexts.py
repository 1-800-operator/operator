"""Targeted follow-up: does re-attach fail ONLY when Chrome has zero
BrowserContexts (the "menu-bar-only after last window closed" state)?

Per the git-blame trail, the original failure mode was:
    `Browser.setDownloadBehavior … Browser context management is not
    supported`
…on macOS, when the user closed the last slip window but Chrome stays
alive in the menu bar.

This spike re-runs three scenarios in one Chrome process:
    A) attach with 2 tabs open → expected OK
    B) close 1 tab so only 1 remains → re-attach → expected OK
    C) close the LAST tab → wait for the page count to hit zero →
       re-attach → expected FAIL with setDownloadBehavior error
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
PORT = 9334
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


def find_pid_on_port(port: int) -> int | None:
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


def page_count() -> int:
    """Ask CDP directly how many targets of type 'page' exist."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=2) as resp:
            targets = json.loads(resp.read().decode())
        return sum(1 for t in targets if t.get("type") == "page")
    except Exception as e:
        log(f"page_count via /json/list failed: {e}")
        return -1


def attempt_attach(label: str) -> dict:
    result: dict = {"label": label, "connect_ok": False,
                    "set_download_ok": None, "errors": [], "page_count_pre": None}
    result["page_count_pre"] = page_count()
    pw = sync_playwright().start()
    try:
        try:
            browser = pw.chromium.connect_over_cdp(CDP_URL)
            result["connect_ok"] = True
        except Exception as e:
            result["errors"].append(f"connect_over_cdp: {type(e).__name__}: {e}")
            return result

        try:
            ctx = browser.contexts[0] if browser.contexts else browser.new_context()
            if ctx.pages:
                cdp = ctx.new_cdp_session(ctx.pages[0])
            else:
                tmp = ctx.new_page()
                cdp = ctx.new_cdp_session(tmp)
            cdp.send("Browser.setDownloadBehavior", {"behavior": "deny"})
            result["set_download_ok"] = True
        except Exception as e:
            result["set_download_ok"] = False
            result["errors"].append(f"setDownloadBehavior: {type(e).__name__}: {e}")

        try:
            browser.close()
        except Exception as e:
            result["errors"].append(f"browser.close: {type(e).__name__}: {e}")
    finally:
        try:
            pw.stop()
        except Exception:
            pass
    return result


def close_a_tab_via_cdp(target_url_substr: str) -> bool:
    """Use raw CDP /json endpoints to close a tab without going through Playwright."""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(f"{CDP_URL}/json/list", timeout=2) as resp:
            targets = json.loads(resp.read().decode())
    except Exception as e:
        log(f"list targets failed: {e}")
        return False
    for t in targets:
        if t.get("type") == "page" and target_url_substr in t.get("url", ""):
            tid = t["id"]
            try:
                urllib.request.urlopen(f"{CDP_URL}/json/close/{tid}", timeout=2)
                return True
            except Exception as e:
                log(f"close {tid} failed: {e}")
                return False
    return False


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="spike-zc-") as tmp:
        profile = Path(tmp) / "profile"
        profile.mkdir()
        marker_a = "data:text/html,<title>USER-TAB-A</title>"
        marker_b = "data:text/html,<title>USER-TAB-B</title>"

        args = [
            "open", "-na", "Google Chrome", "--args",
            f"--remote-debugging-port={PORT}",
            "--remote-allow-origins=*",
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--no-default-browser-check",
            marker_a, marker_b,
        ]
        log(f"launching Chrome with 2 user tabs")
        subprocess.Popen(args)
        if not wait_for_cdp(PORT):
            log("FAIL: CDP port never opened")
            return 1
        pid = find_pid_on_port(PORT)
        log(f"Chrome PID: {pid}, initial page count: {page_count()}")
        time.sleep(1.0)

        # Scenario A: 2 tabs open
        rA = attempt_attach("A: 2 tabs alive")
        log(f"A → {rA}")

        # Close one tab via raw CDP (no Playwright involvement) → 1 left
        log("closing tab A via raw CDP")
        ok = close_a_tab_via_cdp("USER-TAB-A")
        log(f"close ok={ok}, page count now={page_count()}")
        time.sleep(0.5)

        rB = attempt_attach("B: 1 tab alive")
        log(f"B → {rB}")

        # Close the LAST tab → 0 left
        log("closing tab B via raw CDP — Chrome should go to menu-bar-only")
        ok = close_a_tab_via_cdp("USER-TAB-B")
        log(f"close ok={ok}")
        # Give Chrome a moment to settle into the zero-context state
        for i in range(10):
            time.sleep(0.5)
            n = page_count()
            log(f"  page count after {(i+1)*0.5:.1f}s: {n}")
            if n == 0:
                break
        # Verify Chrome process still alive
        import os
        try:
            os.kill(pid, 0)
            log(f"Chrome PID {pid} still alive in menu-bar-only state")
        except ProcessLookupError:
            log(f"Chrome PID {pid} EXITED when last tab closed — no zero-context state possible")
            return 0

        rC = attempt_attach("C: 0 tabs (menu-bar-only)")
        log(f"C → {rC}")

        log("--- VERDICT ---")
        for r in (rA, rB, rC):
            log(f"  {r['label']}: pre_pages={r['page_count_pre']} "
                f"connect={r['connect_ok']} setDownload={r['set_download_ok']} "
                f"errors={len(r['errors'])}")
            for e in r["errors"]:
                log(f"      ! {e}")

        # Cleanup
        pid = find_pid_on_port(PORT)
        if pid:
            log(f"killing Chrome pid={pid}")
            kill_pid(pid)

    return 0


if __name__ == "__main__":
    sys.exit(main())
