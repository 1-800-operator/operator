"""
test_918_wizard_mcp_lock.py — Wizard reorder + MCP auto-enable from skill deps

The setup wizard now runs the skills step before the MCPs step, then locks
MCP-picker rows for any server declared in a chosen skill's `mcp-required`.
Locked rows render dim with a "required by: X" caption, force-check at init,
and are no-ops on space.

Covers:
  - Choice(locked=True) renders and behaves as designed in select_many.
  - _mcp_choice(locked_by=[...]) builds the right Choice.
  - _required_mcps_from_skills aggregates across bundled + user sources.
  - Warning fires when a skill declares a dep the agent doesn't scaffold.

Run: python tests/test_918_wizard_mcp_lock.py
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("OPERATOR_BOT", "pm")
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from _1_800_operator.pipeline.picker import Choice, select_many
from _1_800_operator.pipeline.setup import (
    WizardState,
    _mcp_choice,
    _required_mcps_from_skills,
)


def _write_skill(tmp: Path, name: str, mcp_required: list[str]) -> Path:
    folder = tmp / name
    folder.mkdir(parents=True, exist_ok=True)
    fm_req = f"mcp-required: {mcp_required}\n" if mcp_required else ""
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test {name}\n{fm_req}---\n\nbody\n",
        encoding="utf-8",
    )
    return folder


# ── Picker lock behavior ──────────────────────────────────────────────────


def test_picker_locked_row_forces_checked_even_when_initial_false():
    """select_many() overrides initial_checked=False for any locked choice."""
    choices = [
        Choice(label="free"),
        Choice(label="lockme", locked=True, locked_note="required by: s"),
    ]
    # Feed enter immediately; no toggles attempted.
    result = select_many(
        "",
        choices,
        initial_checked=[False, False],
        key_source=["\r"],  # enter
    )
    assert result == [False, True], (
        f"locked row should be forced-on at init; got {result}"
    )
    print("✓ locked row forces checked at init")


def test_picker_locked_row_ignores_space_toggle():
    """Space on the locked row is a no-op; the row stays on."""
    choices = [
        Choice(label="lockme", locked=True, locked_note="required by: s"),
        Choice(label="free"),
    ]
    # Cursor starts at index 0 (lockme). Space → should NOT toggle.
    # Then down arrow + space to flip 'free' on, then enter.
    import readchar
    keys = [" ", readchar.key.DOWN, " ", "\r"]
    result = select_many("", choices, initial_checked=[False, False], key_source=keys)
    assert result == [True, True], (
        f"locked row unchanged, free toggled on; got {result}"
    )
    print("✓ space on locked row is a no-op")


# ── _mcp_choice wiring ────────────────────────────────────────────────────


def test_mcp_choice_without_lock_is_unlocked():
    """No locked_by → normal Choice, no caption."""
    c = _mcp_choice("linear")
    assert c.locked is False
    assert c.locked_note == ""
    print("✓ _mcp_choice unlocked when no deps")


def test_mcp_choice_with_lock_carries_caption():
    """locked_by → locked=True and caption names the skill(s)."""
    c = _mcp_choice("github", locked_by=["pr-review", "release-notes"])
    assert c.locked is True
    assert "pr-review" in c.locked_note
    assert "release-notes" in c.locked_note
    assert c.locked_note.startswith("required by:")
    print("✓ _mcp_choice locks + captions when deps provided")


# ── _required_mcps_from_skills aggregator (Phase 15.11 shape) ──────────
# State now carries `enabled_skill_names: list[str]` + the skills block in
# `bot_cfg["skills"]["external_paths"]`. The aggregator resolves the names
# via load_skills() which unions shared library + external_paths.
import contextlib


@contextlib.contextmanager
def _no_shared_library():
    """Redirect DEFAULT_SHARED_LIBRARY to a nonexistent path so the
    aggregator doesn't pick up the real user library during tests.
    """
    from _1_800_operator.pipeline import skills as skills_mod
    original = skills_mod.DEFAULT_SHARED_LIBRARY
    skills_mod.DEFAULT_SHARED_LIBRARY = Path("/nonexistent-test-lib-918")
    try:
        yield
    finally:
        skills_mod.DEFAULT_SHARED_LIBRARY = original


def _make_state(enabled_names: list[str], external_paths: list[str]) -> WizardState:
    return WizardState(
        mode="new",
        name="test",
        display_name="Test",
        tagline="",
        based_on="pm",
        portrait="",
        bot_cfg={"skills": {"external_paths": external_paths}},
        enabled_skill_names=enabled_names,
    )


def test_required_mcps_aggregates_across_skills():
    """Enabled skills' mcp-required lists aggregate into one map."""
    with _no_shared_library(), tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_skill(tmp, "a", ["figma"])
        _write_skill(tmp, "b", ["figma", "github"])
        _write_skill(tmp, "c", [])
        state = _make_state(["a", "b", "c"], [str(tmp)])
        got = _required_mcps_from_skills(state)
        assert set(got.keys()) == {"figma", "github"}
        assert sorted(got["figma"]) == ["a", "b"]
        assert got["github"] == ["b"]
    print("✓ required_mcps aggregates across enabled skills")


def test_required_mcps_honors_enabled_filter():
    """Only enabled skills contribute; discovered-but-not-enabled ones don't."""
    with _no_shared_library(), tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_skill(tmp, "keep-me", ["linear"])
        _write_skill(tmp, "drop-me", ["sentry"])  # not in enabled_names
        state = _make_state(["keep-me"], [str(tmp)])
        got = _required_mcps_from_skills(state)
        assert got == {"linear": ["keep-me"]}
    print("✓ required_mcps honors enabled filter")


def test_required_mcps_empty_when_no_deps():
    """Enabled skills with no mcp-required contribute nothing."""
    with _no_shared_library(), tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_skill(tmp, "a", [])
        state = _make_state(["a"], [str(tmp)])
        got = _required_mcps_from_skills(state)
        assert got == {}
    print("✓ required_mcps is empty when no skill declares deps")


def test_required_mcps_empty_when_enabled_empty():
    """Empty enabled list short-circuits to empty map."""
    with _no_shared_library(), tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        _write_skill(tmp, "a", ["figma"])  # discovered but not enabled
        state = _make_state([], [str(tmp)])
        got = _required_mcps_from_skills(state)
        assert got == {}
    print("✓ required_mcps empty when enabled=[]")


if __name__ == "__main__":
    test_picker_locked_row_forces_checked_even_when_initial_false()
    test_picker_locked_row_ignores_space_toggle()
    test_mcp_choice_without_lock_is_unlocked()
    test_mcp_choice_with_lock_carries_caption()
    test_required_mcps_aggregates_across_skills()
    test_required_mcps_honors_enabled_filter()
    test_required_mcps_empty_when_no_deps()
    test_required_mcps_empty_when_enabled_empty()
    print("\nAll test_918 checks passed.")
