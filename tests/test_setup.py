"""Tests for `operator build` wizard (pipeline/setup.py).

Covers the individual helpers rather than end-to-end prompting — the rich
prompts are easy to wire interactively but noisy to mock at scale, and the
interesting failure modes live in the helpers (skill copy, MCP flip,
atomic write, collision validation, edit-in-place swap).

Run: python tests/test_setup.py
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

# pipeline.setup does not from _1_800_operator import config.py, but some sibling imports might
# — set a safe default so tests never fail on missing OPERATOR_BOT.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator.pipeline import setup as wizard  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────


def _make_skill_folder(root: Path, name: str, desc: str = "test skill") -> Path:
    """Create root/<name>/SKILL.md with minimal valid frontmatter."""
    d = root / name
    d.mkdir(parents=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n\nbody\n",
        encoding="utf-8",
    )
    return d


# ── 1. Name validation (collision + reserved) ─────────────────────────────


def test_name_validation_reserved_and_collision():
    """_validate_name rejects reserved CLI verbs, bad chars, and existing bots."""
    # reserved
    ok, reason = wizard._validate_name("build")
    assert not ok and "reserved" in reason.lower(), reason
    ok, reason = wizard._validate_name("setup")  # legacy alias still reserved
    assert not ok and "reserved" in reason.lower(), reason
    ok, reason = wizard._validate_name("list")
    assert not ok and "reserved" in reason.lower(), reason

    # bad shape — starts with digit, too long, contains space, empty
    for bad in ("1bot", "A" * 33, "a b", "", "  "):
        ok, _ = wizard._validate_name(bad)
        assert not ok, f"expected rejection for {bad!r}"

    # collision — pm bot pre-seeded in a tmp agents dir
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "agents"
        (sandbox / "pm").mkdir(parents=True)
        (sandbox / "pm" / "config.yaml").write_text("agent: {name: pm}\n")
        original = wizard._AGENTS_DIR
        wizard._AGENTS_DIR = sandbox
        try:
            ok, reason = wizard._validate_name("pm")
            assert not ok and "already exists" in reason, reason

            # happy path — unoccupied name in same sandbox
            ok, reason = wizard._validate_name("brand-new-bot")
            assert ok, reason
        finally:
            wizard._AGENTS_DIR = original

    print("  name validation: PASS")


# ── 2. MCP enabled-flag round-trip ────────────────────────────────────────


def test_mcp_enabled_flip_round_trip():
    """Toggling a server's `enabled` flag survives yaml dump → load → config.py filter."""
    cfg = wizard._load_yaml(wizard._PM_CONFIG)
    servers = cfg["mcp_servers"]
    # Flip all servers: on→off and off→on.
    for name in servers:
        servers[name]["enabled"] = not bool(servers[name].get("enabled", False))
    expected_enabled = {n for n, s in servers.items() if s["enabled"]}

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "config.yaml"
        wizard._dump_yaml(cfg, out)
        roundtrip = wizard._load_yaml(out)

    # Simulate config.py's enabled filter.
    filtered = {
        n: s for n, s in roundtrip["mcp_servers"].items()
        if s.get("enabled", True)
    }
    assert set(filtered) == expected_enabled, (filtered, expected_enabled)
    print("  mcp enabled flip round-trip: PASS")


# ── 3. Skill copy — folder, single-file wrap, parent walk ─────────────────


# ── 4. Atomic write — rollback on build failure ──────────────────────────


def _make_state(name: str, mode: str = "edit", **overrides) -> "wizard.WizardState":
    """Build a minimally-valid WizardState for write/reveal tests."""
    bot_cfg = overrides.pop("bot_cfg", None) or {
        "agent": {"name": name.capitalize(), "trigger_phrase": "@operator"},
        "llm": {"provider": "anthropic", "model": "x"},
        "mcp_servers": {},
    }
    defaults = dict(
        mode=mode,
        name=name,
        display_name=name.capitalize(),
        tagline="",
        based_on="pm" if mode == "new" else name,
        portrait="placeholder",
        bot_cfg=bot_cfg,
        enabled_skill_names=[],
    )
    defaults.update(overrides)
    return wizard.WizardState(**defaults)


