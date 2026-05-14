"""`operator doctor` — diagnostic checker (Phase 14.19.5).

Analogous to `brew doctor` / `flutter doctor`. Prints a checklist of
world-readiness items, marks each pass/fail, and emits the exact fix
command for failures. No interactive prompts, no fixups — purely
read-only diagnostics. Exits 0 if everything is green, 1 if any check
fails (so CI / scripts can gate on it).

Composition: each check is a tiny pure function returning a
`CheckResult(name, ok, detail, fix)`. `run_doctor()` calls them in
order and renders. macOS-only TCC checks (Screen Recording + Microphone
for slip's audio helper, Phase 14.20.4) skip on other platforms.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from _1_800_operator.pipeline.claude_code_import import _probe_claude_code

CHROME_PATH = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
CHROME_INSTALL_URL = "https://www.google.com/chrome/"


def chrome_installed() -> bool:
    """True on non-darwin (no system-Chrome dependency) or when the binary exists."""
    if sys.platform != "darwin":
        return True
    return CHROME_PATH.exists()

# Slip's audio helper. Production is the signed+notarized .app produced by
# scripts/build_signed_helper.sh (only this path can capture system audio).
# Dev fallback is the raw swiftc-built binary in-tree (mic-only). Production
# wins when both exist; mirrors attach_adapter.py:_AUDIO_HELPER_INSTALLED.
_AUDIO_HELPER_INSTALLED = (
    Path.home() / ".operator" / "bin" / "operator-audio-capture.app"
    / "Contents" / "MacOS" / "operator-audio-capture"
)
_AUDIO_HELPER_DEV = Path(__file__).resolve().parent.parent / "swift" / "operator-audio-capture"

# AEC3 speaker-bleed cleaner. Production is the cargo-built binary installed
# under ~/.operator/bin/; dev fallback is the in-tree build. Mirrors
# attach_adapter.py:_AEC_BINARY_INSTALLED. Optional — missing AEC isn't fatal,
# slip just runs without the bleed defense.
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
    """Real Google Chrome.app present (macOS) — required by slip's CDP attach."""
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
        r = subprocess.run(
            [str(helper), "--probe"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        return json.loads(r.stdout.strip())
    except json.JSONDecodeError:
        return None


def _check_screen_recording(probe: dict[str, str] | None) -> CheckResult:
    """TCC Screen Recording — slip system-audio capture (ScreenCaptureKit).

    Apple gates SCK audio behind the same TCC service as video, even when
    capturing audio only. Without it the helper exits with the silent-
    failure mode (preflight true, callbacks never fire).
    """
    if _audio_helper() is None:
        return CheckResult(
            name="Screen Recording (slip)",
            ok=False,
            detail="audio helper not built",
            fix="re-run install.sh to build operator-audio-capture",
        )
    if probe is None:
        return CheckResult(
            name="Screen Recording (slip)",
            ok=False,
            detail="audio helper probe failed",
            fix="re-run install.sh to rebuild operator-audio-capture",
        )
    status = probe.get("screen_recording", "unknown")
    if status == "ok":
        return CheckResult("Screen Recording (slip)", True, "granted", "")
    return CheckResult(
        name="Screen Recording (slip)",
        ok=False,
        detail=_TCC_STATUS_DETAIL.get(status, status),
        fix=(
            "System Settings → Privacy & Security → Screen Recording → "
            "enable for your terminal app, then quit and relaunch it"
        ),
    )


def _check_mlx_whisper_warm() -> CheckResult:
    """Run the same mlx-whisper warmup operator does at slip meeting entry.

    Surfaces two failure modes at install/diagnostic time rather than mid-
    meeting:

      1. `[metal::Device] Unable to build metal library from source` — MLX
         can't compile its Metal shader library against the local Xcode
         toolchain. operator catches this exception cleanly and falls back
         to chat-only mode (no transcripts), but MLX *also* spawns an
         internal helper to invoke the Metal compiler, and that helper can
         crash as a fork-child, producing a noisy macOS crash dialog. By
         running this check at doctor time, the user hits any breakage in
         a diagnostic context (manageable) instead of mid-call (panic).

      2. Model download / disk / network failures on first run.

    Optional check: a failure here doesn't fail `operator doctor`'s exit
    code — slip still runs (chat-only), so this is a warning, not a
    blocker. Cold-cache first run takes 3-20s while MLX compiles Metal
    shaders; subsequent runs return in under a second.
    """
    if sys.platform != "darwin":
        return CheckResult(
            "mlx-whisper warmup",
            True,
            "skipped (slip audio is mac-only)",
            "",
            optional=True,
        )
    # Lazy imports so a missing mlx dep doesn't break the other checks.
    try:
        import numpy as np
        import mlx_whisper
    except ImportError as e:
        return CheckResult(
            name="mlx-whisper warmup",
            ok=False,
            detail=f"import failed: {e} — slip will run chat-only",
            fix="re-run install.sh",
            optional=True,
        )
    # Hint goes to stdout (not stderr) so the desktop-app harness doesn't
    # treat any stderr output as a failure and silence the result.
    # tqdm from mlx_whisper writes to fd 2 directly, so redirect the real
    # file descriptor to /dev/null for the duration of the call.
    print("    warming mlx-whisper-base (3-20s on first run)…", flush=True)
    t0 = time.monotonic()
    try:
        devnull_fd = os.open(os.devnull, os.O_WRONLY)
        saved_stderr_fd = os.dup(2)
        os.dup2(devnull_fd, 2)
        os.close(devnull_fd)
        try:
            mlx_whisper.transcribe(
                np.zeros(16000, dtype=np.float32),
                path_or_hf_repo="mlx-community/whisper-base-mlx",
                language="en",
            )
        finally:
            os.dup2(saved_stderr_fd, 2)
            os.close(saved_stderr_fd)
    except Exception as e:
        # Collapse multi-line exception text to the first line so the
        # checklist line stays readable.
        first_line = str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__
        return CheckResult(
            name="mlx-whisper warmup",
            ok=False,
            detail=f"{first_line} — slip will run chat-only (no transcripts)",
            fix=(
                "often transient (Metal-XPC compile race): retry `operator doctor`. "
                "If it persists: install Xcode Command Line Tools (`xcode-select --install`), "
                "or pin to Python 3.10-3.13 (mlx Metal-shader compile is flakier on bleeding-edge "
                "Python builds)."
            ),
            optional=True,
        )
    elapsed = time.monotonic() - t0
    return CheckResult(
        "mlx-whisper warmup",
        True,
        f"ready ({elapsed:.1f}s)",
        "",
    )


def _check_aec_binary() -> CheckResult:
    """aec3 speaker-bleed cleaner. Optional but recommended on built-in speakers."""
    binary = _aec_binary()
    if binary is not None:
        return CheckResult(
            "aec3 cleaner (slip)",
            True,
            f"installed at {binary}",
            "",
        )
    if shutil.which("cargo") is None:
        return CheckResult(
            name="aec3 cleaner (slip)",
            ok=False,
            detail="not installed and cargo missing — mic transcripts may include speaker bleed",
            fix="install Rust (https://rustup.rs/), then re-run install.sh",
            optional=True,
        )
    return CheckResult(
        name="aec3 cleaner (slip)",
        ok=False,
        detail="not installed — mic transcripts may include speaker bleed",
        fix="re-run install.sh to build aec3",
        optional=True,
    )


def _check_microphone(probe: dict[str, str] | None) -> CheckResult:
    """TCC Microphone — slip mic capture (AVAudioEngine.inputNode)."""
    if _audio_helper() is None or probe is None:
        # Same upstream cause as Screen Recording — only report once.
        return CheckResult(
            name="Microphone (slip)",
            ok=False,
            detail="audio helper unavailable (see Screen Recording check)",
            fix="",
        )
    status = probe.get("microphone", "unknown")
    if status == "ok":
        return CheckResult("Microphone (slip)", True, "granted", "")
    return CheckResult(
        name="Microphone (slip)",
        ok=False,
        detail=_TCC_STATUS_DETAIL.get(status, status),
        fix=(
            "System Settings → Privacy & Security → Microphone → "
            "enable for your terminal app, then quit and relaunch it"
        ),
    )


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
        _check_chrome(),
        _check_git(),
    ]
    # Slip is macOS-only — TCC checks are meaningless elsewhere.
    if sys.platform == "darwin":
        probe = _probe_audio_helper()
        checks.append(_check_screen_recording(probe))
        checks.append(_check_microphone(probe))
        checks.append(_check_aec_binary())
        checks.append(_check_mlx_whisper_warm())

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

    if failures == 0 and warnings == 0:
        print("All checks passed. You're ready to slip.")
        return 0
    if failures == 0:
        word = "warning" if warnings == 1 else "warnings"
        print(f"{warnings} optional {word} above — slip will still run.")
        return 0
    word = "issue" if failures == 1 else "issues"
    print(f"{failures} {word} above. Fix and re-run `operator doctor`.")
    return 1
