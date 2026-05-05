"""Tests for pipeline/install_preflight.py.

Verifies:
- chromium_installed() honors PLAYWRIGHT_BROWSERS_PATH override.
- chromium_installed() False on empty dir, True when chromium-* present.
- seed_env_file() creates file with mode 600 when missing.
- seed_env_file() leaves existing file untouched (never overwrites real keys).

Standalone — run via `python tests/test_install_preflight.py`.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

# Allow running from repo root without -m.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

os.environ.setdefault("OPERATOR_BOT", "claude")

from _1_800_operator.pipeline import install_preflight  # noqa: E402


_PASS = "\033[32mPASS\033[0m"
_FAIL = "\033[31mFAIL\033[0m"


def _check(label: str, cond: bool, detail: str = "") -> bool:
    print(f"  {_PASS if cond else _FAIL}  {label}{(' — ' + detail) if detail and not cond else ''}")
    return cond


def test_chromium_installed_honors_browsers_path() -> None:
    print("test_chromium_installed_honors_browsers_path")
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": str(tmp_path)}, clear=False):
            ok1 = _check(
                "empty dir → False",
                install_preflight.chromium_installed() is False,
            )
            (tmp_path / "chromium-1234").mkdir()
            ok2 = _check(
                "dir with chromium-* → True",
                install_preflight.chromium_installed() is True,
            )
            (tmp_path / "chromium-1234").rmdir()
            (tmp_path / "firefox-9999").mkdir()
            ok3 = _check(
                "dir with only firefox-* → False",
                install_preflight.chromium_installed() is False,
            )
    assert ok1 and ok2 and ok3


def test_chromium_installed_missing_root() -> None:
    print("test_chromium_installed_missing_root")
    nonexistent = "/tmp/operator-test-does-not-exist-zzz"
    Path(nonexistent).rmdir() if Path(nonexistent).exists() else None
    with mock.patch.dict(os.environ, {"PLAYWRIGHT_BROWSERS_PATH": nonexistent}, clear=False):
        ok = _check(
            "missing root dir → False",
            install_preflight.chromium_installed() is False,
        )
    assert ok


def test_seed_env_file_creates_with_mode_600() -> None:
    print("test_seed_env_file_creates_with_mode_600")
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        with mock.patch.object(install_preflight, "_ENV_FILE", env_path):
            created = install_preflight.seed_env_file()
            ok1 = _check("returned True (newly created)", created is True)
            ok2 = _check("file exists", env_path.exists())
            mode = env_path.stat().st_mode & 0o777
            ok3 = _check(f"mode 600 (got {oct(mode)})", mode == 0o600)
            content = env_path.read_text()
            ok4 = _check(
                "contains ANTHROPIC_API_KEY placeholder",
                "ANTHROPIC_API_KEY" in content,
            )
            ok5 = _check(
                "contains OPENAI_API_KEY placeholder",
                "OPENAI_API_KEY" in content,
            )
            ok6 = _check(
                "contains GITHUB_TOKEN placeholder",
                "GITHUB_TOKEN" in content,
            )
    assert ok1 and ok2 and ok3 and ok4 and ok5 and ok6


def test_seed_env_file_idempotent() -> None:
    print("test_seed_env_file_idempotent")
    with tempfile.TemporaryDirectory() as tmp:
        env_path = Path(tmp) / ".env"
        env_path.write_text("ANTHROPIC_API_KEY=sk-real-key\n")
        env_path.chmod(0o600)
        with mock.patch.object(install_preflight, "_ENV_FILE", env_path):
            created = install_preflight.seed_env_file()
            ok1 = _check("returned False (already exists)", created is False)
            ok2 = _check(
                "real key preserved",
                env_path.read_text() == "ANTHROPIC_API_KEY=sk-real-key\n",
            )
    assert ok1 and ok2


def test_browsers_root_default_paths() -> None:
    print("test_browsers_root_default_paths")
    # Wipe override.
    env = dict(os.environ)
    env.pop("PLAYWRIGHT_BROWSERS_PATH", None)
    with mock.patch.dict(os.environ, env, clear=True):
        root = install_preflight._playwright_browsers_root()
        if sys.platform == "darwin":
            ok = _check(
                "macOS default = ~/Library/Caches/ms-playwright",
                root == Path.home() / "Library" / "Caches" / "ms-playwright",
            )
        else:
            ok = _check(
                "Linux default = ~/.cache/ms-playwright",
                root == Path.home() / ".cache" / "ms-playwright",
            )
    assert ok


def main() -> int:
    print("install_preflight tests\n")
    failures = 0
    for fn in [
        test_chromium_installed_honors_browsers_path,
        test_chromium_installed_missing_root,
        test_seed_env_file_creates_with_mode_600,
        test_seed_env_file_idempotent,
        test_browsers_root_default_paths,
    ]:
        try:
            fn()
        except AssertionError:
            failures += 1
            print(f"  -> {_FAIL} test failed\n")
        except Exception as e:
            failures += 1
            print(f"  -> {_FAIL} unexpected: {type(e).__name__}: {e}\n")
        else:
            print()
    if failures:
        print(f"{failures} test(s) failed.")
        return 1
    print("All tests passed.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
