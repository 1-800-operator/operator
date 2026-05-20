"""Optional branded meeting browser — gated behind OPERATOR_BRANDED_BROWSER=1.

Generates a re-branded, ad-hoc-signed copy of the user's Google Chrome —
"Operator Browser.app" with operator's icon + name — so the dial window is
visually distinct from the user's own Chrome (mitigates the "two identical
Chromes" confusion). Spike 14.37 proved this viable end-to-end; see
debug/14_37_branded_chrome_spike/FINDINGS.md for the full investigation.

OPT-IN by design: with the env var unset, the dial path launches real Chrome
exactly as before. Built lazily (build-if-missing) on the first dial when the
flag is set — ~10-30s one-time (an APFS clone + inside-out ad-hoc codesign),
then reused. Launch the result with --use-mock-keychain (the caller adds it).

Non-obvious requirements, each of which cost a launch in the spike:
  - `disable-library-validation` on EVERY executable: ad-hoc signing is
    teamless, and the hardened-runtime library-validation otherwise refuses to
    load the (now-teamless) framework ("different Team IDs").
  - strip Google's team-scoped entitlements (application-identifier, keychain
    groups) — a non-Google ad-hoc signer can't claim them.
  - sign the versioned framework INSIDE-OUT; `codesign --deep` corrupts it
    ("embedded framework contains modified or invalid version").
  - remove CFBundleIconName — Chrome's icon comes from an asset catalog
    (Assets.car) that overrides the legacy app.icns; without removing the key
    the icon swap is a silent no-op.
  - `xattr -cr` first (the icon copy adds resource forks codesign rejects).
  - drop the stale framework version (Chrome keeps the prior one mid-update)
    and GoogleUpdater (no Keystone in the brand).

NOT handled here (deferred until the approach is adopted): re-bake when the
installed Chrome version drifts, and moving generation to install time.
"""
from __future__ import annotations

import logging
import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

CHROME_APP = Path("/Applications/Google Chrome.app")
BRANDED_APP = Path.home() / ".operator" / "bin" / "Operator Browser.app"

_BUNDLE_ID = "com.1-800-operator.browser"
_NAME = "Operator Browser"

# operator's phone icon — installed helper first, in-tree dev copy as fallback.
_ICON_INSTALLED = (
    Path.home() / ".operator" / "bin" / "Operator.app"
    / "Contents" / "Resources" / "Operator.icns"
)
_ICON_DEV = (
    Path(__file__).resolve().parent.parent / "swift" / "Operator.app"
    / "Contents" / "Resources" / "Operator.icns"
)

_ENT_JIT = {
    "com.apple.security.cs.allow-jit": True,
    "com.apple.security.cs.disable-library-validation": True,
}
_ENT_HELPER = {"com.apple.security.cs.disable-library-validation": True}
_ENT_MAIN = {
    "com.apple.security.cs.disable-library-validation": True,
    "com.apple.security.device.audio-input": True,
    "com.apple.security.device.camera": True,
}


def _icon() -> Path | None:
    for p in (_ICON_INSTALLED, _ICON_DEV):
        if p.exists():
            return p
    return None


def _app_version(app: Path) -> str | None:
    """CFBundleShortVersionString of an .app (None if unreadable). Used to
    detect when the branded copy has drifted from the installed Chrome."""
    try:
        import plistlib
        with (app / "Contents" / "Info.plist").open("rb") as f:
            return plistlib.load(f).get("CFBundleShortVersionString")
    except Exception:  # noqa: BLE001
        return None


def _sign(target: Path, entitlements: dict | None = None) -> None:
    """ad-hoc codesign one item, hardened runtime, optional entitlements."""
    cmd = ["codesign", "--force", "--options", "runtime", "--sign", "-"]
    tmp = None
    if entitlements is not None:
        tmp = tempfile.NamedTemporaryFile(suffix=".plist", delete=False)
        tmp.write(plistlib.dumps(entitlements))
        tmp.close()
        cmd += ["--entitlements", tmp.name]
    cmd.append(str(target))
    try:
        subprocess.run(cmd, check=True, capture_output=True)
    finally:
        if tmp:
            os.unlink(tmp.name)


