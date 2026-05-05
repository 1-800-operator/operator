"""
test_917_skill_mcp_required.py — Skill frontmatter mcp-required parsing

Skills that fundamentally depend on an MCP server declare `mcp-required` in
their frontmatter so the setup wizard can preseed enabled=true for those
servers. Missing field = empty list (honest default for user-authored skills
that don't follow the pattern — runtime falls back on the granular
"disabled server" error from test_916).

Run: python tests/test_917_skill_mcp_required.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("OPERATOR_BOT", "pm")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.skills import Skill, _parse_skill_md, load_skills


def _write_skill(tmp: Path, name: str, frontmatter_extra: str = "") -> Path:
    """Write a SKILL.md with standard name/description + optional extra frontmatter lines."""
    folder = tmp / name
    folder.mkdir(parents=True, exist_ok=True)
    body = (
        "---\n"
        f"name: {name}\n"
        f"description: test skill {name}\n"
        f"{frontmatter_extra}"
        "---\n\n"
        f"# {name}\n"
    )
    p = folder / "SKILL.md"
    p.write_text(body, encoding="utf-8")
    return p


def test_mcp_required_list_form():
    """`mcp-required: [figma, github]` parses to a two-element list."""
    with tempfile.TemporaryDirectory() as td:
        p = _write_skill(Path(td), "s", "mcp-required: [figma, github]\n")
        skill = _parse_skill_md(p)
        assert skill is not None
        assert skill.mcp_required == ["figma", "github"]
    print("✓ list form parses into mcp_required")


def test_mcp_required_underscore_alias():
    """`mcp_required:` (underscore) also accepted — YAML-ergonomic alias."""
    with tempfile.TemporaryDirectory() as td:
        p = _write_skill(Path(td), "s", "mcp_required: [sentry]\n")
        skill = _parse_skill_md(p)
        assert skill is not None
        assert skill.mcp_required == ["sentry"]
    print("✓ underscore alias accepted")


def test_mcp_required_missing_defaults_to_empty():
    """Skills without the field default to []; no warning raised."""
    with tempfile.TemporaryDirectory() as td:
        p = _write_skill(Path(td), "s")
        skill = _parse_skill_md(p)
        assert skill is not None
        assert skill.mcp_required == []
    print("✓ missing field defaults to empty")


def test_mcp_required_string_csv_form():
    """Comma-separated string form (mirrors allowed-tools) splits on commas."""
    with tempfile.TemporaryDirectory() as td:
        p = _write_skill(Path(td), "s", "mcp-required: figma, github\n")
        skill = _parse_skill_md(p)
        assert skill is not None
        assert skill.mcp_required == ["figma", "github"]
    print("✓ csv string form splits on commas")


def test_mcp_required_empty_list():
    """Explicit empty list stays empty."""
    with tempfile.TemporaryDirectory() as td:
        p = _write_skill(Path(td), "s", "mcp-required: []\n")
        skill = _parse_skill_md(p)
        assert skill is not None
        assert skill.mcp_required == []
    print("✓ explicit empty list parses")


def test_mcp_required_unexpected_type_ignored():
    """Non-list/non-string values warn and default to empty (don't crash)."""
    with tempfile.TemporaryDirectory() as td:
        p = _write_skill(Path(td), "s", "mcp-required: {figma: true}\n")
        skill = _parse_skill_md(p)
        assert skill is not None
        assert skill.mcp_required == []
    print("✓ unexpected type yields empty list without crash")


def test_bundled_skills_declare_deps_correctly():
    """All MCP-dependent bundled skills declare their server in mcp-required;
    the discussion-only skills declare nothing."""
    src = Path(__file__).resolve().parent.parent / "src" / "_1_800_operator" / "skills"
    expected = {
        "design-handoff-spec": ["figma"],
        "pr-review": ["github"],
        "release-notes": ["github"],
        "schedule-followup": ["calendar"],
        # Discussion-only; no hard MCP dep.
        "scope-estimate": [],
        "prd-from-discussion": [],
        "standup-summary": [],
    }
    # Collect by name.
    by_name = {}
    for md in src.rglob("*/SKILL.md"):
        skill = _parse_skill_md(md)
        assert skill is not None, f"failed to parse {md}"
        by_name[skill.name] = skill.mcp_required

    missing = [n for n in expected if n not in by_name]
    assert not missing, f"bundled skills not found: {missing}"
    for name, want in expected.items():
        got = by_name[name]
        assert got == want, f"{name}: want mcp-required={want}, got {got}"
    print(f"✓ bundled skill deps match expected ({len(expected)} skills checked)")


def test_load_skills_preserves_mcp_required():
    """End-to-end: load_skills() returns Skill objects with mcp_required populated."""
    with tempfile.TemporaryDirectory() as td:
        _write_skill(Path(td), "alpha", "mcp-required: [linear]\n")
        _write_skill(Path(td), "beta")
        skills = load_skills(
            None,
            external_paths=[td],
            shared_library_dir=Path(td) / "no-library",
        )
        got = {s.name: s.mcp_required for s in skills}
        assert got == {"alpha": ["linear"], "beta": []}
    print("✓ load_skills surfaces mcp_required end-to-end")


if __name__ == "__main__":
    test_mcp_required_list_form()
    test_mcp_required_underscore_alias()
    test_mcp_required_missing_defaults_to_empty()
    test_mcp_required_string_csv_form()
    test_mcp_required_empty_list()
    test_mcp_required_unexpected_type_ignored()
    test_bundled_skills_declare_deps_correctly()
    test_load_skills_preserves_mcp_required()
    print("\nAll test_917 checks passed.")
