"""
Tests that the back-compat translation in config.py promotes legacy
per-MCP `read_tools` / `confirm_tools` entries into the unified
PERMISSIONS_AUTO_APPROVE / PERMISSIONS_ALWAYS_ASK lists.

Run: python tests/test_legacy_permissions_migration.py
"""
import importlib
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))


def _load_config_with_yaml(yaml_text):
    """Boot _1_800_operator.config with a temporary ~/.operator/agents/fakebot/config.yaml.

    Overrides $HOME so config.py's `Path.home() / ".operator" / "agents"`
    points at our scratch dir. Wipes any cached _1_800_operator.config module so
    each call gets a clean read.
    """
    tmp = tempfile.mkdtemp()
    bot_dir = Path(tmp) / ".operator" / "agents" / "fakebot"
    bot_dir.mkdir(parents=True)
    (bot_dir / "config.yaml").write_text(textwrap.dedent(yaml_text))
    # Empty .env so resolver finds no leaked secrets from the test machine.
    (Path(tmp) / ".operator" / ".env").write_text("")

    os.environ["OPERATOR_BOT"] = "fakebot"
    os.environ["HOME"]           = tmp

    for mod_name in list(sys.modules):
        if mod_name == "_1_800_operator.config":
            del sys.modules[mod_name]
    return importlib.import_module("_1_800_operator.config")


def test_legacy_read_tools_translate_to_auto_approve():
    cfg = _load_config_with_yaml("""
        agent:
          name: "FakeBot"
          trigger_phrase: "@fakebot"
        llm:
          provider: "openai"
          model: "gpt-5"
        mcp_servers:
          linear:
            command: "echo"
            args: []
            read_tools:
              - get_issue
              - list_issues
            confirm_tools: []
        system_prompt: ""
    """)
    assert "mcp__linear__get_issue" in cfg.PERMISSIONS_AUTO_APPROVE
    assert "mcp__linear__list_issues" in cfg.PERMISSIONS_AUTO_APPROVE


def test_legacy_confirm_tools_translate_to_always_ask():
    cfg = _load_config_with_yaml("""
        agent:
          name: "FakeBot"
          trigger_phrase: "@fakebot"
        llm:
          provider: "openai"
          model: "gpt-5"
        mcp_servers:
          sentry:
            command: "echo"
            args: []
            read_tools: []
            confirm_tools:
              - analyze_issue_with_seer
        system_prompt: ""
    """)
    assert "mcp__sentry__analyze_issue_with_seer" in cfg.PERMISSIONS_ALWAYS_ASK


def test_unified_permissions_block_takes_precedence():
    """Top-level permissions block + per-server lists merge cleanly."""
    cfg = _load_config_with_yaml("""
        agent:
          name: "FakeBot"
          trigger_phrase: "@fakebot"
        llm:
          provider: "openai"
          model: "gpt-5"
        permissions:
          auto_approve:
            - Read
            - "mcp__sentry__search_*"
          always_ask:
            - Bash
        mcp_servers:
          linear:
            command: "echo"
            args: []
            read_tools:
              - get_issue
            confirm_tools: []
        system_prompt: ""
    """)
    # Native + legacy translation both present
    assert "Read"                    in cfg.PERMISSIONS_AUTO_APPROVE
    assert "mcp__sentry__search_*"   in cfg.PERMISSIONS_AUTO_APPROVE
    assert "mcp__linear__get_issue"  in cfg.PERMISSIONS_AUTO_APPROVE
    assert "Bash"                    in cfg.PERMISSIONS_ALWAYS_ASK


def test_chat_runner_needs_confirmation_uses_unified_lists():
    """_needs_confirmation respects PERMISSIONS_AUTO_APPROVE / ALWAYS_ASK."""
    cfg = _load_config_with_yaml("""
        agent:
          name: "FakeBot"
          trigger_phrase: "@fakebot"
        llm:
          provider: "openai"
          model: "gpt-5"
        permissions:
          auto_approve:
            - "mcp__linear__get_*"
          always_ask:
            - "mcp__linear__delete_issue"
        mcp_servers: {}
        system_prompt: ""
    """)
    # Re-import chat_runner so it picks up the freshly-loaded config.
    if "_1_800_operator.pipeline.chat_runner" in sys.modules:
        del sys.modules["_1_800_operator.pipeline.chat_runner"]
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    runner = ChatRunner.__new__(ChatRunner)  # bypass __init__

    assert runner._needs_confirmation({"name": "linear__get_issue"})    is False
    assert runner._needs_confirmation({"name": "linear__delete_issue"}) is True
    assert runner._needs_confirmation({"name": "linear__save_issue"})   is True


if __name__ == "__main__":
    fns = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        try:
            fn()
            print(f"  ✓ {fn.__name__}")
        except AssertionError as e:
            print(f"  ✗ {fn.__name__}: {e}")
            sys.exit(1)
    print(f"\n{len(fns)} tests passed.")
