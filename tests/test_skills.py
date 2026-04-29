"""
Tests for pipeline.skills loader + LLMClient/ChatRunner wiring.
Run: python tests/test_skills.py
"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
os.environ.setdefault("BRAINCHILD_BOT", "pm")

import logging
import tempfile
from pathlib import Path
from unittest.mock import MagicMock


def _write_skill(folder: Path, name: str, description: str, body: str = "do the thing", extra_fm: str = ""):
    folder.mkdir(parents=True, exist_ok=True)
    fm = f"---\nname: {name}\ndescription: {description}\n"
    if extra_fm:
        fm += extra_fm
    fm += "---\n"
    (folder / "SKILL.md").write_text(fm + body)


def test_happy_path_single_folder():
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "commit"
        _write_skill(root, "commit", "Create a git commit.", body="Run git commit.")
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert len(skills) == 1
        assert skills[0].name == "commit"
        assert skills[0].description == "Create a git commit."
        assert "Run git commit." in skills[0].body
    print("  happy path single: PASS")


def test_parent_dir_scan():
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        _write_skill(parent / "a", "alpha", "A skill.")
        _write_skill(parent / "b", "beta", "B skill.")
        skills = load_skills(None, external_paths=[str(parent)], shared_library_dir=Path(tmp) / "no-library")
        names = sorted(s.name for s in skills)
        assert names == ["alpha", "beta"], names
    print("  parent dir scan: PASS")


def test_missing_path_warns_no_crash(caplog_list):
    from brainchild.pipeline.skills import load_skills
    skills = load_skills(
        None,
        external_paths=["/nonexistent/path/that/does/not/exist"],
        shared_library_dir=Path("/nonexistent-test-lib"),
    )
    assert skills == []
    assert any("not found" in r.getMessage() for r in caplog_list), "missing path should WARN"
    print("  missing path warn: PASS")


def test_malformed_yaml_skipped(caplog_list):
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "bad"
        root.mkdir()
        # YAML that fails to parse inside frontmatter.
        (root / "SKILL.md").write_text("---\nname: x\n:::broken\n---\nbody\n")
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert skills == []
        assert any("malformed YAML" in r.getMessage() for r in caplog_list)
    print("  malformed yaml skipped: PASS")


def test_missing_fields_skipped(caplog_list):
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "incomplete"
        root.mkdir()
        (root / "SKILL.md").write_text("---\nname: x\n---\nbody\n")  # no description
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert skills == []
        assert any("missing name or description" in r.getMessage() for r in caplog_list)
    print("  missing fields skipped: PASS")


def test_duplicate_last_wins(caplog_list):
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        a = Path(tmp) / "a" / "commit"
        b = Path(tmp) / "b" / "commit"
        _write_skill(a, "commit", "First", body="first body")
        _write_skill(b, "commit", "Second", body="second body")
        skills = load_skills(None, external_paths=[str(a), str(b)], shared_library_dir=Path(tmp) / "no-library")
        assert len(skills) == 1
        assert skills[0].body.startswith("second body"), skills[0].body
        assert any("overrides" in r.getMessage() for r in caplog_list)
    print("  duplicate last-wins: PASS")


def test_deep_nesting_ignored():
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        # Two-levels deep — should be ignored by parent-dir scan.
        _write_skill(parent / "outer" / "inner", "deep", "Too deep.")
        skills = load_skills(None, external_paths=[str(parent)], shared_library_dir=Path(tmp) / "no-library")
        assert skills == [], f"expected no skills, got {[s.name for s in skills]}"
    print("  deep nesting ignored: PASS")


def test_allowed_tools_warn(caplog_list):
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "with-tools"
        _write_skill(
            root, "tool-skill", "Uses tools.",
            extra_fm="allowed-tools: [Bash, Edit, load_skill]\n",
        )
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert len(skills) == 1
        assert "load_skill" in skills[0].allowed_tools
        assert "Bash" in skills[0].allowed_tools  # still loaded, just warned
        assert any("unsupported allowed-tools" in r.getMessage() for r in caplog_list)
    print("  allowed-tools warn: PASS")


def test_llm_inject_skills_menu():
    """Progressive mode puts the menu (names + descriptions) in the system prompt, not the bodies."""
    from brainchild.pipeline.llm import LLMClient
    from brainchild.pipeline.skills import Skill

    llm = LLMClient(MagicMock())
    base = llm._system_prompt
    skills = [
        Skill(name="commit", description="Create a commit.", body="secret body"),
    ]
    llm.inject_skills(skills, progressive=True)
    sys_text = llm._system_prompt
    assert "commit: Create a commit." in sys_text
    assert "secret body" not in sys_text, "menu mode should NOT include full bodies"
    assert "load_skill" in sys_text
    print("  llm inject_skills menu: PASS")


def test_llm_inject_skills_full():
    """Non-progressive mode inlines the full skill body."""
    from brainchild.pipeline.llm import LLMClient
    from brainchild.pipeline.skills import Skill

    llm = LLMClient(MagicMock())
    skills = [Skill(name="x", description="d", body="FULL BODY TEXT")]
    llm.inject_skills(skills, progressive=False)
    assert "FULL BODY TEXT" in llm._system_prompt
    print("  llm inject_skills full: PASS")


def test_load_skill_tool_only_when_progressive_with_skills():
    """_tools_for_llm exposes load_skill iff progressive_disclosure AND skills loaded."""
    from brainchild.pipeline.chat_runner import ChatRunner, LOAD_SKILL_TOOL
    from brainchild.pipeline.skills import Skill

    # No skills — no load_skill tool (even when MCP is absent, we return None).
    runner = ChatRunner(connector=MagicMock(), llm=MagicMock(), mcp_client=None, skills=[])
    assert runner._tools_for_llm() is None

    # Skills + progressive on — load_skill appears.
    runner = ChatRunner(
        connector=MagicMock(), llm=MagicMock(), mcp_client=None,
        skills=[Skill(name="a", description="d", body="b")],
        skills_progressive=True,
    )
    tools = runner._tools_for_llm()
    assert tools and any(t["function"]["name"] == LOAD_SKILL_TOOL for t in tools)

    # Skills + progressive off — no load_skill.
    runner = ChatRunner(
        connector=MagicMock(), llm=MagicMock(), mcp_client=None,
        skills=[Skill(name="a", description="d", body="b")],
        skills_progressive=False,
    )
    tools = runner._tools_for_llm()
    assert tools is None or all(t["function"]["name"] != LOAD_SKILL_TOOL for t in tools)
    print("  load_skill tool gating: PASS")


def test_call_rate_sanity():
    """Stubbed LLM: across 6 chat turns (none slash-invoking a skill), no code path
    should count a slash-invoke. This is the regression hook for watching whether
    the model over-calls load_skill. Real call-rate is provider-driven — we only
    verify our local counters don't increment on unrelated messages here."""
    from brainchild.pipeline.chat_runner import ChatRunner
    from brainchild.pipeline.skills import Skill

    runner = ChatRunner(
        connector=MagicMock(), llm=MagicMock(), mcp_client=None,
        skills=[
            Skill(name="commit", description="Create a git commit.", body="..."),
            Skill(name="review", description="Review a PR.", body="..."),
        ],
        skills_progressive=True,
    )
    # Make the stubbed LLM return plain text (no tool_call).
    runner._llm.ask = MagicMock(return_value="ok")

    messages = [
        "hey how are you",
        "what's 2+2",
        "tell me a joke",
        "thanks",
        "what time is it",
        "bye",
    ]
    for m in messages:
        runner._handle_message(m)

    assert runner._load_skill_calls == 0, \
        f"non-slash chit-chat should not count as load_skill calls, got {runner._load_skill_calls}"
    assert runner._turn_count == len(messages)
    print("  call-rate sanity: PASS")


