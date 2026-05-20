"""
Launch-time precision self-update for the operator CLI.

Operator's hot path scrapes the Google Meet / Google Chat DOM, which Google
changes without notice. Those fixes live in the Python wheel (attach_adapter,
chat_dom_js, cdp_ws). This module ships them to users automatically: at the top
of every `operator dial` / `wiretap`, BEFORE the meeting join, it checks a
remote component manifest and — if a newer *wheel* is published — swaps just the
wheel and re-execs into it. The heavy out-of-venv artifacts (Chromium ~520 MB,
the signed Swift helper, aec3) live outside the venv and are never touched by a
wheel swap; if THEY change, we only log a notice (re-running their install steps
mid-launch would pop TCC prompts — that stays a manual `install.sh` / update).

Spike: debug/14_35_selfupdate_spike/ (a wheel swap is ~0.16–0.6s warm).

────────────────────────────────────────────────────────────────────────────
SECURITY MODEL — the governing invariant is:

    Auto-update must be exactly as trustworthy as a fresh `install.sh` run,
    never a weaker channel.

install.sh already trusts "GitHub repo + HTTPS" (it `uv tool install`s a pinned
tag from github.com over TLS). Auto-update reuses that same anchor and adds NO
new one. Concretely:

  1. Source pinned + HTTPS-only. Manifest is fetched ONLY from the canonical
     raw.githubusercontent.com path over https; redirects that leave the GitHub
     host allowlist or downgrade to http are rejected (see _StrictRedirect).
  2. No production override. The manifest URL is a hardcoded constant. The
     OPERATOR_MANIFEST_URL override is honored ONLY under the explicit
     OPERATOR_SELFUPDATE_DEV=1 dev flag (used by tests), and logged loudly.
  3. Roll-forward only. We install a version strictly greater than installed —
     a stale/rolled-back manifest can withhold an update but can never force a
     DOWNGRADE to a known-vulnerable version.
  4. Ref is shape-validated. The install ref must be a `vX.Y.Z` tag or a 40-hex
     commit SHA (commit SHA recommended — immutable, defeats tag-movement).
     Anything else (a branch like `main`, an injection attempt) is refused.
  5. No shell, ever. uv is invoked with an argv list; the ref can never reach a
     shell, so a malformed manifest cannot inject a command.
  6. Bytes pinned when possible. If the manifest carries a wheel asset + sha256,
     we download it (HTTPS), verify the hash, and install the local file — the
     hash is the integrity gate (so following GitHub's CDN redirect for the
     asset is safe). Absent that, we fall back to the pinned git ref (identical
     trust to install.sh).
  7. Fail safe. The whole path is wrapped: any error, timeout, offline, lock
     contention, or a live meeting → log and proceed on the INSTALLED code. An
     update never blocks or breaks a join.
  8. Opt-out. OPERATOR_NO_SELFUPDATE=1 pins the user to their installed version.

Out of scope for v1 (documented upgrade path, not built): out-of-band code
signing with an offline key (the only thing that defends against full GitHub
repo compromise — but the initial install doesn't have it either, so adding it
to auto-update alone wouldn't change the trust floor), and client-side
health-check rollback. The v1 rollback lever is server-side: revert the
manifest `ref` and the next launch rolls forward to the good version.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from importlib import metadata

PKG = "1-800-operator"

# ── Trust anchors (hardcoded; the security of this whole module rests here) ──
_MANIFEST_HOST = "raw.githubusercontent.com"
_MANIFEST_URL = (
    f"https://{_MANIFEST_HOST}/1-800-operator/operator/main/release-manifest.json"
)
# GitHub serves release assets from github.com, then 302s to its object CDN.
# We host-pin the INITIAL asset URL to these; the redirect target is allowed to
# differ because the sha256 check (not the host) is the integrity gate.
_ASSET_HOSTS = {"github.com", "objects.githubusercontent.com", "release-assets.githubusercontent.com"}

# Accept a vX.Y.Z release tag or a full 40-hex commit SHA. Nothing else — no
# branch refs, no partial SHAs, no shell metacharacters.
_REF_RE = re.compile(r"^(v\d+\.\d+\.\d+|[0-9a-f]{40})$")

_FETCH_TIMEOUT_S = 5      # manifest GET
_DOWNLOAD_TIMEOUT_S = 90  # wheel asset GET
_SWAP_TIMEOUT_S = 180     # uv tool install ceiling
_MAX_MANIFEST_BYTES = 64 * 1024
_MAX_WHEEL_BYTES = 50 * 1024 * 1024

_REPO = "https://github.com/1-800-operator/operator.git"
_REEXEC_GUARD = "OPERATOR_SELFUPDATE_DONE"   # set before execv → run-once
_OPT_OUT = "OPERATOR_NO_SELFUPDATE"
_DEV_FLAG = "OPERATOR_SELFUPDATE_DEV"
_DEV_URL_OVERRIDE = "OPERATOR_MANIFEST_URL"

_OPERATOR_DIR = os.path.expanduser("~/.operator")
_DIAL_PID = os.path.join(_OPERATOR_DIR, "dial.pid")
_COMPONENTS = os.path.join(_OPERATOR_DIR, ".components.json")
_LOCK = os.path.join(_OPERATOR_DIR, ".selfupdate.lock")
_LOG = "/tmp/operator.log"


# ── breadcrumbs ─────────────────────────────────────────────────────────────
# Self-update runs before _run_dial configures file logging, so we append our
# own timestamped lines to the operator log directly (best-effort, never raises)
# and keep stdout clean (the desktop app expects a single status line from the
# daemonize step; see project_desktop_app_silences_nonzero_exit).
def _log(msg: str) -> None:
    try:
        with open(_LOG, "a") as f:
            f.write(f"{time.strftime('%H:%M:%S')} SELFUPDATE {msg}\n")
    except OSError:
        pass
    if sys.stderr.isatty():  # dev/terminal: also surface it live
        print(f"operator: {msg}", file=sys.stderr)


# ── version helpers ──────────────────────────────────────────────────────────
def parse_version(s: str) -> tuple[int, ...]:
    """`"v1.2.3"`/`"1.2.3"` → `(1,2,3)`. Malformed → `(0,)` so it never
    spuriously sorts ABOVE a well-formed version (keeps roll-forward safe)."""
    if not s:
        return (0,)
    try:
        return tuple(int(p) for p in str(s).strip().lstrip("v").split("."))
    except (ValueError, AttributeError):
        return (0,)


def valid_ref(ref: str) -> bool:
    return bool(ref) and bool(_REF_RE.match(ref))


def _https_host_ok(url: str, allowed: set[str]) -> bool:
    from urllib.parse import urlparse
    try:
        u = urlparse(url)
    except ValueError:
        return False
    return u.scheme == "https" and u.hostname in allowed


# ── strict, redirect-aware fetch for the manifest ────────────────────────────
class _StrictRedirect(urllib.request.HTTPRedirectHandler):
    """Reject any redirect that downgrades to http or leaves the GitHub host
    allowlist — so the manifest fetch can't be bounced to an attacker host."""
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _https_host_ok(newurl, {_MANIFEST_HOST}):
            raise urllib.request.HTTPError(
                newurl, code, "self-update: refusing off-allowlist redirect", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _manifest_url() -> str:
    if os.environ.get(_DEV_FLAG) == "1":
        override = os.environ.get(_DEV_URL_OVERRIDE)
        if override:
            _log(f"DEV MODE: manifest source overridden to {override} "
                 f"({_DEV_FLAG}=1) — never set this in production")
            return override
    return _MANIFEST_URL


def fetch_manifest(url: str | None = None) -> dict | None:
    """GET + parse the component manifest. None on any failure (offline, 404,
    bad JSON, oversize) — a courtesy check, never fatal."""
    url = url or _manifest_url()
    dev = os.environ.get(_DEV_FLAG) == "1"
    if not dev and not _https_host_ok(url, {_MANIFEST_HOST}):
        _log(f"refusing non-allowlisted manifest URL: {url}")
        return None
    try:
        # In prod, build an opener with the strict redirect guard. In dev we
        # allow file:// (tests) by using the default opener.
        opener = (urllib.request.build_opener()
                  if dev else urllib.request.build_opener(_StrictRedirect()))
        req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
        with opener.open(req, timeout=_FETCH_TIMEOUT_S) as resp:
            raw = resp.read(_MAX_MANIFEST_BYTES + 1)
        if len(raw) > _MAX_MANIFEST_BYTES:
            _log("manifest too large — ignoring")
            return None
        return json.loads(raw)
    except Exception as e:
        _log(f"manifest fetch failed ({e}) — staying on installed code")
        return None


# ── installed state ──────────────────────────────────────────────────────────
def installed_versions() -> dict[str, str]:
    """Per-component installed versions. The wheel is authoritative from package
    metadata; helper/aec3 come from the install-time marker (they ship lockstep
    with the wheel today, so they default to the wheel version if unrecorded)."""
    try:
        wheel = metadata.version(PKG)
    except metadata.PackageNotFoundError:
        wheel = "0.0.0"
    helper = aec3 = wheel
    try:
        with open(_COMPONENTS) as f:
            rec = json.load(f)
        helper = str(rec.get("helper", helper))
        aec3 = str(rec.get("aec3", aec3))
    except (OSError, json.JSONDecodeError):
        pass
    return {"wheel": wheel, "helper": helper, "aec3": aec3}


def plan_swap(installed: dict[str, str], target: dict[str, str]) -> list[str]:
    """Components whose target is STRICTLY newer than installed (roll-forward
    only — never returns a component for a downgrade)."""
    return [c for c, t in target.items()
            if parse_version(t) > parse_version(installed.get(c, "0.0.0"))]


def record_components(versions: dict[str, str]) -> None:
    """Persist component versions to the marker (called by install.sh and after
    a successful swap)."""
    try:
        os.makedirs(_OPERATOR_DIR, exist_ok=True)
        with open(_COMPONENTS, "w") as f:
            json.dump(versions, f)
    except OSError as e:
        _log(f"could not write components marker: {e}")


# ── the swap ─────────────────────────────────────────────────────────────────
def _verify_sha256(path: str, expected: str) -> bool:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest().lower() == str(expected).lower()


def _download_wheel(url: str, sha256: str) -> str | None:
    """Download a wheel asset (HTTPS, host-pinned start) into a temp dir UNDER
    ITS REAL PEP 427 FILENAME and verify its sha256. Returns the path on
    success, else None (caller falls back to the git-ref install). The hash —
    not the redirect host — is the integrity gate, so following GitHub's CDN
    redirect is safe. The caller removes the returned file's parent dir.

    The filename must be preserved: `uv tool install <file.whl>` derives the
    distribution name + version from the wheel FILENAME, so a random temp name
    (mkstemp) makes uv exit non-zero. The basename is taken from the (host-
    pinned) URL and validated to a strict wheel-name shape before use."""
    from urllib.parse import urlparse
    dev = os.environ.get(_DEV_FLAG) == "1"
    if not dev and not _https_host_ok(url, _ASSET_HOSTS):
        _log(f"refusing non-allowlisted wheel URL: {url}")
        return None
    fname = os.path.basename(urlparse(url).path)
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._+-]*\.whl$", fname):
        _log(f"wheel URL basename is not a safe .whl filename: {fname!r}")
        return None
    os.makedirs(_OPERATOR_DIR, exist_ok=True)
    tmpdir = tempfile.mkdtemp(prefix="operator-wheel-", dir=_OPERATOR_DIR)
    dest = os.path.join(tmpdir, fname)
    ok = False
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT_S) as resp, open(dest, "wb") as out:
            total = 0
            for chunk in iter(lambda: resp.read(1 << 20), b""):
                total += len(chunk)
                if total > _MAX_WHEEL_BYTES:
                    _log("wheel exceeds size ceiling — aborting download")
                    return None
                out.write(chunk)
        if not _verify_sha256(dest, sha256):
            _log("wheel sha256 MISMATCH — refusing to install (possible tampering)")
            return None
        _log("wheel sha256 verified")
        ok = True
        return dest
    except Exception as e:
        _log(f"wheel download failed ({e}) — falling back to git ref")
        return None
    finally:
        if not ok:  # remove the temp dir on every non-success path
            shutil.rmtree(tmpdir, ignore_errors=True)


