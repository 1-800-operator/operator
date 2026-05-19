"""`operator doctor` — diagnostic checker (Phase 14.19.5).

Analogous to `brew doctor` / `flutter doctor`. Prints a checklist of
world-readiness items, marks each pass/fail, and emits the exact fix
command for failures. No interactive prompts, no fixups — purely
read-only diagnostics. Exits 0 if everything is green, 1 if any check
fails (so CI / scripts can gate on it).

Composition: each check is a tiny pure function returning a
`CheckResult(name, ok, detail, fix)`. `run_doctor()` calls them in
order and renders. macOS-only TCC checks (System Audio Recording + Microphone
for dial's audio helper, Phase 14.20.4) skip on other platforms.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from _1_800_operator import config
from _1_800_operator.pipeline.audio import WHISPER_BEAM_SIZE
from _1_800_operator.pipeline.claude_code_import import _probe_claude_code

CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_INSTALL_URL = "https://www.google.com/chrome/"


def chrome_installed() -> bool:
    """True on non-darwin (no system-Chrome dependency) or when the binary exists."""
    if sys.platform != "darwin":
        return True
    return CHROME_PATH.exists()

# Dial's audio helper. Production is the signed+notarized .app produced by
# scripts/build_signed_helper.sh (only this path can capture system audio).
# Dev fallback is the raw swiftc-built binary in-tree (mic-only). Production
# wins when both exist; mirrors attach_adapter.py:_AUDIO_HELPER_INSTALLED.
_AUDIO_HELPER_INSTALLED = (
    Path.home() / ".operator" / "bin" / "Operator.app"
    / "Contents" / "MacOS" / "Operator"
)
_AUDIO_HELPER_DEV = Path(__file__).resolve().parent.parent / "swift" / "Operator"

# AEC3 speaker-bleed cleaner. Production is the cargo-built binary installed
# under ~/.operator/bin/; dev fallback is the in-tree build. Mirrors
# attach_adapter.py:_AEC_BINARY_INSTALLED. Optional — missing AEC isn't fatal,
# dial just runs without the bleed defense.
_AEC_BINARY_INSTALLED = Path.home() / ".operator" / "bin" / "aec3"
_AEC_BINARY_DEV = (
    Path(__file__).resolve().parent.parent / "rust" / "aec3" / "target" / "release" / "aec3"
)


def _audio_helper() -> Path | None:
    for p in (_AUDIO_HELPER_INSTALLED, _AUDIO_HELPER_DEV):
        if p.exists() and p.is_file():
            return p
    return None


def _aec_binary() -> Path | None:
    for p in (_AEC_BINARY_INSTALLED, _AEC_BINARY_DEV):
        if p.exists() and p.is_file():
            return p
    return None


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str   # one-line status (green or red)
    fix: str      # exact command(s) to run; empty when ok
    optional: bool = False  # if True, a failure renders as a warning and doesn't drive exit code


def _check_claude() -> CheckResult:
    """claude CLI on PATH + logged in via `claude auth status --json`."""
    if shutil.which("claude") is None:
        return CheckResult(
            name="claude CLI",
            ok=False,
            detail="not on PATH",
            fix="install Claude Code: https://docs.anthropic.com/claude/docs/claude-code",
        )
    status, detail = _probe_claude_code(check_auth=True)
    if status == "ok":
        return CheckResult("claude CLI", True, "installed and logged in", "")
    fix = (
        "update Claude Code — run /plugin, or reinstall from https://claude.ai/code"
        if status == "version_too_old"
        else "claude auth login"
    )
    return CheckResult(
        name="claude CLI",
        ok=False,
        detail=detail,
        fix=fix,
    )


def _check_chrome() -> CheckResult:
    """Real Google Chrome.app present (macOS) — required by dial's CDP attach."""
    if chrome_installed():
        return CheckResult(
            "Google Chrome",
            True,
            f"installed at {CHROME_PATH}",
            "",
        )
    return CheckResult(
        name="Google Chrome",
        ok=False,
        detail="not installed",
        fix=f"brew install --cask google-chrome  (or download from {CHROME_INSTALL_URL})",
    )