def _build() -> bool:
    chrome, icon = CHROME_APP, _icon()
    if not chrome.exists():
        log.warning("branded_browser: no Google Chrome at %s — cannot build", chrome)
        return False
    if icon is None:
        log.warning("branded_browser: operator icon (.icns) not found — cannot build")
        return False

    BRANDED_APP.parent.mkdir(parents=True, exist_ok=True)
    if BRANDED_APP.exists():
        shutil.rmtree(BRANDED_APP)

    # 1. clone (APFS copy-on-write — near-instant)
    subprocess.run(["ditto", str(chrome), str(BRANDED_APP)], check=True)

    # 2. rebrand id + name + icon
    info = str(BRANDED_APP / "Contents" / "Info.plist")
    subprocess.run(["plutil", "-replace", "CFBundleIdentifier", "-string", _BUNDLE_ID, info], check=True)
    subprocess.run(["plutil", "-replace", "CFBundleName", "-string", _NAME, info], check=True)
    subprocess.run(["plutil", "-replace", "CFBundleDisplayName", "-string", _NAME, info], check=True)
    # asset-catalog icon overrides app.icns — drop the key so app.icns wins.
    subprocess.run(["plutil", "-remove", "CFBundleIconName", info], check=False)
    shutil.copyfile(icon, BRANDED_APP / "Contents" / "Resources" / "app.icns")

    # 3. clean: xattrs, stale framework version(s), GoogleUpdater
    subprocess.run(["xattr", "-cr", str(BRANDED_APP)], check=False)
    fw = BRANDED_APP / "Contents" / "Frameworks" / "Google Chrome Framework.framework"
    cur = fw / "Versions" / "Current"
    ver = os.readlink(str(cur)) if cur.is_symlink() else None
    all_versions = [d.name for d in (fw / "Versions").iterdir() if d.name != "Current"]
    if ver is None:
        ver = sorted(all_versions)[-1]
    for d in (fw / "Versions").iterdir():
        if d.name not in ("Current", ver) and not d.is_symlink():
            shutil.rmtree(d)
    vdir = fw / "Versions" / ver
    helpers = vdir / "Helpers"
    if (helpers / "GoogleUpdater.app").exists():
        shutil.rmtree(helpers / "GoogleUpdater.app")

    # 4. sign inside-out (deepest first), ad-hoc
    for dylib in (vdir / "Libraries").rglob("*.dylib"):
        _sign(dylib)
    _sign(helpers / "Google Chrome Helper (Renderer).app", _ENT_JIT)
    _sign(helpers / "Google Chrome Helper (GPU).app", _ENT_JIT)
    _sign(helpers / "Google Chrome Helper.app", _ENT_HELPER)
    _sign(helpers / "Google Chrome Helper (Alerts).app", _ENT_HELPER)
    for x in ("chrome_crashpad_handler", "app_mode_loader", "web_app_shortcut_copier"):
        if (helpers / x).exists():
            _sign(helpers / x)
    _sign(vdir)
    _sign(BRANDED_APP, _ENT_MAIN)

    subprocess.run(
        ["codesign", "--verify", "--deep", "--strict", str(BRANDED_APP)],
        check=True, capture_output=True,
    )
    log.info("branded_browser: built + signed %s", BRANDED_APP)
    return True


def ensure_branded_browser() -> Path | None:
    """Return the branded app path, building (or re-baking) as needed.

    Build-if-missing, and **re-bake on Chrome-version drift**: the branded copy
    is a frozen clone of the Chrome it was built from, so when the user's real
    Chrome auto-updates we rebuild from the new version. Without this the copy
    silently rots — missing security patches and eventually breaking against
    Meet — with no auto-recovery. The drift check is cheap (two Info.plist
    reads); the rebuild only fires the first launch after a Chrome update
    (~10-30s, one-time per update).

    Returns None on any failure so the caller falls back to real Chrome. A
    re-bake that fails also returns None → fall back (the existing copy is left
    in place, but the caller uses system Chrome for this launch).
    """
    try:
        if BRANDED_APP.exists():
            branded_ver = _app_version(BRANDED_APP)
            chrome_ver = _app_version(CHROME_APP)
            if not (branded_ver and chrome_ver and branded_ver != chrome_ver):
                # current, or versions unreadable (don't churn on a bad read)
                return BRANDED_APP
            log.info(
                "branded_browser: Chrome updated %s → %s — re-baking",
                branded_ver, chrome_ver,
            )
        else:
            log.info("branded_browser: building Operator Browser.app (one-time, ~10-30s)…")
        return BRANDED_APP if _build() else None
    except Exception as e:  # noqa: BLE001 — any failure → fall back to real Chrome
        log.warning("branded_browser: build/re-bake failed (%s) — falling back to real Chrome", e)
        return None