def _uv_install(target: list[str]) -> bool:
    uv = shutil.which("uv")
    if not uv:
        _log("uv not on PATH — cannot swap; proceeding on installed code")
        return False
    cmd = [uv, "tool", "install", "--force", *target]
    _log(f"swapping: {' '.join(cmd)}")
    t0 = time.time()
    try:
        subprocess.run(cmd, timeout=_SWAP_TIMEOUT_S, check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except subprocess.TimeoutExpired:
        _log(f"swap exceeded {_SWAP_TIMEOUT_S}s — aborting, proceeding on installed code")
        return False
    except (subprocess.CalledProcessError, OSError) as e:
        _log(f"swap failed ({e}) — proceeding on installed code")
        return False
    _log(f"swap ok in {time.time() - t0:.2f}s")
    return True


def swap_wheel(manifest: dict) -> bool:
    """Install the wheel named by the manifest. Prefers the byte-pinned asset
    (download + sha256 verify), falls back to the pinned git ref."""
    ref = manifest.get("ref")
    if not valid_ref(ref):
        _log(f"manifest ref {ref!r} is not a vX.Y.Z tag or 40-hex SHA — refusing")
        return False

    wheel = manifest.get("wheel") or {}
    url, sha = wheel.get("url"), wheel.get("sha256")
    if url and sha:
        local = _download_wheel(url, sha)
        if local:
            try:
                if _uv_install([local]):
                    return True
                # Local install failed (e.g. uv couldn't parse the wheel) —
                # fall back to the pinned git ref rather than giving up.
                _log("local-wheel install failed — falling back to pinned git ref")
            finally:
                shutil.rmtree(os.path.dirname(local), ignore_errors=True)
        # download/verify/install failed → fall through to git ref
    return _uv_install([f"git+{_REPO}@{ref}"])


# ── re-exec ──────────────────────────────────────────────────────────────────
def reexec(argv: list[str]) -> None:
    """Replace this process with the freshly-swapped operator so the meeting
    runs on the new wheel. Guarded so the successor skips the update path."""
    os.environ[_REEXEC_GUARD] = "1"
    op = shutil.which("operator") or sys.argv[0]
    _log(f"re-exec → {op} {' '.join(argv)}")
    sys.stdout.flush()
    sys.stderr.flush()
    os.execv(op, [op, *argv])


# ── guards ───────────────────────────────────────────────────────────────────
def selfupdate_disabled() -> bool:
    return os.environ.get(_OPT_OUT) == "1" or bool(os.environ.get(_REEXEC_GUARD))


def meeting_in_progress() -> bool:
    return os.path.exists(_DIAL_PID)


class _Lock:
    """Best-effort non-blocking flock so two concurrent launches can't run
    `uv tool install` over each other. If we can't get it, someone else is
    updating — we proceed on installed code rather than wait."""
    def __init__(self):
        self._fh = None

    def __enter__(self):
        import fcntl
        try:
            os.makedirs(_OPERATOR_DIR, exist_ok=True)
            self._fh = open(_LOCK, "w")
            fcntl.flock(self._fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return True
        except (OSError, BlockingIOError):
            if self._fh:
                self._fh.close()
                self._fh = None
            return False

    def __exit__(self, *exc):
        if self._fh:
            try:
                import fcntl
                fcntl.flock(self._fh, fcntl.LOCK_UN)
            except OSError:
                pass
            self._fh.close()


# ── orchestrator ─────────────────────────────────────────────────────────────
def maybe_self_update(argv: list[str]) -> None:
    """Entry point, called from main() for meeting-entry commands BEFORE any
    fork/lock. May os.execv into a newer wheel and never return. Any failure is
    swallowed and we proceed on the installed code — an update never blocks a
    meeting."""
    try:
        if selfupdate_disabled():
            return
        if meeting_in_progress():
            _log("a meeting is live (dial.pid present) — skipping update")
            return

        manifest = fetch_manifest()
        if not manifest:
            return
        target = manifest.get("components") or {}
        if not isinstance(target, dict):
            return
        installed = installed_versions()
        changed = plan_swap(installed, target)
        if not changed:
            return

        heavy = sorted({"helper", "aec3"} & set(changed))
        if heavy:
            # Re-running the helper/aec3 install steps would pop TCC prompts
            # mid-launch — that stays a manual install.sh / update. Just notify.
            _log(f"components {heavy} updated upstream — run install.sh / "
                 f"/operator:update to refresh them (kept manual: avoids TCC "
                 f"prompts at launch)")
        if "wheel" not in changed:
            return

        _log(f"wheel {installed['wheel']} → {target.get('wheel')} "
             f"(ref {manifest.get('ref')})")
        with _Lock() as got:
            if not got:
                _log("another launch holds the update lock — proceeding on installed")
                return
            if not swap_wheel(manifest):
                return
            # Record the new wheel version; keep helper/aec3 as-is (untouched).
            new = dict(installed)
            new["wheel"] = str(target.get("wheel"))
            record_components(new)
        reexec(argv)
    except Exception as e:  # never let an update failure reach the meeting
        _log(f"unexpected error ({e!r}) — proceeding on installed code")