def _check_git() -> CheckResult:
    """git on PATH — required by claude_cli's worktree + repo ops."""
    if shutil.which("git") is None:
        return CheckResult(
            name="git",
            ok=False,
            detail="not on PATH",
            fix="install git from https://git-scm.com/  (or `xcode-select --install`)",
        )
    try:
        r = subprocess.run(
            ["git", "--version"], capture_output=True, text=True, timeout=2
        )
        version = r.stdout.strip() or "(unknown version)"
    except (subprocess.TimeoutExpired, OSError):
        version = "(installed)"
    return CheckResult("git", True, version, "")


def _check_cwd_trusted(*, registry_path: Path | None = None,
                       cwd: Path | None = None) -> CheckResult:
    """Claude Code's workspace-trust state for the current directory.

    `operator dial` spawns claude with cwd = the dir operator was
    invoked from. If Claude Code hasn't been trusted for that dir, it
    shows a first-run "trust this folder?" dialog and blocks on input —
    which wedges the meeting bot's boot: the SessionStart hook never
    fires, so the provider's _wait_for_ready hits its ceiling. This is
    knowable upfront from ~/.claude.json, so doctor surfaces it here —
    the user accepts the dialog once, in Claude Code, before a meeting
    rather than discovering it mid-call.

    Inform-only (optional): operator never *writes* the trust state
    itself — programmatically suppressing a Claude Code security prompt
    would be patterning against it. We tell the human; the human decides.

    Fails open: an unreadable ~/.claude.json just skips the check.
    """
    cwd = cwd or Path.cwd()
    registry = registry_path or (Path.home() / ".claude.json")
    try:
        data = json.loads(Path(registry).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return CheckResult(
            "workspace trust", True,
            "skipped (couldn't read ~/.claude.json)", "",
            optional=True,
        )
    projects = data.get("projects", {}) if isinstance(data, dict) else {}
    # ~/.claude.json keys projects by absolute path — check the cwd as-is
    # and resolved (macOS /tmp vs /private/tmp, symlinks).
    entry = None
    for key in (str(cwd), str(Path(cwd).resolve())):
        if isinstance(projects.get(key), dict):
            entry = projects[key]
            break
    if entry is not None and entry.get("hasTrustDialogAccepted") is True:
        return CheckResult(
            "workspace trust", True,
            f"{cwd} is trusted by Claude Code", "",
        )
    return CheckResult(
        name="workspace trust",
        ok=False,
        detail=(
            f"{cwd} is not trusted by Claude Code — a meeting started here "
            f"would hang at the first-run trust dialog"
        ),
        fix=(
            "open this folder in Claude Code once and accept the trust "
            "prompt — or run /operator:dial from a folder you've already "
            "used with Claude Code"
        ),
        optional=True,
    )


# Canonical installer one-liner — same URL the README and the install.sh
# header advertise. Reproduced here so doctor's fix lines stay literally
# copy-pasteable for terminal users who never want to leave their shell.
_INSTALLER_CMD = "curl -LsSf https://1-800-operator.com/install | bash"


def _installer_fix(suffix: str = "") -> str:
    """Standard dual-target fix line for anything install.sh would re-set-up.

    Two audiences, one line:
      • A Claude Code session reading doctor output (e.g. one that ran
        /operator:doctor) — it sees "ask Claude to fix this" and knows
        it's the addressee. It already has the install.sh contents in
        the repo and full Bash + Edit tool access, so it can resolve
        the issue without a context switch.
      • Terminal-only users (no Claude Code session in the loop) get the
        verbatim curl command they can paste into the shell.

    `suffix` lets a specific check add extra wording (e.g. a pre-req that
    needs installing before the installer can finish, like cargo for aec3).
    """
    base = f"ask Claude to fix this, or re-run the installer: {_INSTALLER_CMD}"
    return f"{suffix} — {base}" if suffix else base


_MCP_SERVER_NAME = "operator-meeting-record"
_MCP_ALLOWLIST_ENTRY = "mcp__operator-meeting-record__*"
# Regex matches the `claude mcp list` line for our server. The CLI prints
#   "<name>: <command_or_url> - <status>"
# where <status> starts with "✓" when connected. We capture the status
# tail so the doctor message can include the CLI's own wording on failure.
_MCP_LIST_LINE = re.compile(
    rf"^{re.escape(_MCP_SERVER_NAME)}:\s.*?-\s(?P<status>.+?)\s*$",
    re.MULTILINE,
)


def _run_claude_mcp_list() -> tuple[int, str]:
    """Default `claude mcp list` runner. Returns (returncode, combined_output).

    Timeout sized to the CLI's worst case — it health-checks every
    registered server in series, including hosted connectors. 15s
    accommodates a few slow-but-reachable HTTP servers; a longer hang
    almost certainly means the user's claude install itself is wedged.
    """
    try:
        r = subprocess.run(
            ["claude", "mcp", "list"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except subprocess.TimeoutExpired:
        return 124, "timed out"
    except OSError as e:
        return 127, str(e)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def _check_meeting_record_mcp(
    *,
    settings_path: Path | None = None,
    mcp_list_runner: Callable[[], tuple[int, str]] | None = None,
) -> CheckResult:
    """Bundled operator-meeting-record MCP is registered AND allowlisted.

    Two silent failure modes that today only surface mid-meeting:

      1. **Registration missing or unhealthy** — `claude mcp list` either
         doesn't show operator-meeting-record at all (install.sh never
         finished, or a user-scope `claude mcp remove` was run) or shows
         it with a non-connected status. Claude can call the tool names,
         but the call fails at dispatch.

      2. **Allowlist missing** — `~/.claude/settings.json`
         permissions.allow lacks the `mcp__operator-meeting-record__*`
         wildcard. In the Claude Code desktop app (most users' default
         surface), the call silent-fails with no approval prompt and the
         model goes quiet. (See project memory: desktop-app silences
         non-allowlisted Bash + MCP calls.)

      Both are install.sh's job; this check catches drift after upgrades
      or manual edits. Fix line routes either through the Claude Code
      session (which can run the install commands directly) or the
      canonical curl-piped installer (see _installer_fix).

    Requires claude CLI on PATH. If absent, the dedicated claude-CLI
    check has already failed loudly; we skip here to avoid double-noise.
    """
    if shutil.which("claude") is None:
        return CheckResult(
            name="operator-meeting-record MCP",
            ok=True,
            detail="skipped (claude CLI not on PATH)",
            fix="",
            optional=True,
        )

    runner = mcp_list_runner or _run_claude_mcp_list
    rc, out = runner()
    if rc != 0:
        return CheckResult(
            name="operator-meeting-record MCP",
            ok=False,
            detail=f"`claude mcp list` failed (rc={rc}): {out.strip()[:120]}",
            fix=_installer_fix("run `claude mcp list` directly to investigate first"),
        )
    match = _MCP_LIST_LINE.search(out)
    if match is None:
        return CheckResult(
            name="operator-meeting-record MCP",
            ok=False,
            detail=(
                "not registered — Claude can't read meeting captions or "
                "search past meetings"
            ),
            fix=_installer_fix(),
        )
    status = match.group("status").strip()
    # `claude mcp list` prefixes connected servers with "✓". Anything
    # else (✗ Failed to connect, ! Needs authentication, etc.) means the
    # tools aren't reachable from claude either.
    if not status.startswith("✓"):
        return CheckResult(
            name="operator-meeting-record MCP",
            ok=False,
            detail=f"registered but not connected: {status}",
            fix=_installer_fix(),
        )

    # Allowlist half.
    path = settings_path or (Path.home() / ".claude" / "settings.json")
    try:
        cfg = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return CheckResult(
            name="operator-meeting-record MCP",
            ok=False,
            detail=(
                f"registered, but ~/.claude/settings.json is missing — desktop-app "
                f"tool calls will silent-fail"
            ),
            fix=_installer_fix(),
        )
    except (OSError, ValueError) as e:
        return CheckResult(
            name="operator-meeting-record MCP",
            ok=True,
            detail=f"registered; couldn't parse settings.json to verify allowlist ({e})",
            fix="",
            optional=True,
        )
    allow = (
        cfg.get("permissions", {}).get("allow", [])
        if isinstance(cfg, dict) else []
    )
    if not isinstance(allow, list) or _MCP_ALLOWLIST_ENTRY not in allow:
        return CheckResult(
            name="operator-meeting-record MCP",
            ok=False,
            detail=(
                f"registered, but {_MCP_ALLOWLIST_ENTRY!r} missing from "
                f"settings.json — desktop-app tool calls will silent-fail"
            ),
            fix=_installer_fix(),
        )
    return CheckResult(
        name="operator-meeting-record MCP",
        ok=True,
        detail="registered and allowlisted",
        fix="",
    )


_TCC_STATUS_DETAIL = {
    "ok": "granted",
    "denied": "denied",
    "restricted": "restricted by policy",
    "not_determined": "not yet prompted",
    "unknown": "unknown",
}


def _probe_audio_helper() -> dict[str, str] | None:
    """Run the helper with --probe; return parsed status dict or None on failure."""
    helper = _audio_helper()
    if helper is None:
        return None
    try:
        # Minimal env — the helper has no auth needs and shouldn't see
        # the user's shell secrets. See _disclaimed_spawn.minimal_helper_env.
        from _1_800_operator.pipeline._disclaimed_spawn import minimal_helper_env
        r = subprocess.run(
            [str(helper), "--probe"],
            capture_output=True,
            text=True,
            timeout=5,
            env=minimal_helper_env(),
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None


def _check_system_audio(probe: dict[str, str] | None) -> CheckResult:
    """TCC System Audio Recording — dial system-audio capture (Core Audio Tap).

    Phase 14.32 migration replaced SCStream (gated behind kTCCServiceScreenCapture)
    with Core Audio Process Tap (gated behind kTCCServiceAudioCapture, the
    "System Audio Recording Only" pane). The helper-side dialog is surfaced
    by AudioHardwareCreateProcessTap on first call when permission is not
    yet determined; this check just reads the helper's --probe output.
    """
    if _audio_helper() is None:
        return CheckResult(
            name="System Audio Recording (dial)",
            ok=False,
            detail="audio helper not built",
            fix=_installer_fix(),
        )
    if probe is None:
        return CheckResult(
            name="System Audio Recording (dial)",
            ok=False,
            detail="audio helper probe failed",
            fix=_installer_fix(),
        )
    status = probe.get("system_audio", "unknown")
    if status == "ok":
        return CheckResult("System Audio Recording (dial)", True, "granted", "")
    return CheckResult(
        name="System Audio Recording (dial)",
        ok=False,
        detail=_TCC_STATUS_DETAIL.get(status, status),
        fix=(
            "System Settings → Privacy & Security → System Audio Recording Only → "
            "enable 'Operator'"
        ),
    )


def _check_faster_whisper_warm() -> CheckResult:
    """Run the same faster-whisper warmup operator does at dial meeting entry.

    Surfaces two failure modes at install/diagnostic time rather than mid-
    meeting:

      1. First-time model download — ~1.5GB from HuggingFace into
         ~/.cache/huggingface/. On a slow connection this can take
         100s+. Running it here means the user pays that cost at install,
         not at meeting-join. Warm-cache subsequent runs are 1-2s.

      2. CTranslate2 binary / architecture / disk issues — operator catches
         these cleanly at dial entry (falls back to chat-only), but the
         failure-now-vs-mid-meeting framing applies.

    S233 swapped this from `mlx-whisper` after two production crashes from
    MLX's async Metal command-buffer abort path. See HWK S227 + S233 in
    docs/agent-context.md. The MLX/Metal crash family no longer exists.

    Optional check: a failure here doesn't fail `operator doctor`'s exit
    code — dial still runs (chat-only), so this is a warning, not a blocker.
    """
    if sys.platform != "darwin":
        return CheckResult(
            "faster-whisper warmup",
            True,
            "skipped (dial audio is mac-only)",
            "",
            optional=True,
        )
    # Lazy imports so a missing dep doesn't break the other checks.
    try:
        import numpy as np
        from faster_whisper import WhisperModel
    except ImportError as e:
        return CheckResult(
            name="faster-whisper warmup",
            ok=False,
            detail=f"import failed: {e} — dial will run chat-only",
            fix=_installer_fix(),
            optional=True,
        )
    # Hint goes to stdout (not stderr) so the desktop-app harness doesn't
    # treat any stderr output as a failure and silence the result.
    # faster-whisper writes a download progress bar to fd 2 on first run,
    # so redirect the real file descriptor for the duration of the call.
    print(
        "    warming faster-whisper-large-v3-turbo "
        "(1-2s warm cache, up to 100s on first run — downloads ~1.5GB)…",
        flush=True,
    )
    t0 = time.monotonic()
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_stderr_fd = os.dup(2)
        os.dup2(devnull_fd, 2)
        os.close(devnull_fd)
        try:
            model = WhisperModel(
                "deepdml/faster-whisper-large-v3-turbo-ct2",
                device="cpu",
                compute_type="int8",
                cpu_threads=0,
            )
            segments, _info = model.transcribe(
                np.zeros(16000, dtype=np.float32),
                language="en",
                beam_size=WHISPER_BEAM_SIZE,
                vad_filter=False,
            )
            # Materialise the generator — faster-whisper does no compute
            # until you iterate.
            for _ in segments:
                pass
        finally:
            os.dup2(saved_stderr_fd, 2)
            os.close(saved_stderr_fd)
    except Exception as e:
        # Collapse multi-line exception text to the first line so the
        # checklist line stays readable.
        first_line = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return CheckResult(
            name="faster-whisper warmup",
            ok=False,
            detail=f"{first_line} — dial will run chat-only (no transcripts)",
            fix=(
                "check network (model downloads from HuggingFace on first run) and "
                "that ~/.cache/huggingface/ is writable. Re-run `operator doctor` to retry."
            ),
            optional=True,
        )
    elapsed = time.monotonic() - t0
    return CheckResult(
        "faster-whisper warmup",
        True,
        f"ready ({elapsed:.1f}s)",
        "",
    )


def _check_aec_binary() -> CheckResult:
    """aec3 speaker-bleed cleaner. Optional but recommended on built-in speakers."""
    binary = _aec_binary()
    if binary is not None:
        return CheckResult(
            "aec3 cleaner (dial)",
            True,
            f"installed at {binary}",
            "",
        )
    if shutil.which("cargo") is None:
        return CheckResult(
            name="aec3 cleaner (dial)",
            ok=False,
            detail="not installed and cargo missing — mic transcripts may include speaker bleed",
            fix=_installer_fix("install Rust first (https://rustup.rs/)"),
            optional=True,
        )
    return CheckResult(
        name="aec3 cleaner (dial)",
        ok=False,
        detail="not installed — mic transcripts may include speaker bleed",
        fix=_installer_fix(),
        optional=True,
    )


def _check_microphone(probe: dict[str, str] | None) -> CheckResult:
    """TCC Microphone — dial mic capture (AVAudioEngine.inputNode)."""
    if _audio_helper() is None or probe is None:
        # Same upstream cause as System Audio Recording — only report once.
        return CheckResult(
            name="Microphone (dial)",
            ok=False,
            detail="audio helper unavailable (see System Audio Recording check)",
            fix="",
        )
    status = probe.get("microphone", "unknown")
    if status == "ok":
        return CheckResult("Microphone (dial)", True, "granted", "")
    return CheckResult(
        name="Microphone (dial)",
        ok=False,
        detail=_TCC_STATUS_DETAIL.get(status, status),
        fix=(
            "System Settings → Privacy & Security → Microphone → "
            "enable for your terminal app, then quit and relaunch it"
        ),
    )


def _render_last_failure() -> None:
    """Print the post-failure snapshot (if one exists) verbatim.

    Doctor does NOT classify or translate the failure data into a fix —
    that's the model's job. We just dump the structured record so the
    outer Claude Code session reading `operator doctor`'s output has
    everything it needs to interpret what happened in plain language
    for the user (see the doctor SKILL.md for the interpretation
    instruction). Same shape as the other doctor sections: dump signals,
    let the model translate.

    Skips silently if no failure file exists or the file is unreadable —
    a clean run has nothing to say here.
    """
    path = Path(config.LAST_FAILURE_PATH)
    try:
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
    except (OSError, ValueError):
        return
    if not isinstance(data, dict):
        return

    print()
    print("Last meeting failure")
    print("────────────────────")
    # Single-line scalars first, then the multi-line tails. Use the
    # field's value verbatim — no abridgement or rewriting.
    scalar_fields = (
        "ts", "meeting_url", "meeting_slug",
        "exception_class", "phase", "message",
    )
    for key in scalar_fields:
        if key not in data:
            continue
        val = data[key]
        if key == "ts" and isinstance(val, (int, float)):
            try:
                age = max(0.0, time.time() - val)
                stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(val))
                print(f"  when:            {stamp}  ({_fmt_age(age)} ago)")
                continue
            except (OSError, ValueError):
                pass
        # Long single-line strings (message can be ~2KB) — print on its
        # own line below the label so it's readable.
        sval = str(val)
        if "\n" in sval or len(sval) > 60:
            print(f"  {key}:")
            for line in sval.splitlines() or [sval]:
                print(f"    {line}")
        else:
            print(f"  {key:<16} {sval}")

    for block_key, label in (
        ("pty_tail", "pty_tail"),
        ("operator_log_tail", "operator_log_tail"),
    ):
        if not data.get(block_key):
            continue
        print(f"  {label}:")
        for line in str(data[block_key]).splitlines():
            print(f"    {line}")


def _fmt_age(seconds: float) -> str:
    """Short human-readable age — '23s', '4m', '2h', '3d'. No locale, no commas."""
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def run_doctor() -> int:
    """Run every check, print a checklist, return shell exit code.

    Exit codes:
      0 — every check passed
      1 — at least one check failed

    Output format mirrors `brew doctor` / `flutter doctor`: one line
    per check, ✓ or ✗ glyph, name + detail, indented fix on failures.
    """
    checks = [
        _check_claude(),
        _check_meeting_record_mcp(),
        _check_chrome(),
        _check_git(),
        _check_cwd_trusted(),
    ]
    # Dial is macOS-only — TCC checks are meaningless elsewhere.
    if sys.platform == "darwin":
        probe = _probe_audio_helper()
        checks.append(_check_system_audio(probe))
        checks.append(_check_microphone(probe))
        checks.append(_check_aec_binary())
        checks.append(_check_faster_whisper_warm())

    print()
    print("operator doctor")
    print("───────────────")
    failures = 0
    warnings = 0
    for c in checks:
        if c.ok:
            glyph = "✓"
        elif c.optional:
            glyph = "!"
        else:
            glyph = "✗"
        print(f"  {glyph} {c.name}: {c.detail}")
        if not c.ok:
            if c.optional:
                warnings += 1
            else:
                failures += 1
            if c.fix:
                print(f"      fix: {c.fix}")
    print()

    # Post-failure context (if any) — printed before the verdict so the
    # model reading our output has the last failure's raw signals in
    # hand when it interprets the overall state for the user.
    _render_last_failure()
    print()

    if failures == 0 and warnings == 0:
        print("All checks passed. You're ready to dial.")
        return 0
    if failures == 0:
        word = "warning" if warnings == 1 else "warnings"
        print(f"{warnings} optional {word} above — dial will still run.")
        return 0
    word = "issue" if failures == 1 else "issues"
    print(f"{failures} {word} above. Fix and re-run `operator doctor`.")
    return 1
