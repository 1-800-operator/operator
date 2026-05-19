"""Spike (S250): does Playwright connect_over_cdp expose + evaluate in a
CROSS-ORIGIN (OOPIF) iframe?

This is the load-bearing assumption behind the Google Chat iframe chat
observer (chat_dom_js.INSTALL_GCHAT_OBSERVER_JS + attach_adapter
_find_gchat_frame). A Meet attached to a Google Chat space renders chat in a
cross-origin chat.google.com iframe; the observer must be installed INTO that
frame via Playwright's Frame.evaluate. Cross-origin iframes are out-of-process
(site isolation), and Playwright-over-CDP historically had OOPIF edge cases.

Synthetic setup avoids needing a live space-attached meeting: parent on :8801,
child iframe on :8802 (different port = different origin = OOPIF). Launch a
throwaway headless Chrome on CDP :9333 (NOT the dial Chrome's 9222), then
connect_over_cdp and confirm page.frames includes the child + frame.evaluate
reaches it + a [data-topic-id][data-is-user-topic] selector resolves in-frame.

Result 2026-05-19 (Playwright 1.58, Chrome 148): PASS. Re-run after Playwright
or Chrome major upgrades.

    python debug/14_34_audio_replay/oopif_frame_evaluate_spike.py
"""
import http.server
import os
import shutil
import socketserver
import subprocess
import tempfile
import threading
import time

from playwright.sync_api import sync_playwright


def _serve(directory, port):
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(*a, directory=directory, **k)
    httpd = socketserver.TCPServer(("127.0.0.1", port), handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> int:
    d_parent = tempfile.mkdtemp()
    d_child = tempfile.mkdtemp()
    with open(os.path.join(d_parent, "parent.html"), "w") as f:
        f.write('<!doctype html><h1>parent</h1>'
                '<iframe src="http://127.0.0.1:8802/child.html"></iframe>')
    with open(os.path.join(d_child, "child.html"), "w") as f:
        f.write('<!doctype html><body>'
                '<div data-topic-id="X1" data-is-user-topic="true">child-msg</div>'
                '<script>window.__childMarker=42;</script>')
    s1 = _serve(d_parent, 8801)
    s2 = _serve(d_child, 8802)

    chrome = (shutil.which("google-chrome")
              or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    prof = tempfile.mkdtemp()
    proc = subprocess.Popen(
        [chrome, "--headless=new", "--remote-debugging-port=9333",
         f"--user-data-dir={prof}", "--no-first-run",
         "http://127.0.0.1:8801/parent.html"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3.0)
    rc = 1
    try:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp("http://localhost:9333")
            ctx = browser.contexts[0]
            page = next((pg for pg in ctx.pages if "parent.html" in (pg.url or "")),
                        ctx.pages[0] if ctx.pages else None)
            print("frame count:", len(page.frames))
            child = next((fr for fr in page.frames if "8802" in (fr.url or "")), None)
            print("cross-origin child in page.frames:", child is not None)
            if child:
                val = child.evaluate("() => window.__childMarker")
                sel = child.evaluate(
                    '() => { const t=document.querySelector('
                    '"[data-topic-id][data-is-user-topic=\\"true\\"]"); '
                    'return t ? t.innerText : null; }')
                print("frame.evaluate marker:", val, "| selector:", repr(sel))
                rc = 0 if (val == 42 and sel == "child-msg") else 1
            print("VERDICT:", "PASS" if rc == 0 else "FAIL")
            browser.close()
    finally:
        proc.terminate()
        s1.shutdown()
        s2.shutdown()
        shutil.rmtree(prof, ignore_errors=True)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
