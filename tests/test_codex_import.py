"""
Tests for pipeline/codex_import.py — the install + login preflight gate.

Patches `shutil.which` and `subprocess.run` so we can drive every branch
without depending on the user's actual codex CLI state.

Usage:
    python tests/test_codex_import.py
"""
import os
import subprocess
import sys
import unittest.mock as _mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator.pipeline import codex_import


def _check(label, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        raise AssertionError(label)


def _make_run(answers):
    """Build a fake subprocess.run that returns canned results by argv[0:2]."""
    def fake_run(argv, *a, **kw):
        key = " ".join(argv[:3]) if isinstance(argv, list) else str(argv)
        for matcher, result in answers:
            if matcher in key:
                if isinstance(result, Exception):
                    raise result
                rc, stdout, stderr = result
                return subprocess.CompletedProcess(
                    args=argv, returncode=rc, stdout=stdout, stderr=stderr,
                )
        raise AssertionError(f"unexpected subprocess.run for {argv}")
    return fake_run


def test_codex_missing_returns_false():
    print("\n1. codex not on PATH → (False, install hint)")
    with _mock.patch("_1_800_operator.pipeline.codex_import.shutil.which", return_value=None):
        ok, reason = codex_import.codex_installed_and_logged_in()
    _check("ok=False", not ok)
    _check("hint mentions npm install", "npm install" in reason)
    _check("hint mentions @openai/codex", "@openai/codex" in reason)


def test_chatgpt_login_succeeds():
    print("\n2. codex installed + logged in via ChatGPT → (True, '')")
    answers = [
        ("codex --version", (0, "codex-cli 0.128.0\n", "")),
        ("codex login status", (0, "", "Logged in using ChatGPT\n")),
    ]
    with _mock.patch.object(codex_import.shutil, "which", return_value="/usr/local/bin/codex"), \
         _mock.patch.object(codex_import.subprocess, "run", side_effect=_make_run(answers)):
        ok, reason = codex_import.codex_installed_and_logged_in()
    _check("ok=True", ok)
    _check("no reason text", reason == "")


def test_api_key_login_rejected():
    print("\n3. logged in with API key → rejected (subscription-only)")
    answers = [
        ("codex --version", (0, "codex-cli 0.128.0\n", "")),
        ("codex login status", (0, "Logged in using API key\n", "")),
    ]
    with _mock.patch.object(codex_import.shutil, "which", return_value="/usr/local/bin/codex"), \
         _mock.patch.object(codex_import.subprocess, "run", side_effect=_make_run(answers)):
        ok, reason = codex_import.codex_installed_and_logged_in()
    _check("ok=False", not ok)
    _check("hint mentions API key", "API key" in reason)
    _check("hint mentions codex logout", "codex logout" in reason)


def test_login_status_nonzero_rejected():
    print("\n4. `codex login status` exits non-zero → rejected with login hint")
    answers = [
        ("codex --version", (0, "codex-cli 0.128.0\n", "")),
        ("codex login status", (1, "", "Not logged in\n")),
    ]
    with _mock.patch.object(codex_import.shutil, "which", return_value="/usr/local/bin/codex"), \
         _mock.patch.object(codex_import.subprocess, "run", side_effect=_make_run(answers)):
        ok, reason = codex_import.codex_installed_and_logged_in()
    _check("ok=False", not ok)
    _check("hint mentions codex login", "codex login" in reason)


def test_login_status_timeout_rejected():
    print("\n5. `codex login status` timeout → rejected, hint says try manually")
    def fake_run(argv, *a, **kw):
        if "version" in " ".join(argv):
            return subprocess.CompletedProcess(argv, 0, "codex-cli 0.128.0\n", "")
        raise subprocess.TimeoutExpired(cmd=argv, timeout=5)
    with _mock.patch.object(codex_import.shutil, "which", return_value="/usr/local/bin/codex"), \
         _mock.patch.object(codex_import.subprocess, "run", side_effect=fake_run):
        ok, reason = codex_import.codex_installed_and_logged_in()
    _check("ok=False", not ok)
    _check("hint mentions timeout / manually", "manually" in reason)


def test_version_drift_warns_but_does_not_block():
    print("\n6. Version drift → WARN, login still authoritative")
    import io, logging
    captured = io.StringIO()
    handler = logging.StreamHandler(captured)
    logger = logging.getLogger("_1_800_operator.pipeline.codex_import")
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    answers = [
        ("codex --version", (0, "codex-cli 0.999.0\n", "")),  # future version
        ("codex login status", (0, "", "Logged in using ChatGPT\n")),
    ]
    try:
        with _mock.patch.object(codex_import.shutil, "which", return_value="/usr/local/bin/codex"), \
             _mock.patch.object(codex_import.subprocess, "run", side_effect=_make_run(answers)):
            ok, reason = codex_import.codex_installed_and_logged_in()
        _check("ok=True (drift does NOT block)", ok)
        log_output = captured.getvalue()
        _check("WARNING contains 'version drift'", "version drift" in log_output)
    finally:
        logger.removeHandler(handler)


def main():
    print("=" * 50)
    print("codex_import preflight")
    print("=" * 50)
    failed = 0
    for fn in [
        test_codex_missing_returns_false,
        test_chatgpt_login_succeeds,
        test_api_key_login_rejected,
        test_login_status_nonzero_rejected,
        test_login_status_timeout_rejected,
        test_version_drift_warns_but_does_not_block,
    ]:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1
    print("\n" + "=" * 50)
    if failed == 0:
        print("All tests passed!")
        return 0
    print(f"{failed} test(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