def test_slash_invocation_counts_as_load():
    from brainchild.pipeline.chat_runner import ChatRunner
    from brainchild.pipeline.skills import Skill

    runner = ChatRunner(
        connector=MagicMock(), llm=MagicMock(), mcp_client=None,
        skills=[Skill(name="commit", description="d", body="commit instructions")],
        skills_progressive=True,
    )
    runner._llm.ask = MagicMock(return_value="ok")

    runner._handle_message("/commit please")
    assert runner._load_skill_calls == 1
    assert runner._load_skill_by_name == {"commit": 1}
    # extra_system kwarg should carry the skill body.
    kwargs = runner._llm.ask.call_args.kwargs
    assert "commit instructions" in kwargs["extra_system"]

    # Unknown slash should be a pass-through, no counter bump.
    runner._handle_message("/unknown thing")
    assert runner._load_skill_calls == 1
    print("  slash-invocation counts: PASS")


# ---------------------------------------------------------------------------
# Gap-fill tests (session 133) — branches not covered by the tests above
# ---------------------------------------------------------------------------

def test_no_frontmatter_skipped(caplog_list):
    """File that doesn't start with '---' is skipped with a 'missing frontmatter' warning."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "no-fm"
        root.mkdir()
        (root / "SKILL.md").write_text("just body, no frontmatter at all\n")
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert skills == []
        assert any("missing frontmatter" in r.getMessage() for r in caplog_list)
    print("  no frontmatter skipped: PASS")


def test_unterminated_frontmatter_skipped(caplog_list):
    """'---' opens but is never closed → skipped with 'unterminated frontmatter' warning."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "unterminated"
        root.mkdir()
        (root / "SKILL.md").write_text("---\nname: x\ndescription: y\nbody goes here no closing\n")
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert skills == []
        assert any("unterminated frontmatter" in r.getMessage() for r in caplog_list)
    print("  unterminated frontmatter skipped: PASS")


