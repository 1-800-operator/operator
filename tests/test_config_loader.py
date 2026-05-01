"""
Unit tests for Component A — Config loader (Boundary depth).

Covers `config.py`'s behavior at import time:
  1. Missing OPERATOR_BOT → SystemExit(2)
  2. Unknown bot name → SystemExit(2) listing available bots
  3. Happy-path yaml → agent/llm/skills/transcript fields parse with defaults
  4. SYSTEM_PROMPT = personality + "\\n\\n" + ground_rules; absent blocks drop out
  5. `agent.intro_on_join` — defaults to True when absent, honored when present
  6. MCP servers — `enabled: false` filtered, `tool_timeout_seconds` override preserved,
     `${VAR}` env resolution against os.environ

Approach: copy `config.py` into a tmp dir beside a tmp `agents/<bot>/config.yaml`,
then load fresh via importlib. That way we can exercise the loader with arbitrary
yaml without touching the real `agents/` tree.

Run:
    source venv/bin/activate
    python tests/test_config_loader.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import importlib.util
import shutil
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG_PY = REPO_ROOT / "src" / "_1_800_operator" / "config.py"


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def load_config(yaml_text: str, bot: str = "testbot", env: dict | None = None,
                extra_bots: list[str] | None = None):
    """Load config.py fresh against a tmp ~/.operator/agents/<bot>/config.yaml.

    Redirects HOME to a tmp dir so `Path.home() / ".operator" / "agents"` in
    the loader resolves inside the sandbox — the real user dir stays untouched.

    Returns (module, exc) — module is the loaded config module (or None if
    import raised SystemExit), exc is the SystemExit code (or None).
    """
    tmp = Path(tempfile.mkdtemp())
    try:
        agents_root = tmp / ".operator" / "agents"
        (agents_root / bot).mkdir(parents=True)
        (agents_root / bot / "config.yaml").write_text(yaml_text)
        for extra in (extra_bots or []):
            (agents_root / extra).mkdir(parents=True)
            (agents_root / extra / "config.yaml").write_text("agent: {name: x}\nllm: {provider: openai, model: m}")

        full_env = dict(env or {})
        saved = {k: os.environ.get(k) for k in list(full_env.keys()) + ["OPERATOR_BOT", "HOME"]}
        try:
            if "OPERATOR_BOT" not in full_env:
                full_env["OPERATOR_BOT"] = bot
            full_env["HOME"] = str(tmp)
            for k, v in full_env.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

            spec = importlib.util.spec_from_file_location(f"config_test_{id(tmp)}", REAL_CONFIG_PY)
            module = importlib.util.module_from_spec(spec)
            try:
                spec.loader.exec_module(module)
                return module, None
            except SystemExit as e:
                return None, e.code
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(tmp)


MIN_YAML = """\
agent:
  name: Test Bot
llm:
  provider: openai
  model: gpt-4o-mini
