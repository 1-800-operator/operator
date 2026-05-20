"""Unit tests for branded_browser version-drift + fallback logic.

Mocks _build (no real 1.4GB clone/sign) and points BRANDED_APP / CHROME_APP
at temp dirs to exercise the build-if-missing / re-bake-on-drift / fall-back
decision tree.
"""
from __future__ import annotations

import plistlib
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import _1_800_operator.pipeline.branded_browser as bb


def _write_app(path: Path, version: str) -> None:
    (path / "Contents").mkdir(parents=True, exist_ok=True)
    with (path / "Contents" / "Info.plist").open("wb") as f:
        plistlib.dump({"CFBundleShortVersionString": version}, f)


def _stub_build(returns: bool = True):
    calls = {"n": 0}

    def fake():
        calls["n"] += 1
        return returns

    bb._build = fake
    return calls


def test_app_version():
    with tempfile.TemporaryDirectory() as tmp:
        app = Path(tmp) / "X.app"
        _write_app(app, "148.0.1")
        assert bb._app_version(app) == "148.0.1"
        assert bb._app_version(Path(tmp) / "missing.app") is None
    print("OK app_version")


def test_builds_when_missing():
    with tempfile.TemporaryDirectory() as tmp:
        bb.BRANDED_APP = Path(tmp) / "Operator Browser.app"  # does not exist
        calls = _stub_build()
        assert bb.ensure_branded_browser() == bb.BRANDED_APP
        assert calls["n"] == 1
    print("OK builds_when_missing")


def test_uses_existing_when_current():
    with tempfile.TemporaryDirectory() as tmp:
        branded = Path(tmp) / "Operator Browser.app"
        chrome = Path(tmp) / "Chrome.app"
        _write_app(branded, "148.0.1")
        _write_app(chrome, "148.0.1")
        bb.BRANDED_APP, bb.CHROME_APP = branded, chrome
        calls = _stub_build()
        assert bb.ensure_branded_browser() == branded
        assert calls["n"] == 0  # same version → no rebuild
    print("OK uses_existing_when_current")


def test_rebakes_on_drift():
    with tempfile.TemporaryDirectory() as tmp:
        branded = Path(tmp) / "Operator Browser.app"
        chrome = Path(tmp) / "Chrome.app"
        _write_app(branded, "148.0.1")
        _write_app(chrome, "149.0.0")  # Chrome updated
        bb.BRANDED_APP, bb.CHROME_APP = branded, chrome
        calls = _stub_build()
        bb.ensure_branded_browser()
        assert calls["n"] == 1  # drift → rebuild
    print("OK rebakes_on_drift")


def test_no_churn_on_unreadable_version():
    with tempfile.TemporaryDirectory() as tmp:
        branded = Path(tmp) / "Operator Browser.app"
        (branded / "Contents").mkdir(parents=True)  # exists but no Info.plist
        chrome = Path(tmp) / "Chrome.app"
        _write_app(chrome, "149.0.0")
        bb.BRANDED_APP, bb.CHROME_APP = branded, chrome
        calls = _stub_build()
        assert bb.ensure_branded_browser() == branded
        assert calls["n"] == 0  # unreadable version → don't churn
    print("OK no_churn_on_unreadable_version")


def test_returns_none_on_build_failure():
    with tempfile.TemporaryDirectory() as tmp:
        bb.BRANDED_APP = Path(tmp) / "Operator Browser.app"  # missing → build
        calls = _stub_build(returns=False)
        assert bb.ensure_branded_browser() is None
        assert calls["n"] == 1
    print("OK returns_none_on_build_failure")


if __name__ == "__main__":
    test_app_version()
    test_builds_when_missing()
    test_uses_existing_when_current()
    test_rebakes_on_drift()
    test_no_churn_on_unreadable_version()
    test_returns_none_on_build_failure()
    print("\nAll branded_browser tests passed.")