def test_atomic_write_rollback_on_build_failure():
    """If build fails mid-flight, the target dir is untouched and no tmpdir lingers.

    Forces a failure by monkey-patching `face.write_if_missing` to raise —
    it's called after the skills block is resolved but before the final
    rename into place, so the rollback path exercises cleanup fully.
    """
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "agents"
        sandbox.mkdir()
        original_agents_dir = wizard._AGENTS_DIR
        original_write_if_missing = wizard.face.write_if_missing
        wizard._AGENTS_DIR = sandbox

        def _boom(*args, **kwargs):
            raise RuntimeError("forced build failure")
        wizard.face.write_if_missing = _boom

        try:
            target = sandbox / "victim"
            target.mkdir()
            (target / "sentinel.txt").write_text("keep-me", encoding="utf-8")

            state = _make_state("victim", mode="edit")

            raised = False
            try:
                wizard._step7_write(state)
            except Exception:
                raised = True

            assert raised, "expected exception during build"
            assert (target / "sentinel.txt").read_text(encoding="utf-8") == "keep-me"
            stray = [p for p in sandbox.iterdir() if p.name.startswith(".victim.tmp-")]
            assert not stray, f"tempdir not cleaned up: {stray}"
            baks = [p for p in sandbox.iterdir() if p.name.startswith("victim.bak-")]
            assert not baks, f"backup left behind: {baks}"
        finally:
            wizard._AGENTS_DIR = original_agents_dir
            wizard.face.write_if_missing = original_write_if_missing
    print("  atomic write rollback: PASS")


# ── 5. Edit-in-place swap — .bak cleaned on success ──────────────────────


def test_edit_in_place_swap_cleans_backup():
    """Successful edit-in-place renames old dir to .bak-<ts>, then deletes .bak."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "agents"
        sandbox.mkdir()
        original_agents_dir = wizard._AGENTS_DIR
        wizard._AGENTS_DIR = sandbox
        try:
            target = sandbox / "preset"
            target.mkdir()
            (target / ".env.example").write_text("KEY=val", encoding="utf-8")
            (target / "README.md").write_text("# Hand-written\n", encoding="utf-8")
            (target / "skills").mkdir()
            (target / "skills" / "deselected").mkdir()

            state = _make_state("preset", mode="edit")
            out = wizard._step7_write(state)
            assert out == target
            assert (target / "config.yaml").is_file()
            assert (target / ".env.example").read_text(encoding="utf-8") == "KEY=val"
            assert "Hand-written" in (target / "README.md").read_text(encoding="utf-8")
            assert not (target / "skills" / "deselected").exists()
            baks = [p for p in sandbox.iterdir() if p.name.startswith("preset.bak-")]
            assert not baks, f"backup not cleaned: {baks}"
        finally:
            wizard._AGENTS_DIR = original_agents_dir
    print("  edit-in-place swap: PASS")


# ── 6. From-scratch write — new bundle created ────────────────────────────


def test_from_scratch_write_creates_bundle():
    """mode='new' with a free target writes a complete bundle."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "agents"
        sandbox.mkdir()
        original_agents_dir = wizard._AGENTS_DIR
        wizard._AGENTS_DIR = sandbox
        try:
            bot_cfg = {
                "agent": {"name": "Fresh", "trigger_phrase": "@operator", "tagline": "t"},
                "llm": {"provider": "anthropic", "model": "x"},
                "mcp_servers": {"notion": {"enabled": True, "command": "npx", "args": []}},
            }
            state = _make_state("fresh", mode="new", bot_cfg=bot_cfg)
            out = wizard._step7_write(state)
            assert out.is_dir()
            assert (out / "config.yaml").is_file()
            assert (out / "portrait.txt").is_file()
            assert (out / "README.md").is_file()
            loaded = wizard._load_yaml(out / "config.yaml")
            assert loaded["skills"]["enabled"] == []
            assert loaded["skills"]["external_paths"] == []
            assert "paths" not in loaded["skills"]
        finally:
            wizard._AGENTS_DIR = original_agents_dir
    print("  from-scratch write: PASS")