"""


# ---------------------------------------------------------------------------
# Test 1: missing OPERATOR_BOT → SystemExit(2)
# ---------------------------------------------------------------------------

def test_missing_operator_bot_exits():
    """Loading config without OPERATOR_BOT set must exit with code 2."""
    _, code = load_config(MIN_YAML, env={"OPERATOR_BOT": ""})
    assert code == 2, f"Expected SystemExit(2), got {code}"
    print("PASS  test_missing_operator_bot_exits")


# ---------------------------------------------------------------------------
# Test 2: unknown bot → SystemExit(2)
# ---------------------------------------------------------------------------

def test_unknown_bot_exits():
    """OPERATOR_BOT=<nonexistent> exits with code 2."""
    _, code = load_config(MIN_YAML, bot="realbot",
                          env={"OPERATOR_BOT": "ghost"},
                          extra_bots=["other"])
    assert code == 2, f"Expected SystemExit(2), got {code}"
    print("PASS  test_unknown_bot_exits")


# ---------------------------------------------------------------------------
# Test 3: happy path — fields parse, defaults applied
# ---------------------------------------------------------------------------

def test_happy_path_parses_fields():
    """Minimal yaml populates agent/llm fields; optional fields get defaults."""
    mod, _ = load_config(MIN_YAML)
    assert mod.AGENT_NAME == "Test Bot"
    assert mod.TRIGGER_PHRASE == "@operator"       # default
    assert mod.FIRST_CONTACT_HINT == ""             # default
    assert mod.AGENT_TAGLINE == ""                  # default
    assert mod.LLM_PROVIDER == "openai"
    assert mod.LLM_MODEL == "gpt-4o-mini"
    assert mod.HISTORY_MESSAGES == 40               # default
    assert mod.CAPTIONS_ENABLED is False            # default
    assert mod.SKILLS_ENABLED == []                 # default
    assert mod.SKILLS_EXTERNAL_PATHS == []           # default
    assert mod.SKILLS_PROGRESSIVE_DISCLOSURE is True  # default
    assert mod.MCP_SERVERS == {}                    # no servers configured
    # Internal tuning constants are always present
    assert mod.TOOL_TIMEOUT_SECONDS == 60
    print("PASS  test_happy_path_parses_fields")


# ---------------------------------------------------------------------------
# Test 4: SYSTEM_PROMPT composition
# ---------------------------------------------------------------------------

def test_claude_md_imports_mounted_into_system_prompt():
    """`claude_md_imports` paths get read fresh at config-load and appended
    to SYSTEM_PROMPT after personality + ground_rules. Missing files
    warn-and-skip; absent block is a silent no-op."""
    # Set up a fixture CLAUDE.md under tmp HOME so ~/... resolves to it.
    tmp = Path(tempfile.mkdtemp())
    try:
        (tmp / ".claude").mkdir()
        (tmp / ".claude" / "CLAUDE.md").write_text("USER RULE\n")
        (tmp / "proj").mkdir()
        (tmp / "proj" / "CLAUDE.md").write_text("PROJECT RULE\n")

        # Multi-source mounts both with section headers.
        yaml_text = (
            MIN_YAML
            + "\npersonality: 'be nice'\n"
            + "ground_rules: 'no lies'\n"
            + "claude_md_imports:\n"
            + "  - ~/.claude/CLAUDE.md\n"
            + "  - ./CLAUDE.md\n"
        )
        # Pin cwd to the proj dir so `./CLAUDE.md` resolves there.
        prev_cwd = os.getcwd()
        os.chdir(tmp / "proj")
        try:
            agents_root = tmp / ".operator" / "agents"
            (agents_root / "testbot").mkdir(parents=True)
            (agents_root / "testbot" / "config.yaml").write_text(yaml_text)
            saved = {k: os.environ.get(k) for k in ("OPERATOR_BOT", "HOME")}
            try:
                os.environ["OPERATOR_BOT"] = "testbot"
                os.environ["HOME"] = str(tmp)
                spec = importlib.util.spec_from_file_location(
                    f"config_test_md_{id(tmp)}", REAL_CONFIG_PY
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v
        finally:
            os.chdir(prev_cwd)

        sp = module.SYSTEM_PROMPT
        # personality + ground_rules + claude_md block, in that order
        assert sp.startswith("be nice\n\nno lies\n\n"), repr(sp[:100])
        assert "# CLAUDE.md — ~/.claude/CLAUDE.md" in sp
        assert "USER RULE" in sp
        assert "# CLAUDE.md — ./CLAUDE.md" in sp
        assert "PROJECT RULE" in sp
        assert module.CLAUDE_MD_IMPORTS_RAW == [
            "~/.claude/CLAUDE.md", "./CLAUDE.md",
        ]
    finally:
        shutil.rmtree(tmp)
    print("PASS  test_claude_md_imports_mounted_into_system_prompt")


def test_claude_md_imports_missing_paths_skipped_silently():
    """Stored path that doesn't resolve to a file is skipped (warn to
    stderr, not an error). Common when configs travel cross-machine."""
    yaml_text = (
        MIN_YAML
        + "\npersonality: 'p'\n"
        + "claude_md_imports:\n"
        + "  - ~/.this/does/not/exist.md\n"
    )
    mod, exc = load_config(yaml_text)
    assert exc is None, f"unexpected exit: {exc}"
    assert mod.SYSTEM_PROMPT == "p"
    assert mod.CLAUDE_MD_BLOCK == ""
    print("PASS  test_claude_md_imports_missing_paths_skipped_silently")


def test_system_prompt_composition():
    """personality first, ground_rules last, joined with blank line; missing blocks drop out."""
    # Both present
    yaml_both = MIN_YAML + "\npersonality: |\n  You are friendly.\nground_rules: |\n  Never lie.\n"
    mod, _ = load_config(yaml_both)
    assert mod.SYSTEM_PROMPT == "You are friendly.\n\nNever lie.", \
        f"Bad composition: {mod.SYSTEM_PROMPT!r}"
    assert mod.PERSONALITY == "You are friendly."
    assert mod.GROUND_RULES == "Never lie."

    # Only personality
    mod, _ = load_config(MIN_YAML + "\npersonality: 'only me'\n")
    assert mod.SYSTEM_PROMPT == "only me"
    assert mod.GROUND_RULES == ""

    # Only ground_rules
    mod, _ = load_config(MIN_YAML + "\nground_rules: 'rules only'\n")
    assert mod.SYSTEM_PROMPT == "rules only"
    assert mod.PERSONALITY == ""

    # Neither present
    mod, _ = load_config(MIN_YAML)
    assert mod.SYSTEM_PROMPT == ""
    print("PASS  test_system_prompt_composition")


# ---------------------------------------------------------------------------
# Test 5: intro_on_join default + override
# ---------------------------------------------------------------------------

def test_intro_on_join_default_and_override():
    """Defaults to True when absent; honors explicit False/True."""
    def with_intro(value: str) -> str:
        return MIN_YAML.replace("  name: Test Bot",
                                f"  name: Test Bot\n  intro_on_join: {value}")

    # Absent → True
    mod, _ = load_config(MIN_YAML)
    assert mod.INTRO_ON_JOIN is True

    # Explicit False
    mod, _ = load_config(with_intro("false"))
    assert mod.INTRO_ON_JOIN is False

    # Explicit True
    mod, _ = load_config(with_intro("true"))
    assert mod.INTRO_ON_JOIN is True
    print("PASS  test_intro_on_join_default_and_override")


# ---------------------------------------------------------------------------
# Test 6: MCP servers — enabled filter, timeout override, env resolution
# ---------------------------------------------------------------------------

def test_mcp_servers_filter_and_overrides():
    """Disabled servers dropped; tool_timeout_seconds carried through; ${VAR} resolved from env."""
    yaml_text = MIN_YAML + """