def test_non_dict_frontmatter_skipped(caplog_list):
    """Frontmatter that parses as a list (or any non-mapping) is skipped."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "listy"
        root.mkdir()
        (root / "SKILL.md").write_text("---\n- name: x\n- description: y\n---\nbody\n")
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert skills == []
        assert any("not a mapping" in r.getMessage() for r in caplog_list)
    print("  non-dict frontmatter skipped: PASS")


def test_allowed_tools_string_parses_comma_split():
    """allowed-tools declared as a comma-separated string is split and loaded."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp) / "string-tools"
        _write_skill(
            root, "s", "Uses comma-string tools.",
            extra_fm="allowed-tools: 'load_skill, Bash, Edit'\n",
        )
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert len(skills) == 1
        assert skills[0].allowed_tools == ["load_skill", "Bash", "Edit"]
    print("  allowed-tools comma string: PASS")


def test_empty_parent_folder_warns(caplog_list):
    """Parent folder with no SKILL.md anywhere → warns 'no SKILL.md found' and returns []."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        parent = Path(tmp)
        (parent / "empty-subdir").mkdir()  # subdir with no SKILL.md
        (parent / "another").mkdir()
        skills = load_skills(None, external_paths=[str(parent)], shared_library_dir=Path(tmp) / "no-library")
        assert skills == []
    print("  empty parent folder yields nothing: PASS")


class _CapturingHandler(logging.Handler):
    def __init__(self):
        super().__init__()
        self.records = []
    def emit(self, record):
        self.records.append(record)


def _run_with_caplog(test_fn):
    """Run a test function that accepts a caplog_list (log records)."""
    handler = _CapturingHandler()
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    prev_level = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(handler)
    try:
        test_fn(handler.records)
    finally:
        root.removeHandler(handler)
        root.setLevel(prev_level)


# ---------------------------------------------------------------------------
# Phase 15.11 coverage — enabled filtering, external_paths validation,
# shared-library resolution.
# ---------------------------------------------------------------------------


def test_enabled_filters_to_named_subset():
    """enabled_names keeps only the named subset; non-matching discovered skills drop."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_skill(root / "a", "alpha", "A.")
        _write_skill(root / "b", "beta", "B.")
        _write_skill(root / "c", "gamma", "G.")
        skills = load_skills(["alpha", "gamma"], external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        names = [s.name for s in skills]
        assert names == ["alpha", "gamma"], names
    print("  enabled filters to subset: PASS")


def test_enabled_preserves_declared_order():
    """Output order matches enabled_names order, not filesystem order."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_skill(root / "a", "alpha", "A.")
        _write_skill(root / "b", "beta", "B.")
        skills = load_skills(["beta", "alpha"], external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert [s.name for s in skills] == ["beta", "alpha"]
    print("  enabled preserves order: PASS")


def test_enabled_none_returns_all(caplog_list):
    """None means 'return all discovered' — wizard scan mode."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_skill(root / "a", "alpha", "A.")
        _write_skill(root / "b", "beta", "B.")
        skills = load_skills(None, external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert sorted(s.name for s in skills) == ["alpha", "beta"]
    print("  enabled=None returns all: PASS")


def test_enabled_unknown_name_warns_and_drops(caplog_list):
    """Enabled name with no matching candidate WARNs once and is dropped."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _write_skill(root / "a", "alpha", "A.")
        skills = load_skills(["alpha", "mystery"], external_paths=[str(root)], shared_library_dir=Path(tmp) / "no-library")
        assert [s.name for s in skills] == ["alpha"]
        assert any("'mystery' not found" in r.getMessage() for r in caplog_list)
    print("  unknown enabled warns + drops: PASS")


def test_relative_external_path_warns_and_skips(caplog_list):
    """Relative external_paths entry is CWD-dependent → WARN + skip."""
    from brainchild.pipeline.skills import load_skills

    skills = load_skills(
        None,
        external_paths=["agents/pm/skills"],
        shared_library_dir=Path("/nonexistent-test-lib"),
    )
    # With no matching library + skipped relative entry, result is empty.
    assert skills == []
    assert any("not tilde-prefixed or absolute" in r.getMessage() for r in caplog_list)
    print("  relative external_path rejected: PASS")


def test_tilde_external_path_expands(caplog_list):
    """Tilde-prefixed entries expand to $HOME. Nonexistent ones still WARN."""
    from brainchild.pipeline.skills import load_skills

    # Use a guaranteed-nonexistent subpath so we test the expansion path
    # without polluting the user's real ~/.
    skills = load_skills(
        None,
        external_paths=["~/.brainchild-test-dir-that-does-not-exist-92837"],
        shared_library_dir=Path("/nonexistent-test-lib"),
    )
    assert skills == []
    # The warn should mention "not found", not "not absolute" — tilde is accepted.
    assert any("not found" in r.getMessage() for r in caplog_list)
    assert not any("not tilde-prefixed" in r.getMessage() for r in caplog_list)
    print("  tilde external_path expands: PASS")


def test_shared_library_scanned_first(caplog_list):
    """Shared library is scanned before external_paths; external_paths override."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "library"
        ext = Path(tmp) / "external"
        _write_skill(lib / "alpha", "alpha", "library version", body="LIB BODY")
        _write_skill(ext / "alpha", "alpha", "external version", body="EXT BODY")
        skills = load_skills(
            None,
            external_paths=[str(ext)],
            shared_library_dir=lib,
        )
        assert len(skills) == 1
        assert "EXT BODY" in skills[0].body, skills[0].body
        assert skills[0].description == "external version"
    print("  shared library then external_paths (last-wins): PASS")


def test_shared_library_skills_without_external():
    """Library-only skills are selectable via enabled_names."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        lib = Path(tmp) / "library"
        _write_skill(lib / "alpha", "alpha", "A.")
        _write_skill(lib / "beta", "beta", "B.")
        skills = load_skills(["alpha"], shared_library_dir=lib)
        assert [s.name for s in skills] == ["alpha"]
    print("  library-only resolution: PASS")


def test_project_scope_skills_walked_via_cwd():
    """`<cwd>/.claude/skills/` is walked implicitly so project-scoped Claude
    Code skills surface without needing a config edit. Mirrors the cwd-aware
    walk we do for project-scope MCPs.
    """
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        proj = Path(tmp)
        _write_skill(proj / ".claude" / "skills" / "proj-only", "proj-only", "Project skill.")
        skills = load_skills(
            None,
            external_paths=None,
            shared_library_dir=Path(tmp) / "no-library",
            cwd=proj,
        )
        names = [s.name for s in skills]
        assert names == ["proj-only"], names
    print("  project-scope skills walked via cwd: PASS")


def test_project_scope_skills_override_external_on_collision():
    """Project scope wins on name collision (last-wins, project comes after
    external in source order). Mirrors project-scope MCP precedence.
    """
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        ext = Path(tmp) / "external"
        proj = Path(tmp) / "project"
        _write_skill(ext / "shared", "shared", "External version.", body="EXT BODY")
        _write_skill(proj / ".claude" / "skills" / "shared", "shared", "Project version.", body="PROJ BODY")
        skills = load_skills(
            None,
            external_paths=[str(ext)],
            shared_library_dir=Path(tmp) / "no-library",
            cwd=proj,
        )
        assert len(skills) == 1
        assert skills[0].description == "Project version."
        assert "PROJ BODY" in skills[0].body
    print("  project-scope overrides external on collision: PASS")


def test_empty_external_paths_with_missing_library():
    """No library, no external_paths → empty result, no crash."""
    from brainchild.pipeline.skills import load_skills

    with tempfile.TemporaryDirectory() as tmp:
        fake_lib = Path(tmp) / "never-created"
        skills = load_skills(["anything"], shared_library_dir=fake_lib)
        assert skills == []
    print("  empty library + enabled = empty (no crash): PASS")


if __name__ == "__main__":
    print("Running skill tests...")
    test_happy_path_single_folder()
    test_parent_dir_scan()
    _run_with_caplog(test_missing_path_warns_no_crash)
    _run_with_caplog(test_malformed_yaml_skipped)
    _run_with_caplog(test_missing_fields_skipped)
    _run_with_caplog(test_duplicate_last_wins)
    test_deep_nesting_ignored()
    _run_with_caplog(test_allowed_tools_warn)
    test_llm_inject_skills_menu()
    test_llm_inject_skills_full()
    test_load_skill_tool_only_when_progressive_with_skills()
    test_call_rate_sanity()
    test_slash_invocation_counts_as_load()
    # Gap-fill (session 133)
    _run_with_caplog(test_no_frontmatter_skipped)
    _run_with_caplog(test_unterminated_frontmatter_skipped)
    _run_with_caplog(test_non_dict_frontmatter_skipped)
    test_allowed_tools_string_parses_comma_split()
    _run_with_caplog(test_empty_parent_folder_warns)
    # Phase 15.11 — library/external_paths/enabled model
    test_enabled_filters_to_named_subset()
    test_enabled_preserves_declared_order()
    _run_with_caplog(test_enabled_none_returns_all)
    _run_with_caplog(test_enabled_unknown_name_warns_and_drops)
    _run_with_caplog(test_relative_external_path_warns_and_skips)
    _run_with_caplog(test_tilde_external_path_expands)
    _run_with_caplog(test_shared_library_scanned_first)
    test_shared_library_skills_without_external()
    test_project_scope_skills_walked_via_cwd()
    test_project_scope_skills_override_external_on_collision()
    test_empty_external_paths_with_missing_library()
    print("\nAll skill tests passed.")