# ── 7. .env append — never overwrites existing keys ───────────────────────


def test_env_append_preserves_existing():
    """_append_env adds new keys to .env without overwriting existing ones."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text("EXISTING='already-set'\n", encoding="utf-8")

        wizard._append_env(env, {"NEW_KEY": "new-value"})

        parsed = wizard._parse_env(env)
        assert parsed["EXISTING"] == "already-set"
        assert parsed["NEW_KEY"] == "new-value"
    print("  env append preserves existing: PASS")


# ── 8. WizardState — equipped views + card render ────────────────────────


def test_wizard_state_equipped_views():
    """equipped_mcps reflects `enabled` flags; equipped_skills reflects
    state.enabled_skill_names (the Phase 15.11 single source of truth)."""
    cfg = {
        "mcp_servers": {
            "linear": {"enabled": True},
            "notion": {"enabled": False},
            "github": {"enabled": True},
        },
    }
    state = wizard.WizardState(
        mode="new",
        name="researcher",
        display_name="Researcher",
        tagline="t",
        based_on="pm",
        portrait="placeholder",
        bot_cfg=cfg,
        enabled_skill_names=["alpha", "beta", "gamma"],
    )
    assert state.equipped_mcps() == ["linear", "github"]
    assert state.equipped_skills() == ["alpha", "beta", "gamma"]
    # Card renders without raising; smoke test.
    assert state.card() is not None
    assert state.card(mcps=["only-this"]) is not None
    print("  wizard state equipped views: PASS")


# ── 10. Reveal — placeholder swaps for real portrait ─────────────────────


def test_reveal_swaps_portrait():
    """_reveal mutates state.portrait from placeholder to the real face."""
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "agents"
        sandbox.mkdir()
        original_agents_dir = wizard._AGENTS_DIR
        wizard._AGENTS_DIR = sandbox
        try:
            # Pre-create the portrait file that _reveal will read.
            target = sandbox / "fresh"
            target.mkdir()
            from _1_800_operator.pipeline import face
            (target / "portrait.txt").write_text(face.render("fresh") + "\n", encoding="utf-8")

            from _1_800_operator.pipeline import build_card
            state = _make_state("fresh", mode="new")
            assert state.portrait == "placeholder"
            state.portrait = build_card.PLACEHOLDER_PORTRAIT
            wizard._reveal(state)
            assert state.portrait != build_card.PLACEHOLDER_PORTRAIT
            assert "█" in state.portrait, "expected real face glyphs after reveal"
        finally:
            wizard._AGENTS_DIR = original_agents_dir
    print("  reveal swaps portrait: PASS")


# ── 11. Picker — driven by injected key_source ───────────────────────────


def test_picker_select_one_with_key_source():
    """select_one navigates with UP/DOWN and returns the chosen Choice."""
    import readchar
    from _1_800_operator.pipeline.picker import Choice, select_one
    choices = [Choice(label=f"item{i}", value=i) for i in range(3)]
    keys = [readchar.key.DOWN, readchar.key.DOWN, readchar.key.ENTER]
    picked = select_one("pick", choices, key_source=keys)
    assert picked.value == 2
    print("  picker select_one: PASS")


def test_picker_select_many_with_key_source():
    """select_many navigates with UP/DOWN, toggles with SPACE, confirms with ENTER."""
    import readchar
    from _1_800_operator.pipeline.picker import Choice, select_many
    choices = [Choice(label=f"item{i}") for i in range(3)]
    # Start at 0, toggle item0 on, move down, toggle item1 on, confirm.
    keys = [
        " ",
        readchar.key.DOWN,
        " ",
        readchar.key.ENTER,
    ]
    out = select_many("pick", choices, key_source=keys)
    assert out == [True, True, False]
    print("  picker select_many: PASS")


def test_picker_cancels_on_q():
    """select_one raises PickerCancelled when 'q' is pressed."""
    from _1_800_operator.pipeline.picker import Choice, PickerCancelled, select_one
    choices = [Choice(label="x")]
    raised = False
    try:
        select_one("pick", choices, key_source=["q"])
    except PickerCancelled:
        raised = True
    assert raised, "expected PickerCancelled on 'q'"
    print("  picker cancels on q: PASS")


# ── 12. _parse_env edge cases (S1) ────────────────────────────────────────


def test_parse_env_strips_quotes():
    """Both single- and double-quoted values are unwrapped to the raw value."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text(
            "DOUBLE=\"value-1\"\n"
            "SINGLE='value-2'\n"
            "BARE=value-3\n",
            encoding="utf-8",
        )
        parsed = wizard._parse_env(env)
        assert parsed == {"DOUBLE": "value-1", "SINGLE": "value-2", "BARE": "value-3"}, parsed
    print("  parse_env strips quotes: PASS")