mcp_servers:
  active:
    command: /bin/echo
    args: ["hi"]
    env:
      TOKEN: ${FAKE_TOKEN_VAR}
      STATIC: literal
    hints: "use me"
    read_tools: [list_stuff, get_stuff]
    confirm_tools: [delete_stuff]
    tool_timeout_seconds: 300
  dormant:
    command: /bin/true
    enabled: false
"""
    mod, _ = load_config(yaml_text, env={"FAKE_TOKEN_VAR": "resolved-secret"})

    # Disabled server filtered out
    assert "dormant" not in mod.MCP_SERVERS
    assert "active" in mod.MCP_SERVERS

    active = mod.MCP_SERVERS["active"]
    assert active["command"] == "/bin/echo"
    assert active["args"] == ["hi"]
    # Env var resolution
    assert active["env"]["TOKEN"] == "resolved-secret"
    assert active["env"]["STATIC"] == "literal"
    # Hints, read/confirm tools
    assert active["hints"] == "use me"
    assert active["read_tools"] == {"list_stuff", "get_stuff"}
    assert active["confirm_tools"] == {"delete_stuff"}
    # Per-server timeout override preserved
    assert active["tool_timeout_seconds"] == 300
    print("PASS  test_mcp_servers_filter_and_overrides")


def test_relativize_home_renders_tilde():
    """relativize_home replaces $HOME prefix with `~`, leaves other paths alone."""
    mod, _ = load_config(MIN_YAML)
    home = os.path.expanduser("~")
    assert mod.relativize_home(home) == "~"
    assert mod.relativize_home(home + "/code/operator") == "~/code/operator"
    assert mod.relativize_home("/var/log/syslog") == "/var/log/syslog"
    assert mod.relativize_home("") == ""
    assert mod.relativize_home(None) is None
    # Partial match (same prefix but different dir) must NOT be tilde-swapped
    fake = home + "extrasuffix/file"
    assert mod.relativize_home(fake) == fake
    print("PASS  test_relativize_home_renders_tilde")


def test_mcp_env_strips_unsafe_keys():
    """PATH, PYTHONPATH, LD_*, DYLD_* in a server env block must be dropped, not passed through."""
    yaml_text = MIN_YAML + """
mcp_servers:
  hostile:
    command: /bin/echo
    env:
      PATH: /tmp/attacker
      PYTHONPATH: /tmp/attacker
      LD_PRELOAD: /tmp/evil.so
      DYLD_INSERT_LIBRARIES: /tmp/evil.dylib
      path: /tmp/case
      SAFE_TOKEN: keep-me
"""
    mod, _ = load_config(yaml_text)
    env = mod.MCP_SERVERS["hostile"]["env"]
    # All dangerous keys stripped (case-insensitive)
    for banned in ("PATH", "PYTHONPATH", "LD_PRELOAD", "DYLD_INSERT_LIBRARIES", "path"):
        assert banned not in env, f"{banned!r} should have been stripped, got env={env}"
    # Safe keys survive
    assert env.get("SAFE_TOKEN") == "keep-me"
    print("PASS  test_mcp_env_strips_unsafe_keys")


# ---------------------------------------------------------------------------
# Skills — new shape (Phase 15.11) and legacy-paths migration
# ---------------------------------------------------------------------------


def test_skills_new_shape_parses():
    """`skills.enabled + external_paths` parse directly, no migration."""
    yaml_text = MIN_YAML + """
