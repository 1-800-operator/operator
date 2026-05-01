"""
Static bootability check for every MCP in every bundled agent.

Catches the class of bug where a bundled `config.yaml` advertises an MCP
whose `command`/`args` can't actually resolve on a clean install:

  - Relative path commands like `./github-mcp-server` (breaks once CWD
    isn't the repo root — i.e. under `uv tool install`).
  - Relative-path `python <file>.py` invocations that rot when the
    package layout moves (the session 152 `agents/engineer/claude_code.py`
    crash was exactly this).
  - `npx` entries where the package isn't installable — not checked here
    because `npx -y <pkg>@<ver>` is resolved at subprocess time by node;
    version pinning is validated instead.

Scope: every MCP in every `src/_1_800_operator/agents/*/config.yaml`,
INCLUDING `enabled: false` blocks. If we ship the definition, it has to
work — otherwise the wizard flips it on and the user hits a boot error.

This is the static tier. A dynamic tier (actually spawn each MCP, call
`initialize()` + `list_tools()`, then kill) lives under deferred-live
until we have credential fixtures for auth-required servers.

Run:
    source venv/bin/activate
    python tests/test_bundled_mcps_bootable_static.py
"""
import importlib.util
import os
import shutil
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import yaml  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
BUNDLED_AGENTS_DIR = REPO_ROOT / "src" / "_1_800_operator" / "agents"


def _iter_bundled_configs():
    """Yield (agent_name, config_path, parsed_yaml) for each bundled agent."""
    for cfg_path in sorted(BUNDLED_AGENTS_DIR.glob("*/config.yaml")):
        agent = cfg_path.parent.name
        yield agent, cfg_path, yaml.safe_load(cfg_path.read_text())


def _check_module_importable(module_path: str) -> tuple[bool, str]:
    """Return (ok, detail). Uses importlib.util.find_spec so we don't execute
    the module — we just verify Python can locate it on sys.path (with src/
    prepended, as the test harness + launcher both do)."""
    try:
        spec = importlib.util.find_spec(module_path)
    except (ImportError, ValueError) as e:
        return False, f"find_spec raised: {e}"
    if spec is None:
        return False, "module not found on sys.path"
    return True, f"resolved to {spec.origin}"


def _classify_command(cmd: str, args: list) -> tuple[str, str]:
    """Return (status, detail) where status ∈ {'ok', 'fail', 'warn'}."""
    # Case 1: python -m <module> — module must be importable
    if cmd in ("python", "python3") and args and args[0] == "-m":
        if len(args) < 2:
            return "fail", "python -m requires a module name in args[1]"
        ok, detail = _check_module_importable(args[1])
        return ("ok" if ok else "fail"), detail

    # Case 2: python <script.py> — script must be an absolute existing path
    if cmd in ("python", "python3") and args:
        first = args[0]
        if first.startswith("-"):
            # Some other python flag — tolerate but note
            return "warn", f"python flag {first!r} — not validated"
        if not os.path.isabs(first):
            return "fail", (
                f"python script args[0]={first!r} is a relative path. "
                f"Relative paths resolve against CWD which changes once "
                f"operator is installed via `uv tool install`. Use "
                f"`-m <module>` form instead."
            )
        if not os.path.exists(first):
            return "fail", f"python script args[0]={first!r} does not exist"
        return "ok", f"absolute script path exists: {first}"

    # Case 3: relative-path command (./foo, ../foo, foo/bar) — always a fail
    if cmd.startswith("./") or cmd.startswith("../") or ("/" in cmd and not os.path.isabs(cmd)):
        return "fail", (
            f"command {cmd!r} is a relative path. Resolves against CWD, "
            f"which is the launcher's REPO_DIR today but won't be under "
            f"`uv tool install`. Either install the binary on PATH and use "
            f"the bare name, or switch to an npx/module-based invocation."
        )

    # Case 4: absolute-path command — must exist and be executable
    if os.path.isabs(cmd):
        if not os.path.exists(cmd):
            return "fail", f"absolute command {cmd!r} does not exist"
        if not os.access(cmd, os.X_OK):
            return "fail", f"absolute command {cmd!r} is not executable"
        return "ok", f"absolute path exists and is executable"

    # Case 5: bare command name — must be on PATH
    resolved = shutil.which(cmd)
    if resolved is None:
        return "fail", (
            f"command {cmd!r} not found on PATH. Either install it "
            f"(document in README + install.sh) or switch to an invocation "
            f"that doesn't require a separate install (e.g. `npx -y <pkg>`)."
        )
    return "ok", f"resolved to {resolved}"