def test_parse_env_skips_comments_and_blanks():
    """`#` comments and blank lines are ignored without error."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text(
            "# leading comment\n"
            "\n"
            "KEY_A=a\n"
            "   \n"
            "# trailing comment\n"
            "KEY_B=b\n",
            encoding="utf-8",
        )
        parsed = wizard._parse_env(env)
        assert parsed == {"KEY_A": "a", "KEY_B": "b"}, parsed
    print("  parse_env skips comments + blanks: PASS")


def test_parse_env_tolerates_malformed_line():
    """A line without `=` is silently dropped — does not raise."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / ".env"
        env.write_text(
            "NOT_AN_ASSIGNMENT\n"
            "VALID=ok\n",
            encoding="utf-8",
        )
        parsed = wizard._parse_env(env)
        assert parsed == {"VALID": "ok"}, parsed
    print("  parse_env tolerates malformed: PASS")


# ── 13. _append_env creates missing file (S2) ─────────────────────────────


def test_append_env_creates_missing_file():
    """Non-existent .env is created with the new values appended."""
    with tempfile.TemporaryDirectory() as tmp:
        env = Path(tmp) / "new.env"
        assert not env.exists()
        wizard._append_env(env, {"FRESH_KEY": "fresh-value"})
        assert env.is_file()
        parsed = wizard._parse_env(env)
        assert parsed == {"FRESH_KEY": "fresh-value"}, parsed
    print("  append_env creates missing file: PASS")


# ── 14. _collect_env_refs (S3) ────────────────────────────────────────────


def test_collect_env_refs_from_enabled_servers():
    """${VAR} refs are gathered from enabled servers' env dict."""
    cfg = {
        "mcp_servers": {
            "linear": {
                "enabled": True,
                "env": {"LINEAR_API_KEY": "${LINEAR_API_KEY}"},
            },
            "github": {
                "enabled": True,
                "env": {"GITHUB_TOKEN": "${GITHUB_TOKEN}", "OTHER": "literal"},
            },
        },
    }
    state = wizard.WizardState(
        mode="new", name="x", display_name="X", tagline="", based_on="pm",
        portrait="placeholder", bot_cfg=cfg, enabled_skill_names=[],
    )
    refs = wizard._collect_env_refs(state)
    assert refs == {"LINEAR_API_KEY", "GITHUB_TOKEN"}, refs
    print("  collect_env_refs enabled only: PASS")


def test_collect_env_refs_skips_disabled_servers():
    """Refs from disabled servers are NOT collected — we don't prompt for keys
    the wizard is turning off."""
    cfg = {
        "mcp_servers": {
            "on-server": {
                "enabled": True,
                "env": {"KEEP_THIS": "${KEEP_THIS}"},
            },
            "off-server": {
                "enabled": False,
                "env": {"DROP_THIS": "${DROP_THIS}"},
            },
        },
    }
    state = wizard.WizardState(
        mode="new", name="x", display_name="X", tagline="", based_on="pm",
        portrait="placeholder", bot_cfg=cfg, enabled_skill_names=[],
    )
    refs = wizard._collect_env_refs(state)
    assert refs == {"KEEP_THIS"}, refs
    print("  collect_env_refs skips disabled: PASS")