skills:
  enabled: [alpha, beta]
  external_paths:
    - ~/my-skills
  progressive_disclosure: false
"""
    mod, _ = load_config(yaml_text)
    assert mod.SKILLS_ENABLED == ["alpha", "beta"]
    assert mod.SKILLS_EXTERNAL_PATHS == ["~/my-skills"]
    assert mod.SKILLS_PROGRESSIVE_DISCLOSURE is False
    print("PASS  test_skills_new_shape_parses")


def test_skills_legacy_paths_migrates_in_memory():
    """Legacy `skills.paths` translates to enabled + external_paths at load time.

    Only tilde/absolute entries survive as external_paths; relative ones drop.
    Enabled names are derived by scanning the legacy paths. This test writes
    a SKILL.md into a tilde-expanded path so the derivation has something to
    pick up — the load_config helper already redirects HOME to a tmp dir, so
    '~/seed-skill' resolves safely inside the sandbox.
    """
    # We'll need to seed a skill dir that's tilde-reachable. Because
    # load_config redirects HOME inside the sandbox, we plant the skill
    # there by pre-creating it under the sandboxed HOME.
    # Roll our own minimal loader to avoid retrofitting load_config.
    import importlib.util
    import shutil as _shutil
    import tempfile as _tempfile

    tmp = Path(_tempfile.mkdtemp())
    try:
        # Seed sandboxed HOME + agent config + a tilde-reachable skill.
        agents_root = tmp / ".operator" / "agents" / "testbot"
        agents_root.mkdir(parents=True)
        agents_root.joinpath("config.yaml").write_text(MIN_YAML + """
skills:
  paths:
    - ~/seed-skill
    - agents/pm/skills
""")
        skill_dir = tmp / "seed-skill" / "prd-drafter"
        skill_dir.mkdir(parents=True)
        skill_dir.joinpath("SKILL.md").write_text(
            "---\nname: prd-drafter\ndescription: draft a PRD.\n---\nbody\n"
        )

        saved = {k: os.environ.get(k) for k in ("OPERATOR_BOT", "HOME")}
        try:
            os.environ["OPERATOR_BOT"] = "testbot"
            os.environ["HOME"] = str(tmp)
            spec = importlib.util.spec_from_file_location(f"config_legacy_{id(tmp)}", REAL_CONFIG_PY)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)

            # Enabled derived from scanning the legacy paths (only ~/seed-skill
            # has a resolvable SKILL.md; agents/pm/skills is relative + cwd-
            # dependent and may or may not resolve — the enabled list still
            # contains prd-drafter from the tilde path).
            assert "prd-drafter" in mod.SKILLS_ENABLED, mod.SKILLS_ENABLED
            # External paths only retain the tilde-prefixed entry; the
            # relative 'agents/pm/skills' is dropped.
            assert mod.SKILLS_EXTERNAL_PATHS == ["~/seed-skill"], mod.SKILLS_EXTERNAL_PATHS
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        _shutil.rmtree(tmp)
    print("PASS  test_skills_legacy_paths_migrates_in_memory")


def test_skills_absent_block_defaults_empty():
    """Config with no `skills:` block → both lists empty, progressive True."""
    mod, _ = load_config(MIN_YAML)
    assert mod.SKILLS_ENABLED == []
    assert mod.SKILLS_EXTERNAL_PATHS == []
    assert mod.SKILLS_PROGRESSIVE_DISCLOSURE is True
    print("PASS  test_skills_absent_block_defaults_empty")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_missing_operator_bot_exits,
        test_unknown_bot_exits,
        test_happy_path_parses_fields,
        test_system_prompt_composition,
        test_claude_md_imports_mounted_into_system_prompt,
        test_claude_md_imports_missing_paths_skipped_silently,
        test_intro_on_join_default_and_override,
        test_mcp_servers_filter_and_overrides,
        test_mcp_env_strips_unsafe_keys,
        test_relativize_home_renders_tilde,
        test_skills_new_shape_parses,
        test_skills_legacy_paths_migrates_in_memory,
        test_skills_absent_block_defaults_empty,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            import traceback
            print(f"FAIL  {t.__name__}: {e}")
            traceback.print_exc()
            failures.append(t.__name__)

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
