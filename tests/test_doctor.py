"""
Tests for `operator doctor` checks.

Covers _check_cwd_trusted — the workspace-trust check, the inform-only
half of operator's trust-dialog handling (operator detects + warns, never
writes the trust state itself). Run:

    source venv/bin/activate
    python tests/test_doctor.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.doctor import _check_cwd_trusted


def _registry(tmp, projects):
    """Write a minimal ~/.claude.json-shaped file; return its path."""
    p = Path(tmp) / ".claude.json"
    p.write_text(json.dumps({"projects": projects}), encoding="utf-8")
    return p


def test_trusted_cwd():
    """projects[<cwd>].hasTrustDialogAccepted is True → ok, no warning."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "proj"
        cwd.mkdir()
        reg = _registry(tmp, {str(cwd): {"hasTrustDialogAccepted": True}})
        r = _check_cwd_trusted(registry_path=reg, cwd=cwd)
        assert r.ok, r
        assert "trusted" in r.detail, r.detail
    print("  trusted cwd → ok OK")


def test_untrusted_cwd_entry_exists():
    """Entry exists but hasTrustDialogAccepted is false → optional warning."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "proj"
        cwd.mkdir()
        reg = _registry(tmp, {str(cwd): {"hasTrustDialogAccepted": False}})
        r = _check_cwd_trusted(registry_path=reg, cwd=cwd)
        assert not r.ok and r.optional, r
        assert "not trusted" in r.detail, r.detail
        assert "trust prompt" in r.fix, r.fix
    print("  untrusted cwd (entry exists) → optional warning OK")


def test_cwd_not_in_registry():
    """cwd absent from projects entirely → optional warning (never opened
    claude here, so it's untrusted)."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "proj"
        cwd.mkdir()
        reg = _registry(tmp, {"/some/other/dir": {"hasTrustDialogAccepted": True}})
        r = _check_cwd_trusted(registry_path=reg, cwd=cwd)
        assert not r.ok and r.optional, r
    print("  cwd not in registry → optional warning OK")


def test_registry_missing_fails_open():
    """A missing ~/.claude.json skips the check (ok=True) — fail open
    rather than hard-block on our own check being unable to read."""
    with tempfile.TemporaryDirectory() as tmp:
        r = _check_cwd_trusted(registry_path=Path(tmp) / "nope.json", cwd=Path(tmp))
        assert r.ok and r.optional, r
        assert "skipped" in r.detail, r.detail
    print("  missing registry → fails open (skipped) OK")


def test_registry_garbage_fails_open():
    """Unparseable JSON in the registry also fails open."""
    with tempfile.TemporaryDirectory() as tmp:
        reg = Path(tmp) / ".claude.json"
        reg.write_text("{not json", encoding="utf-8")
        r = _check_cwd_trusted(registry_path=reg, cwd=Path(tmp))
        assert r.ok and r.optional, r
    print("  garbage registry → fails open OK")


def test_resolved_path_match():
    """cwd is matched both as-is and resolved — covers macOS /tmp vs
    /private/tmp and other symlinked paths. The registry here is keyed
    ONLY under the resolved path; on macOS temp dirs cwd != resolved, so
    this genuinely exercises the resolve() fallback."""
    with tempfile.TemporaryDirectory() as tmp:
        cwd = Path(tmp) / "proj"
        cwd.mkdir()
        resolved = str(cwd.resolve())
        reg = _registry(tmp, {resolved: {"hasTrustDialogAccepted": True}})
        r = _check_cwd_trusted(registry_path=reg, cwd=cwd)
        assert r.ok, f"resolved-path match failed (cwd={cwd}, resolved={resolved}): {r}"
    print("  resolved-path match OK")


def main():
    tests = [
        test_trusted_cwd,
        test_untrusted_cwd_entry_exists,
        test_cwd_not_in_registry,
        test_registry_missing_fails_open,
        test_registry_garbage_fails_open,
        test_resolved_path_match,
    ]
    for t in tests:
        print(t.__name__)
        t()
    print(f"\nAll {len(tests)} doctor tests passed.")


if __name__ == "__main__":
    main()