def test_collect_env_refs_empty_when_no_mcps():
    """A bot with no mcp_servers section yields the empty set."""
    state = wizard.WizardState(
        mode="new", name="x", display_name="X", tagline="", based_on="pm",
        portrait="placeholder", bot_cfg={}, enabled_skill_names=[],
    )
    assert wizard._collect_env_refs(state) == set()
    print("  collect_env_refs empty when no mcps: PASS")


# ── 16. build_card.render + _wrap_cells (S5) ──────────────────────────────


def test_build_card_render_panel_mode():
    """render returns a plain white Panel with the given title and icon colorization."""
    from _1_800_operator.pipeline import build_card
    from rich.panel import Panel

    out = build_card.render(
        name="Pm",
        tagline="task wrangler",
        portrait=build_card.PLACEHOLDER_PORTRAIT,
        power_ups=["linear", "github"],
        skills=["review"],
        title="Your build",
    )
    assert isinstance(out, Panel), f"expected Panel, got {type(out).__name__}"
    assert out.title == "Your build"
    # Render body content to a string so we can sanity-check the fields landed.
    body_plain = out.renderable.plain if hasattr(out.renderable, "plain") else str(out.renderable)
    assert "Pm" in body_plain
    assert "task wrangler" in body_plain
    assert "linear" in body_plain and "github" in body_plain
    assert "review" in body_plain
    # ⚡ and ★ glyphs survive markup parsing into the plain form.
    assert "⚡" in body_plain and "★" in body_plain, body_plain
    print("  build_card render Panel mode: PASS")


def test_build_card_empty_bullets_show_placeholder():
    """Empty power_ups/skills render as the '—' placeholder in the body rows."""
    from _1_800_operator.pipeline import build_card

    rows = build_card._compose_body(
        name="X", tagline="t", portrait=build_card.PLACEHOLDER_PORTRAIT,
        power_ups=[], skills=[],
        inner=build_card.DEFAULT_WIDTH - 2,
    )
    joined = "\n".join(rows)
    assert "MCPs:       —" in joined, f"expected em-dash for empty MCPs, got: {joined!r}"
    assert "skills:     —" in joined, f"expected em-dash for empty skills, got: {joined!r}"
    print("  build_card empty bullets show em-dash: PASS")


def test_wrap_cells_hard_splits_oversized_token():
    """A single token wider than the width is split on code-point boundaries,
    not dropped or overflowing."""
    from _1_800_operator.pipeline import build_card
    long_word = "x" * 25
    out = build_card._wrap_cells(long_word, 10)
    # All rows ≤ 10 cells; concatenation reconstructs the input.
    assert all(len(r) <= 10 for r in out), out
    assert "".join(out) == long_word
    # Normal case: short-enough text returns as-is.
    assert build_card._wrap_cells("hi", 10) == ["hi"]
    print("  wrap_cells hard-splits wide token: PASS")


# ── _maybe_reset_to_bundled — `operator build` reset-with-backup ──────


def _seed_reset_fixture(tmp: Path, name: str = "claude") -> tuple[Path, Path]:
    """Build minimal user-scope + bundled cfg paths for reset tests.
    Returns (cfg_path, bundled_cfg)."""
    user_dir = tmp / "user_agents" / name
    bundled_dir = tmp / "bundled_agents" / name
    user_dir.mkdir(parents=True)
    bundled_dir.mkdir(parents=True)
    cfg_path = user_dir / "config.yaml"
    bundled_cfg = bundled_dir / "config.yaml"
    return cfg_path, bundled_cfg


