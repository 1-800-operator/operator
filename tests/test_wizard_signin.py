"""Tests for the Phase 14.10 wizard Google sign-in step.

Pure-helper coverage for `detect_google_session` against fixture
auth_state.json + google_account.json files; the launch flow itself
(Playwright + interactive sign-in) is not unit-tested — it gets a smoke
test in the live-QA marathon (Phase 14.8).

Run: python tests/test_wizard_signin.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator.pipeline import chrome_preflight, google_signin  # noqa: E402


def _write_auth_state(path: Path, *, with_sid: bool = True) -> None:
    cookies = []
    if with_sid:
        cookies.append({"name": "SID", "domain": ".google.com", "value": "x"})
    path.write_text(json.dumps({"cookies": cookies}), encoding="utf-8")


def _write_account(path: Path, email: str) -> None:
    path.write_text(json.dumps({"email": email}), encoding="utf-8")


# ── detect_google_session ────────────────────────────────────────────────


def test_detect_no_files_returns_undetected():
    with tempfile.TemporaryDirectory() as tmp:
        result = google_signin.detect_google_session(
            auth_state_path=Path(tmp) / "auth_state.json",
            account_file=Path(tmp) / "google_account.json",
        )
        assert result.detected is False
        assert result.email is None


def test_detect_auth_state_without_sid_returns_undetected():
    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth_state.json"
        _write_auth_state(auth, with_sid=False)
        result = google_signin.detect_google_session(
            auth_state_path=auth,
            account_file=Path(tmp) / "google_account.json",
        )
        assert result.detected is False
        assert result.email is None


def test_detect_with_sid_no_email_cache_returns_detected_no_email():
    """Legacy profile case — auth_state present, account file absent."""
    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth_state.json"
        _write_auth_state(auth)
        result = google_signin.detect_google_session(
            auth_state_path=auth,
            account_file=Path(tmp) / "google_account.json",
        )
        assert result.detected is True
        assert result.email is None


def test_detect_with_sid_and_email_cache():
    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth_state.json"
        account = Path(tmp) / "google_account.json"
        _write_auth_state(auth)
        _write_account(account, "foo@gmail.com")
        result = google_signin.detect_google_session(
            auth_state_path=auth,
            account_file=account,
        )
        assert result.detected is True
        assert result.email == "foo@gmail.com"


def test_detect_account_file_malformed_falls_back_to_no_email():
    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth_state.json"
        account = Path(tmp) / "google_account.json"
        _write_auth_state(auth)
        account.write_text("not valid json {", encoding="utf-8")
        result = google_signin.detect_google_session(
            auth_state_path=auth,
            account_file=account,
        )
        assert result.detected is True
        assert result.email is None


def test_detect_account_file_missing_email_key():
    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth_state.json"
        account = Path(tmp) / "google_account.json"
        _write_auth_state(auth)
        account.write_text(json.dumps({"other": "x"}), encoding="utf-8")
        result = google_signin.detect_google_session(
            auth_state_path=auth,
            account_file=account,
        )
        assert result.detected is True
        assert result.email is None


def test_detect_account_file_email_without_at_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        auth = Path(tmp) / "auth_state.json"
        account = Path(tmp) / "google_account.json"
        _write_auth_state(auth)
        account.write_text(json.dumps({"email": "not-an-email"}), encoding="utf-8")
        result = google_signin.detect_google_session(
            auth_state_path=auth,
            account_file=account,
        )
        assert result.detected is True
        assert result.email is None


# ── _read_account_email helper ───────────────────────────────────────────


def test_read_account_email_returns_string():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "a.json"
        _write_account(path, "bar@example.com")
        assert google_signin._read_account_email(path) == "bar@example.com"


def test_read_account_email_missing_file_returns_none():
    assert google_signin._read_account_email(Path("/nonexistent/path.json")) is None


# ── _capture_email DOM regex (unit-testable in isolation) ────────────────


def test_email_regex_matches_aria_label():
    """The capture path relies on the regex finding an email in scraped text."""
    sample = 'Google Account: Foo Bar (foo.bar+test@gmail.com)'
    m = google_signin._EMAIL_RE.search(sample)
    assert m is not None
    assert m.group(0) == "foo.bar+test@gmail.com"


def test_email_regex_no_false_match_on_plain_text():
    sample = 'no email here just words'
    assert google_signin._EMAIL_RE.search(sample) is None


# ── chrome_preflight ─────────────────────────────────────────────────────


def test_chrome_installed_non_darwin_always_true():
    """Linux + Windows skip the check (linux_adapter uses bundled chromium)."""
    real_platform = sys.platform
    try:
        sys.platform = "linux"
        assert chrome_preflight.chrome_installed() is True
    finally:
        sys.platform = real_platform


def test_chrome_installed_darwin_true_when_path_exists():
    real_platform = sys.platform
    real_path = chrome_preflight.CHROME_PATH
    with tempfile.TemporaryDirectory() as tmp:
        fake = Path(tmp) / "Chrome"
        fake.write_text("x")
        try:
            sys.platform = "darwin"
            chrome_preflight.CHROME_PATH = fake
            assert chrome_preflight.chrome_installed() is True
        finally:
            sys.platform = real_platform
            chrome_preflight.CHROME_PATH = real_path


def test_chrome_installed_darwin_false_when_path_missing():
    real_platform = sys.platform
    real_path = chrome_preflight.CHROME_PATH
    try:
        sys.platform = "darwin"
        chrome_preflight.CHROME_PATH = Path("/nonexistent/Chrome")
        assert chrome_preflight.chrome_installed() is False
    finally:
        sys.platform = real_platform
        chrome_preflight.CHROME_PATH = real_path


# ── Runner ────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ✗ {fn.__name__}: {type(e).__name__}: {e}")
    if failed:
        print(f"\n{failed} test(s) failed")
        sys.exit(1)
    print(f"\nAll {len(fns)} tests passed.")
