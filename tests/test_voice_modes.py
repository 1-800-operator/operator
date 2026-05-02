"""
Tests for the voice mode redesign.

Architectural shape: operator emits a sterile, neutral confirmation
prompt regardless of voice. The bot's persona (set via the
system_prompt field) is responsible for the conversational preamble that
appears in chat before the system's prompt. This keeps customization
(pirate voice, Spanish, etc.) cleanly in prompt territory and out of
Python templating.

The two voice modes only choose how much detail to show in the
sterile prompt:
  plain     — one-line summary that hides bulk content fields, keeps
              imperative fields verbatim
  technical — full parameter dump with head…tail truncation

Run: python tests/test_voice_modes.py
"""
import importlib
import os
import sys
import tempfile
import textwrap
from pathlib import Path
from unittest.mock import MagicMock


sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))


def _load_config(yaml_text):
    tmp = tempfile.mkdtemp()
    bot_dir = Path(tmp) / ".operator" / "agents" / "fakebot"
    bot_dir.mkdir(parents=True)
    (bot_dir / "config.yaml").write_text(textwrap.dedent(yaml_text))
    (Path(tmp) / ".operator" / ".env").write_text("")
    os.environ["OPERATOR_BOT"] = "fakebot"
    os.environ["HOME"]           = tmp
    for mod in list(sys.modules):
        if mod == "_1_800_operator.config":
            del sys.modules[mod]
    return importlib.import_module("_1_800_operator.config")


def _yaml(voice_line=""):
    return f"""
        agent:
          name: "FakeBot"
          trigger_phrase: "@fakebot"
          {voice_line}
        llm:
          provider: "openai"
          model: "gpt-5"
        mcp_servers: {{}}
        system_prompt: ""
    """


def test_voice_plain_is_default():
    cfg = _load_config(_yaml())
    assert cfg.VOICE == "plain"


def test_voice_technical_explicit():
    cfg = _load_config(_yaml("voice: technical"))
    assert cfg.VOICE == "technical"


def test_legacy_permission_verbosity_translates():
    """terse → plain, verbose → technical, with a deprecation log."""
    yaml = """
        agent:
          name: "FakeBot"
          trigger_phrase: "@fakebot"
          permission_verbosity: terse
        llm:
          provider: "openai"
          model: "gpt-5"
        mcp_servers: {}
        system_prompt: ""
    """
    cfg = _load_config(yaml)
    assert cfg.VOICE == "plain"

    yaml = yaml.replace("permission_verbosity: terse", "permission_verbosity: verbose")
    cfg = _load_config(yaml)
    assert cfg.VOICE == "technical"


def test_url_imperative_field_never_collapsed_in_terse():
    """The Sentry URL bug: terse mode must keep URLs verbatim, not collapse to (89 B)."""
    from _1_800_operator.pipeline.permission_chat_handler import _format_terse
    long_url = "https://operator-3z.sentry.io/issues/7441991509/events/" + "x" * 50
    out = _format_terse("mcp__sentry__get_sentry_resource", {"url": long_url})
    assert long_url in out, f"URL was collapsed to size hint: {out!r}"
    assert "B)" not in out, f"URL hidden as size hint: {out!r}"


def test_format_confirmation_plain_mode():
    """Plain voice produces a sterile single-line 'Run? <terse>\\nOK?' prompt.

    No persona phrasing — the bot's system_prompt directive is what
    paraphrases destructive actions in conversational chat *before*
    this sterile prompt arrives.
    """
    _load_config(_yaml("voice: plain"))
    if "_1_800_operator.pipeline.permission_chat_handler" in sys.modules:
        del sys.modules["_1_800_operator.pipeline.permission_chat_handler"]
    from _1_800_operator.pipeline.permission_chat_handler import _format_confirmation
    out = _format_confirmation("Bash", {"command": "ls /tmp"})
    assert out.startswith("Run? Bash:")
    assert "ls /tmp" in out
    assert out.rstrip().endswith("OK?")


def test_format_confirmation_technical_mode():
    """Technical voice produces a multi-line 'Run X?\\n  • ...' parameter dump."""
    _load_config(_yaml("voice: technical"))
    if "_1_800_operator.pipeline.permission_chat_handler" in sys.modules:
        del sys.modules["_1_800_operator.pipeline.permission_chat_handler"]
    from _1_800_operator.pipeline.permission_chat_handler import _format_confirmation
    out = _format_confirmation("Bash", {"command": "ls /tmp"})
    assert out.startswith("Run Bash?")
    assert "command:" in out
    assert "OK?" in out


def test_no_persona_templating_remains():
    """The operator templating layer must NOT carry per-tool English phrases.

    If `_format_plain`, `_friendly_mcp_name`, or `_MCP_SERVER_FRIENDLY`
    come back, persona customization (pirate, Spanish, etc.) breaks
    again. They live in the bot's prompt now, not in Python.
    """
    import _1_800_operator.pipeline.permission_chat_handler as h
    for forbidden in ("_format_plain", "_friendly_mcp_name",
                      "_MCP_SERVER_FRIENDLY", "_MCP_VERB_FRIENDLY"):
        assert not hasattr(h, forbidden), (
            f"{forbidden!r} reappeared in permission_chat_handler — "
            f"persona-flavored phrases belong in system_prompt, not Python"
        )


def test_narrator_silent_in_plain_mode():
    """Plain voice → operator narrator stays silent; bot self-narrates."""
    _load_config(_yaml("voice: plain"))
    if "_1_800_operator.pipeline.chat_runner" in sys.modules:
        del sys.modules["_1_800_operator.pipeline.chat_runner"]
    from _1_800_operator.pipeline.chat_runner import ChatRunner

    runner = ChatRunner.__new__(ChatRunner)
    runner._narration_auto_approve = {"Read"}
    runner._narration_buffer = []
    import threading
    runner._narration_lock = threading.Lock()
    runner._send = MagicMock()

    runner._on_tool_use("Read", {"file_path": "/tmp/x"})

    assert not runner._send.called, (
        "Plain voice should let the bot self-narrate via prompt — "
        "operator's narrator must stay silent."
    )


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