def test_reset_to_bundled_skips_when_user_scope_missing():
    """No user-scope config → nothing to back up, return None silently."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path, bundled_cfg = _seed_reset_fixture(Path(tmp))
        bundled_cfg.write_text("name: claude\n")
        # cfg_path intentionally missing
        result = wizard._maybe_reset_to_bundled("claude", cfg_path)
        assert result is None
        assert not cfg_path.exists()


def test_reset_to_bundled_skips_when_user_scope_identical_to_bundled():
    """Pristine user-scope (== bundled) → no prompt, no backup."""
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path, bundled_cfg = _seed_reset_fixture(Path(tmp))
        bundled_cfg.write_text("name: claude\nfoo: bar\n")
        cfg_path.write_text("name: claude\nfoo: bar\n")
        # Patch the module-level bundled dir to point at our fixture.
        orig = wizard._BUNDLED_AGENTS_DIR
        wizard._BUNDLED_AGENTS_DIR = bundled_cfg.parents[1]
        try:
            result = wizard._maybe_reset_to_bundled("claude", cfg_path)
        finally:
            wizard._BUNDLED_AGENTS_DIR = orig
        assert result is None
        # cfg_path untouched
        assert cfg_path.read_text() == "name: claude\nfoo: bar\n"
        # No backup files in the user dir
        assert list(cfg_path.parent.glob("*.bak.*")) == []


def test_reset_to_bundled_writes_backup_and_replaces_when_confirmed():
    """User edited config + says 'y' → backup written, cfg replaced with
    bundled, backup path returned. Other agents' configs are never read."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path, bundled_cfg = _seed_reset_fixture(Path(tmp))
        bundled_cfg.write_text("name: claude\npersonality: bundled\n")
        cfg_path.write_text("name: claude\npersonality: USER EDITED\n")
        # Decoy second agent — must not be touched
        decoy = cfg_path.parent.parent / "pm" / "config.yaml"
        decoy.parent.mkdir()
        decoy.write_text("name: pm\npersonality: pm-rules\n")
        decoy_before = decoy.read_text()

        orig = wizard._BUNDLED_AGENTS_DIR
        wizard._BUNDLED_AGENTS_DIR = bundled_cfg.parents[1]
        try:
            with patch.object(wizard.Prompt, "ask", return_value="y"):
                result = wizard._maybe_reset_to_bundled("claude", cfg_path)
        finally:
            wizard._BUNDLED_AGENTS_DIR = orig

        assert result is not None
        assert result.exists()
        assert "USER EDITED" in result.read_text()  # backup carries pre-reset
        assert cfg_path.read_text() == "name: claude\npersonality: bundled\n"
        # Decoy agent untouched
        assert decoy.read_text() == decoy_before
        assert not list(decoy.parent.glob("*.bak.*"))


def test_edit_preset_skips_reset_when_reset_allowed_false():
    """`operator edit <name>` passes reset_allowed=False so users get
    surgical access to the wizard without the destructive gate firing.
    `_edit_preset(name, reset_allowed=False)` should never call
    `_maybe_reset_to_bundled` even when user-scope differs from bundled."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp)
        # Set up an agent that DIFFERS from bundled — the trigger condition
        # for the reset prompt under reset_allowed=True.
        agents_dir = sandbox / "user_agents"
        bundled_dir = sandbox / "bundled_agents"
        (agents_dir / "claude").mkdir(parents=True)
        (bundled_dir / "claude").mkdir(parents=True)
        cfg_path = agents_dir / "claude" / "config.yaml"
        # Minimal valid yaml the wizard can actually load — must include
        # agent + llm or _load_yaml/portrait machinery downstream may break.
        cfg_path.write_text(
            "agent:\n  name: claude\nllm:\n  provider: claude_cli\n"
            "personality: USER EDITED\n"
        )
        (bundled_dir / "claude" / "config.yaml").write_text(
            "agent:\n  name: claude\nllm:\n  provider: claude_cli\n"
            "personality: bundled\n"
        )

        orig_user = wizard._AGENTS_DIR
        orig_bundled = wizard._BUNDLED_AGENTS_DIR
        wizard._AGENTS_DIR = agents_dir
        wizard._BUNDLED_AGENTS_DIR = bundled_dir
        try:
            # If _maybe_reset_to_bundled were called, it would prompt — patch
            # to assert it never runs.
            with patch.object(
                wizard, "_maybe_reset_to_bundled",
                side_effect=AssertionError("must not be called in edit mode"),
            ), patch.object(
                wizard.face, "load_or_render", return_value="portrait"
            ), patch.object(
                wizard, "_auto_import_claude_setup", return_value=None
            ):
                state = wizard._edit_preset("claude", reset_allowed=False)
            assert state.bot_cfg["personality"] == "USER EDITED", \
                "edit mode must preserve current state, not reset"
        finally:
            wizard._AGENTS_DIR = orig_user
            wizard._BUNDLED_AGENTS_DIR = orig_bundled


def test_run_target_agent_unknown_returns_1():
    """`operator edit <missing>` should fail-fast with a clear message
    rather than dropping into the picker."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        sandbox = Path(tmp) / "agents"
        sandbox.mkdir()
        # No bots seeded → `target_agent="missing"` not in _existing_bots()
        orig = wizard._AGENTS_DIR
        wizard._AGENTS_DIR = sandbox
        try:
            with patch.object(wizard, "_step1_fighter_select",
                              side_effect=AssertionError("must not run picker")):
                rc = wizard.run([], target_agent="missing", reset_allowed=False)
            assert rc == 1
        finally:
            wizard._AGENTS_DIR = orig


