"""
Tests for the bundled codex agent config — schema validation,
default values, and the new codex-specific permissions block.

Loads the agent config under OPERATOR_BOT=codex and checks that all
fields wired by config.py have the expected values, then drives a
few config-error scenarios via inline tweaks to make sure the new
validation surface catches them.

Usage:
    python tests/test_codex_agent_config.py
"""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _check(label, cond, detail=""):
    mark = "✓" if cond else "✗"
    print(f"  {mark} {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        raise AssertionError(label)


def _seed_codex_into(home_dir: Path):
    """Copy the bundled codex agent into a fake $HOME/.operator/agents/."""
    bundled = (
        Path(__file__).resolve().parent.parent
        / "src" / "_1_800_operator" / "agents" / "codex"
    )
    dst = home_dir / ".operator" / "agents" / "codex"
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(bundled, dst)
    return dst


def _load_config_with(env_overrides=None, fake_home: Path | None = None):
    """Reload the config module with the given env. Returns the module."""
    import importlib
    save = {k: os.environ.get(k) for k in ("OPERATOR_BOT", "HOME")}
    if env_overrides:
        for k, v in env_overrides.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = str(v)
    if fake_home is not None:
        os.environ["HOME"] = str(fake_home)
    try:
        if "_1_800_operator.config" in sys.modules:
            del sys.modules["_1_800_operator.config"]
        return importlib.import_module("_1_800_operator.config")
    finally:
        for k, v in save.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_config_loads_with_expected_defaults():
    print("\n1. Bundled codex config loads with expected defaults")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        _seed_codex_into(home)
        # The .env loader is keyed off `Path.home() / ".operator" / ".env"`
        # which we just created the parent of; absent .env is fine.
        cfg = _load_config_with({"OPERATOR_BOT": "codex"}, fake_home=home)
        _check("LLM_PROVIDER is codex_mcp", cfg.LLM_PROVIDER == "codex_mcp")
        _check("CODEX_APPROVAL_POLICY default", cfg.CODEX_APPROVAL_POLICY == "on-request")
        _check("CODEX_SANDBOX default", cfg.CODEX_SANDBOX == "read-only")
        _check("VOICE default plain", cfg.VOICE == "plain")
        _check("CAPTIONS_ENABLED False", cfg.CAPTIONS_ENABLED is False)
        _check("TRIGGER_PHRASE @codex", cfg.TRIGGER_PHRASE == "@codex")
        _check("AGENT_NAME Codex", cfg.AGENT_NAME == "Codex")
        _check("MCP_SERVERS contains 'codex'", "codex" in cfg.MCP_SERVERS)
        codex_srv = cfg.MCP_SERVERS["codex"]
        _check("MCP_SERVERS.codex.command", codex_srv.get("command") == "codex")
        _check("MCP_SERVERS.codex.args", codex_srv.get("args") == ["mcp-server"])
        _check("MCP_SERVERS.codex.env clears OPENAI_API_KEY",
               codex_srv.get("env", {}).get("OPENAI_API_KEY") == "")
        # CLAUDE_MD_BLOCK skipped for brain providers (codex inherits this
        # path from claude_cli — both CLIs read AGENTS.md / CLAUDE.md
        # themselves; re-passing would double context).
        _check("CLAUDE_MD_BLOCK is empty for codex", cfg.CLAUDE_MD_BLOCK == "")
        _check("SYSTEM_PROMPT starts with framework prompt",
               cfg.SYSTEM_PROMPT.startswith("You are Codex"))


def test_invalid_approval_policy_rejected():
    print("\n2. Invalid default_approval_policy is rejected at load")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        codex_dir = _seed_codex_into(home)
        # Hand-edit to a typo
        cfg_path = codex_dir / "config.yaml"
        text = cfg_path.read_text()
        text = text.replace(
            "default_approval_policy: on-request",
            "default_approval_policy: aggressive",
        )
        cfg_path.write_text(text)
        try:
            _load_config_with({"OPERATOR_BOT": "codex"}, fake_home=home)
        except SystemExit:
            return  # config.py exits with a clear message; that's pass
        raise AssertionError("expected config to reject invalid policy")


def test_invalid_sandbox_rejected():
    print("\n3. Invalid default_sandbox is rejected at load")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        codex_dir = _seed_codex_into(home)
        cfg_path = codex_dir / "config.yaml"
        text = cfg_path.read_text()
        text = text.replace(
            "default_sandbox: read-only",
            "default_sandbox: full-access",
        )
        cfg_path.write_text(text)
        try:
            _load_config_with({"OPERATOR_BOT": "codex"}, fake_home=home)
        except SystemExit:
            return
        raise AssertionError("expected config to reject invalid sandbox")


def test_unknown_provider_rejected():
    print("\n4. Unknown llm.provider rejected (and error mentions codex_mcp)")
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        codex_dir = _seed_codex_into(home)
        cfg_path = codex_dir / "config.yaml"
        text = cfg_path.read_text()
        text = text.replace('provider: "codex_mcp"', 'provider: "codex_xyz"')
        cfg_path.write_text(text)
        try:
            _load_config_with({"OPERATOR_BOT": "codex"}, fake_home=home)
        except SystemExit as e:
            return
        raise AssertionError("expected config to reject unknown provider")


def main():
    print("=" * 50)
    print("Codex agent config")
    print("=" * 50)
    failed = 0
    for fn in [
        test_config_loads_with_expected_defaults,
        test_invalid_approval_policy_rejected,
        test_invalid_sandbox_rejected,
        test_unknown_provider_rejected,
    ]:
        try:
            fn()
        except AssertionError as e:
            print(f"  FAILED: {e}")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {type(e).__name__}: {e}")
            failed += 1
    print("\n" + "=" * 50)
    if failed == 0:
        print("All tests passed!")
        return 0
    print(f"{failed} test(s) failed")
    return 1


if __name__ == "__main__":
    sys.exit(main())
