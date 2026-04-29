"""
Test config-load schema validation.

Validates that bad shapes in the agent YAML produce clear, actionable
stderr messages and exit cleanly — rather than letting the bad shape
leak into runtime where it surfaces as TypeErrors deep in the call
stack (the exact failure mode that bit the user during the
mcp__transcript__recall_transcript edit).

Usage:
    python tests/test_config_validation.py
"""
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

# We need to manipulate the env BEFORE importing config, and we re-import
# config per-test to get fresh validation. Use a helper.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


_BASE_VALID_CONFIG = """
agent:
  name: TestBot
  trigger_phrase: "@test"
llm:
  provider: claude_cli
"""


def _run_with_config(yaml_text: str):
    """Set up a temp agents dir + run config import. Returns (exit_code, stderr_text).

    Uses subprocess so we get a clean Python interpreter per test —
    config.py is module-level evaluated and can't easily be reloaded.
    """
    import subprocess
    with tempfile.TemporaryDirectory() as td:
        agents_dir = Path(td) / ".brainchild" / "agents" / "testbot"
        agents_dir.mkdir(parents=True)
        (agents_dir / "config.yaml").write_text(yaml_text)
        # Patch BRAINCHILD_BOT and HOME so config.py finds our temp config.
        env = {
            **os.environ,
            "BRAINCHILD_BOT": "testbot",
            "HOME": str(td),
            "PYTHONPATH": os.path.join(os.path.dirname(__file__), "..", "src"),
        }
        result = subprocess.run(
            [sys.executable, "-c", "from brainchild import config"],
            env=env,
            capture_output=True,
            text=True,
            cwd=os.path.join(os.path.dirname(__file__), ".."),
        )
        return result.returncode, result.stderr


def test_valid_config_loads():
    code, err = _run_with_config(_BASE_VALID_CONFIG)
    assert code == 0, f"valid config rejected: {err}"
    print("✓ valid config loads cleanly")


def test_dict_in_string_list_caught():
    """The exact failure mode the user hit: a missing newline turned a
    list entry into a dict-shaped item."""
    yaml_text = _BASE_VALID_CONFIG + """
permissions:
  always_ask:
  - Bash
  - Editmcp_servers:
    sentry:
      enabled: true
"""
    code, err = _run_with_config(yaml_text)
    assert code == 2, f"bad config accepted (code={code}): {err}"
    assert "permissions.always_ask" in err, err
    assert "must be a string" in err, err
    print("✓ dict-instead-of-string in list caught")


def test_missing_agent_block():
    yaml_text = """
llm:
  provider: claude_cli
"""
    code, err = _run_with_config(yaml_text)
    assert code == 2, err
    assert "agent" in err and "missing" in err.lower(), err
    print("✓ missing agent block caught")


def test_unknown_provider():
    yaml_text = """
agent:
  name: TestBot
llm:
  provider: gpt5
"""
    code, err = _run_with_config(yaml_text)
    assert code == 2, err
    assert "llm.provider" in err and "gpt5" in err, err
    print("✓ unknown provider caught")


def test_missing_model_for_openai():
    yaml_text = """
agent:
  name: TestBot
llm:
  provider: openai
"""
    code, err = _run_with_config(yaml_text)
    assert code == 2, err
    assert "llm.model" in err, err
    print("✓ openai requires model")


def test_claude_cli_does_not_require_model():
    yaml_text = """
agent:
  name: TestBot
llm:
  provider: claude_cli
"""
    code, err = _run_with_config(yaml_text)
    assert code == 0, f"claude_cli without model rejected: {err}"
    print("✓ claude_cli does not require model")


def test_captions_enabled_must_be_bool():
    yaml_text = _BASE_VALID_CONFIG + """
transcript:
  captions_enabled: "yes please"
"""
    code, err = _run_with_config(yaml_text)
    assert code == 2, err
    assert "captions_enabled" in err and "true or false" in err, err
    print("✓ non-bool captions_enabled caught")


def test_mcp_server_block_must_be_mapping():
    yaml_text = _BASE_VALID_CONFIG + """
mcp_servers:
  sentry: "this should be a dict not a string"
"""
    code, err = _run_with_config(yaml_text)
    assert code == 2, err
    assert "mcp_servers.sentry" in err, err
    print("✓ non-mapping mcp_server caught")


def test_multiple_errors_surfaced_together():
    """All violations should be listed in one shot, not just the first."""
    yaml_text = """
agent:
  name: TestBot
llm:
  provider: openai
permissions:
  always_ask:
  - {bad: dict}
  - {also: bad}
"""
    code, err = _run_with_config(yaml_text)
    assert code == 2, err
    # Should mention both: missing model, and two bad entries in always_ask
    assert "llm.model" in err, err
    assert "always_ask[0]" in err, err
    assert "always_ask[1]" in err, err
    print("✓ all errors surfaced in one message")


def test_error_message_points_at_brainchild_edit():
    yaml_text = _BASE_VALID_CONFIG + """
permissions:
  always_ask:
  - {bad: dict}
"""
    code, err = _run_with_config(yaml_text)
    assert "brainchild edit" in err, err
    print("✓ error message mentions brainchild edit")


if __name__ == "__main__":
    test_valid_config_loads()
    test_dict_in_string_list_caught()
    test_missing_agent_block()
    test_unknown_provider()
    test_missing_model_for_openai()
    test_claude_cli_does_not_require_model()
    test_captions_enabled_must_be_bool()
    test_mcp_server_block_must_be_mapping()
    test_multiple_errors_surfaced_together()
    test_error_message_points_at_brainchild_edit()
    print("\nAll config validation tests passed.")