def test_reset_to_bundled_raises_wizard_cancel_on_decline():
    """User says 'n' → WizardCancel raised, no backup, no replacement."""
    from unittest.mock import patch
    with tempfile.TemporaryDirectory() as tmp:
        cfg_path, bundled_cfg = _seed_reset_fixture(Path(tmp))
        bundled_cfg.write_text("name: claude\nbundled: 1\n")
        cfg_path.write_text("name: claude\nuser: 1\n")
        cfg_before = cfg_path.read_text()

        orig = wizard._BUNDLED_AGENTS_DIR
        wizard._BUNDLED_AGENTS_DIR = bundled_cfg.parents[1]
        try:
            with patch.object(wizard.Prompt, "ask", return_value="n"):
                try:
                    wizard._maybe_reset_to_bundled("claude", cfg_path)
                    assert False, "expected WizardCancel"
                except wizard.WizardCancel as e:
                    assert "edit" in str(e).lower(), str(e)
        finally:
            wizard._BUNDLED_AGENTS_DIR = orig

        # Nothing changed on decline
        assert cfg_path.read_text() == cfg_before
        assert not list(cfg_path.parent.glob("*.bak.*"))


if __name__ == "__main__":
    print("Setup wizard tests:")
    test_name_validation_reserved_and_collision()
    test_mcp_enabled_flip_round_trip()
    test_atomic_write_rollback_on_build_failure()
    test_edit_in_place_swap_cleans_backup()
    test_from_scratch_write_creates_bundle()
    test_env_append_preserves_existing()
    test_wizard_state_equipped_views()
    test_reveal_swaps_portrait()
    test_picker_select_one_with_key_source()
    test_picker_select_many_with_key_source()
    test_picker_cancels_on_q()
    # Session 134 gap-fill
    test_parse_env_strips_quotes()
    test_parse_env_skips_comments_and_blanks()
    test_parse_env_tolerates_malformed_line()
    test_append_env_creates_missing_file()
    test_collect_env_refs_from_enabled_servers()
    test_collect_env_refs_skips_disabled_servers()
    test_collect_env_refs_empty_when_no_mcps()
    test_build_card_render_panel_mode()
    test_build_card_empty_bullets_show_placeholder()
    test_wrap_cells_hard_splits_oversized_token()
    test_reset_to_bundled_skips_when_user_scope_missing()
    test_reset_to_bundled_skips_when_user_scope_identical_to_bundled()
    test_reset_to_bundled_writes_backup_and_replaces_when_confirmed()
    test_edit_preset_skips_reset_when_reset_allowed_false()
    test_run_target_agent_unknown_returns_1()
    test_reset_to_bundled_raises_wizard_cancel_on_decline()
    print("\nAll tests passed.")