def _check_version_pinning(cmd: str, args: list) -> tuple[str, str] | None:
    """Return ('warn', detail) if an @latest package is detected, else None.
    Applies to `npx -y <pkg>@<ver>` entries."""
    if cmd != "npx":
        return None
    # look for <anything>@latest (excluding URLs in mcp-remote args)
    for a in args:
        if isinstance(a, str) and a.endswith("@latest"):
            return "warn", (
                f"npx package {a!r} is pinned to @latest — upstream changes "
                f"could silently break meeting runs. Pin to a specific "
                f"version after pressure-testing (15.7.5 matrix)."
            )
    return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_every_bundled_mcp_command_resolves():
    """For each bundled agent, every MCP block (enabled or not) must have a
    resolvable `command`. This is the cardinal shipping promise: if the
    wizard can turn it on, boot must work on a clean install."""
    failures = []
    for agent, cfg_path, cfg in _iter_bundled_configs():
        servers = (cfg or {}).get("mcp_servers", {}) or {}
        for name, block in servers.items():
            cmd = block.get("command", "")
            args = block.get("args", []) or []
            status, detail = _classify_command(cmd, args)
            if status == "fail":
                failures.append(
                    f"{agent}/{name}: command={cmd!r} args={args!r} — {detail}"
                )

    if failures:
        print("FAIL  test_every_bundled_mcp_command_resolves")
        for f in failures:
            print(f"  ✗ {f}")
        raise AssertionError(f"{len(failures)} unresolvable MCP command(s)")
    print("PASS  test_every_bundled_mcp_command_resolves")


def test_bundled_mcps_pin_versions():
    """`npx -y <pkg>@latest` is a ticking bomb — upstream format drift will
    silently break meetings. Flag (warn, not fail) so the 15.7.5 pressure-
    test matrix has a visible punch list."""
    warnings = []
    for agent, _cfg_path, cfg in _iter_bundled_configs():
        servers = (cfg or {}).get("mcp_servers", {}) or {}
        for name, block in servers.items():
            cmd = block.get("command", "")
            args = block.get("args", []) or []
            result = _check_version_pinning(cmd, args)
            if result is not None:
                status, detail = result
                warnings.append(f"{agent}/{name}: {detail}")

    # Warnings only — this test passes even if warnings fire, since the
    # 15.7.5 work explicitly left these unpinned with a "pin after
    # pressure-testing" comment. We just want them visible in CI output.
    if warnings:
        print(f"PASS  test_bundled_mcps_pin_versions ({len(warnings)} @latest warnings — see 15.7.5 matrix)")
        for w in warnings:
            print(f"  ⚠ {w}")
    else:
        print("PASS  test_bundled_mcps_pin_versions")


def test_bundled_agents_are_parseable():
    """Every bundled config must be valid YAML with an `mcp_servers` block
    that's a dict (or absent). Defense against a malformed edit that would
    make the bootability check silently skip servers."""
    failures = []
    for agent, cfg_path, cfg in _iter_bundled_configs():
        if cfg is None:
            failures.append(f"{agent}: {cfg_path} parsed to None")
            continue
        if not isinstance(cfg, dict):
            failures.append(f"{agent}: top-level not a dict ({type(cfg).__name__})")
            continue
        servers = cfg.get("mcp_servers")
        if servers is not None and not isinstance(servers, dict):
            failures.append(f"{agent}: mcp_servers is {type(servers).__name__}, expected dict")

    if failures:
        print("FAIL  test_bundled_agents_are_parseable")
        for f in failures:
            print(f"  ✗ {f}")
        raise AssertionError(f"{len(failures)} unparseable bundled config(s)")
    print("PASS  test_bundled_agents_are_parseable")


if __name__ == "__main__":
    tests = [
        test_bundled_agents_are_parseable,
        test_every_bundled_mcp_command_resolves,
        test_bundled_mcps_pin_versions,
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
