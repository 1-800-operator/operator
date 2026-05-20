"""
Unit tests for launch-time precision self-update (pipeline/selfupdate.py).

Focus is the SECURITY-CRITICAL logic and the fail-safe orchestration — the
parts that decide what gets installed and whether a meeting is ever blocked.
The actual `uv tool install` + `os.execv` are mocked: running them would
mutate the dev machine's install and replace the test process.

What this exercises:
  - version parse + roll-forward-only (downgrade refusal)
  - ref shape validation (branch / injection / partial-SHA refusal)
  - URL https + host pinning, strict-redirect rejection
  - manifest fetch: prod refuses off-allowlist; dev allows file://; oversize cap
  - sha256 verification (match / mismatch)
  - installed_versions marker read + legacy fallback
  - plan_swap precision (wheel-only vs heavy components)
  - guards: opt-out, re-exec guard, mid-meeting, lock contention
  - swap_wheel: invalid ref blocks install; git-ref vs hash-pinned-wheel paths
  - maybe_self_update orchestration end-to-end (never raises, never installs
    on the no-op / guarded / up-to-date paths)

Live behavior (real uv swap timing + blast radius) is covered by the spike at
debug/14_35_selfupdate_spike/.
"""
import json
import os
import sys
import tempfile
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from _1_800_operator.pipeline import selfupdate as su


# ── small patch helper ───────────────────────────────────────────────────────
class patched:
    """Context manager: temporarily set module attrs / env, restore on exit."""
    def __init__(self, **attrs):
        self.attrs = attrs
        self.saved = {}

    def __enter__(self):
        for k, v in self.attrs.items():
            self.saved[k] = getattr(su, k)
            setattr(su, k, v)
        return self

    def __exit__(self, *exc):
        for k, v in self.saved.items():
            setattr(su, k, v)


def _clear_env():
    for k in (su._OPT_OUT, su._REEXEC_GUARD, su._DEV_FLAG, su._DEV_URL_OVERRIDE):
        os.environ.pop(k, None)


# ── version + ref ─────────────────────────────────────────────────────────────
def test_parse_version():
    assert su.parse_version("v1.2.3") == (1, 2, 3)
    assert su.parse_version("1.2.3") == (1, 2, 3)
    assert su.parse_version("") == (0,)
    assert su.parse_version("garbage") == (0,)
    # malformed must never sort above well-formed (keeps roll-forward safe)
    assert su.parse_version("garbage") < su.parse_version("0.0.1")
    print("  parse_version: OK")


def test_valid_ref():
    assert su.valid_ref("v0.1.38")
    assert su.valid_ref("a" * 40)              # full commit SHA
    assert not su.valid_ref("main")            # branch refused
    assert not su.valid_ref("v1.2")            # partial version refused
    assert not su.valid_ref("a" * 39)          # partial SHA refused
    assert not su.valid_ref("v1.0.0; rm -rf /")  # injection refused
    assert not su.valid_ref("v1.0.0 && curl evil")
    assert not su.valid_ref("")
    assert not su.valid_ref(None)
    print("  valid_ref: OK")


# ── URL pinning ───────────────────────────────────────────────────────────────
def test_https_host_ok():
    H = {"raw.githubusercontent.com"}
    assert su._https_host_ok("https://raw.githubusercontent.com/x", H)
    assert not su._https_host_ok("http://raw.githubusercontent.com/x", H)  # downgrade
    assert not su._https_host_ok("https://evil.com/x", H)                  # wrong host
    assert not su._https_host_ok("file:///etc/passwd", H)                  # scheme
    assert not su._https_host_ok("https://raw.githubusercontent.com.evil.com/x", H)
    print("  _https_host_ok: OK")


def test_strict_redirect_rejects_off_allowlist():
    h = su._StrictRedirect()
    req = urllib.request.Request("https://raw.githubusercontent.com/a")
    raised = False
    try:
        h.redirect_request(req, None, 302, "Found", {}, "http://evil.com/a")
    except urllib.request.HTTPError:
        raised = True
    assert raised, "redirect to off-allowlist host must raise"
    print("  _StrictRedirect rejects off-allowlist: OK")


# ── manifest fetch ────────────────────────────────────────────────────────────
def test_fetch_manifest_prod_refuses_off_allowlist():
    _clear_env()
    # No dev flag → a non-github URL must be refused (returns None) and must not
    # even attempt a network call.
    assert su.fetch_manifest("https://evil.com/manifest.json") is None
    assert su.fetch_manifest("http://raw.githubusercontent.com/x") is None
    print("  fetch_manifest prod refuses off-allowlist: OK")


def test_fetch_manifest_dev_allows_file_url():
    _clear_env()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump({"ref": "v0.1.38", "components": {"wheel": "0.1.38"}}, f)
        path = f.name
    try:
        os.environ[su._DEV_FLAG] = "1"
        m = su.fetch_manifest(f"file://{path}")
        assert m and m["ref"] == "v0.1.38"
    finally:
        _clear_env()
        os.unlink(path)
    print("  fetch_manifest dev allows file://: OK")


def test_fetch_manifest_oversize_refused():
    _clear_env()
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        f.write("x" * (su._MAX_MANIFEST_BYTES + 10))
        path = f.name
    try:
        os.environ[su._DEV_FLAG] = "1"
        assert su.fetch_manifest(f"file://{path}") is None
    finally:
        _clear_env()
        os.unlink(path)
    print("  fetch_manifest oversize refused: OK")


# ── sha256 ────────────────────────────────────────────────────────────────────
def test_verify_sha256():
    import hashlib
    with tempfile.NamedTemporaryFile("wb", delete=False) as f:
        f.write(b"hello operator")
        path = f.name
    try:
        good = hashlib.sha256(b"hello operator").hexdigest()
        assert su._verify_sha256(path, good)
        assert su._verify_sha256(path, good.upper())          # case-insensitive
        assert not su._verify_sha256(path, "0" * 64)          # mismatch
    finally:
        os.unlink(path)
    print("  _verify_sha256: OK")


# ── installed state ───────────────────────────────────────────────────────────
def test_installed_versions_marker_and_fallback():
    with tempfile.TemporaryDirectory() as d:
        marker = os.path.join(d, ".components.json")
        with patched(_COMPONENTS=marker):
            # no marker → helper/aec3 fall back to the wheel version
            v = su.installed_versions()
            assert v["helper"] == v["wheel"] == v["aec3"]
            # with marker → helper/aec3 read from it
            with open(marker, "w") as f:
                json.dump({"helper": "0.2.0", "aec3": "0.3.0"}, f)
            v = su.installed_versions()
            assert v["helper"] == "0.2.0" and v["aec3"] == "0.3.0"
    print("  installed_versions marker + fallback: OK")


# ── plan precision ────────────────────────────────────────────────────────────
def test_plan_swap_precision():
    inst = {"wheel": "0.1.37", "helper": "0.1.30", "aec3": "0.1.30"}
    # pure DOM fix: only wheel newer
    assert su.plan_swap(inst, {"wheel": "0.1.38", "helper": "0.1.30", "aec3": "0.1.30"}) == ["wheel"]
    # helper bump too
    p = su.plan_swap(inst, {"wheel": "0.1.38", "helper": "0.1.38", "aec3": "0.1.30"})
    assert set(p) == {"wheel", "helper"}
    # nothing newer
    assert su.plan_swap(inst, inst) == []
    print("  plan_swap precision: OK")


# ── guards ────────────────────────────────────────────────────────────────────
def test_selfupdate_disabled():
    _clear_env()
    assert not su.selfupdate_disabled()
    os.environ[su._OPT_OUT] = "1"
    assert su.selfupdate_disabled()
    _clear_env()
    os.environ[su._REEXEC_GUARD] = "1"
    assert su.selfupdate_disabled()
    _clear_env()
    print("  selfupdate_disabled (opt-out + re-exec guard): OK")


def test_meeting_in_progress():
    with tempfile.TemporaryDirectory() as d:
        pid = os.path.join(d, "dial.pid")
        with patched(_DIAL_PID=pid):
            assert not su.meeting_in_progress()
            open(pid, "w").close()
            assert su.meeting_in_progress()
    print("  meeting_in_progress: OK")


# ── swap_wheel paths ──────────────────────────────────────────────────────────
def test_swap_wheel_refuses_bad_ref():
    calls = []
    with patched(_uv_install=lambda t: calls.append(t) or True):
        assert su.swap_wheel({"ref": "main"}) is False
        assert su.swap_wheel({"ref": "v1.0.0; rm"}) is False
    assert calls == [], "no install may be attempted for a bad ref"
    print("  swap_wheel refuses bad ref (no install attempted): OK")


def test_swap_wheel_git_ref_path():
    calls = []
    with patched(_uv_install=lambda t: calls.append(t) or True):
        assert su.swap_wheel({"ref": "v0.1.38"}) is True
    assert len(calls) == 1 and calls[0][0].startswith("git+") and calls[0][0].endswith("@v0.1.38")
    print("  swap_wheel git-ref path: OK")


def _fake_wheel_in_tmpdir():
    """A properly-named wheel in its own temp dir (swap_wheel rmtrees the dir)."""
    d = tempfile.mkdtemp()
    p = os.path.join(d, "1_800_operator-0.1.40-py3-none-any.whl")
    open(p, "w").close()
    return p


def test_swap_wheel_hash_pinned_path():
    calls = []
    fake = _fake_wheel_in_tmpdir()
    with patched(_uv_install=lambda t: calls.append(t) or True,
                 _download_wheel=lambda url, sha: fake):
        ok = su.swap_wheel({"ref": "v0.1.40",
                            "wheel": {"url": "https://github.com/x.whl", "sha256": "ab"}})
    import shutil as _sh; _sh.rmtree(os.path.dirname(fake), ignore_errors=True)
    assert ok and calls == [[fake]], "verified wheel must be installed locally"
    print("  swap_wheel hash-pinned wheel path: OK")


def test_swap_wheel_falls_back_when_download_fails():
    calls = []
    with patched(_uv_install=lambda t: calls.append(t) or True,
                 _download_wheel=lambda url, sha: None):  # download/verify failed
        ok = su.swap_wheel({"ref": "v0.1.40",
                            "wheel": {"url": "https://github.com/x.whl", "sha256": "ab"}})
    assert ok and calls[0][0].startswith("git+"), "must fall back to pinned git ref"
    print("  swap_wheel falls back to git ref on download failure: OK")


def test_swap_wheel_falls_back_when_local_install_fails():
    # The bug found in the live test: the local-wheel install failed and we gave
    # up instead of trying the git ref. It must now fall back.
    calls = []
    fake = _fake_wheel_in_tmpdir()

    def fake_install(t):
        calls.append(t)
        return not str(t[0]).endswith(".whl")  # local .whl fails; git-ref succeeds

    with patched(_uv_install=fake_install, _download_wheel=lambda url, sha: fake):
        ok = su.swap_wheel({"ref": "v0.1.40",
                            "wheel": {"url": "https://github.com/x.whl", "sha256": "ab"}})
    import shutil as _sh; _sh.rmtree(os.path.dirname(fake), ignore_errors=True)
    assert ok, "must succeed via git-ref fallback after local install fails"
    assert len(calls) == 2 and calls[0] == [fake] and calls[1][0].startswith("git+")
    print("  swap_wheel falls back to git-ref when local install fails: OK")


def test_download_wheel_rejects_bad_filename():
    # The other live-test bug: a non-PEP427 filename breaks uv. github host is
    # allowed, but a basename that isn't a .whl must be refused before any
    # network call (and before it can reach uv).
    _clear_env()
    r = su._download_wheel(
        "https://github.com/1-800-operator/operator/releases/download/v0.1.40/notawheel", "ab")
    assert r is None
    print("  _download_wheel rejects non-.whl basename: OK")


# ── orchestration ─────────────────────────────────────────────────────────────
def _orchestration_harness(*, manifest, installed, meeting=False, opt_out=False):
    """Run maybe_self_update with everything mocked; return what it did."""
    _clear_env()
    if opt_out:
        os.environ[su._OPT_OUT] = "1"
    rec = {"installs": [], "reexec": [], "recorded": []}

    class DummyLock:
        def __enter__(self): return True
        def __exit__(self, *a): pass

    with patched(
        fetch_manifest=lambda url=None: manifest,
        installed_versions=lambda: installed,
        meeting_in_progress=lambda: meeting,
        _uv_install=lambda t: rec["installs"].append(t) or True,
        _download_wheel=lambda url, sha: None,   # force git-ref path
        reexec=lambda argv: rec["reexec"].append(argv),
        record_components=lambda v: rec["recorded"].append(v),
        _Lock=DummyLock,
    ):
        su.maybe_self_update(["dial", "claude", "https://meet.google.com/abc"])
    _clear_env()
    return rec


def test_orchestration_happy_path_swaps_and_reexecs():
    rec = _orchestration_harness(
        manifest={"ref": "v0.1.38", "components": {"wheel": "0.1.38", "helper": "0.1.30", "aec3": "0.1.30"}},
        installed={"wheel": "0.1.37", "helper": "0.1.30", "aec3": "0.1.30"},
    )
    assert len(rec["installs"]) == 1, "wheel swap should run"
    assert len(rec["reexec"]) == 1, "should re-exec into new wheel"
    assert rec["recorded"] and rec["recorded"][0]["wheel"] == "0.1.38"
    # helper/aec3 must be preserved untouched in the recorded marker
    assert rec["recorded"][0]["helper"] == "0.1.30"
    print("  orchestration happy path swaps + re-execs: OK")


def test_orchestration_up_to_date_noop():
    rec = _orchestration_harness(
        manifest={"ref": "v0.1.38", "components": {"wheel": "0.1.38"}},
        installed={"wheel": "0.1.38", "helper": "0.1.38", "aec3": "0.1.38"},
    )
    assert rec["installs"] == [] and rec["reexec"] == []
    print("  orchestration up-to-date no-op: OK")


def test_orchestration_heavy_only_does_not_swap():
    # helper bumped but wheel unchanged → must NOT swap or re-exec (heavy stays manual)
    rec = _orchestration_harness(
        manifest={"ref": "v0.1.38", "components": {"wheel": "0.1.37", "helper": "0.1.99", "aec3": "0.1.30"}},
        installed={"wheel": "0.1.37", "helper": "0.1.30", "aec3": "0.1.30"},
    )
    assert rec["installs"] == [] and rec["reexec"] == []
    print("  orchestration heavy-only does not swap: OK")


def test_orchestration_midmeeting_guard():
    rec = _orchestration_harness(
        manifest={"ref": "v0.1.38", "components": {"wheel": "0.1.38"}},
        installed={"wheel": "0.1.37", "helper": "0.1.30", "aec3": "0.1.30"},
        meeting=True,
    )
    assert rec["installs"] == [] and rec["reexec"] == []
    print("  orchestration mid-meeting guard: OK")


def test_orchestration_opt_out():
    rec = _orchestration_harness(
        manifest={"ref": "v0.1.38", "components": {"wheel": "0.1.38"}},
        installed={"wheel": "0.1.37", "helper": "0.1.30", "aec3": "0.1.30"},
        opt_out=True,
    )
    assert rec["installs"] == [] and rec["reexec"] == []
    print("  orchestration opt-out: OK")


def test_orchestration_lock_contention_skips():
    _clear_env()
    rec = {"installs": [], "reexec": []}

    class BusyLock:
        def __enter__(self): return False   # someone else holds it
        def __exit__(self, *a): pass

    with patched(
        fetch_manifest=lambda url=None: {"ref": "v0.1.38", "components": {"wheel": "0.1.38"}},
        installed_versions=lambda: {"wheel": "0.1.37", "helper": "0.1.30", "aec3": "0.1.30"},
        meeting_in_progress=lambda: False,
        _uv_install=lambda t: rec["installs"].append(t) or True,
        reexec=lambda argv: rec["reexec"].append(argv),
        _Lock=BusyLock,
    ):
        su.maybe_self_update(["dial", "claude", "u"])
    assert rec["installs"] == [] and rec["reexec"] == []
    print("  orchestration lock contention skips: OK")


def test_orchestration_never_raises_on_internal_error():
    _clear_env()

    def boom(*a, **k):
        raise RuntimeError("network exploded")

    # Must swallow and return (never propagate to the meeting launch path)
    with patched(fetch_manifest=boom, meeting_in_progress=lambda: False):
        su.maybe_self_update(["dial", "claude", "u"])  # should not raise
    print("  orchestration never raises on internal error: OK")


def test_manifest_none_noop():
    rec = _orchestration_harness(
        manifest=None,
        installed={"wheel": "0.1.37", "helper": "0.1.30", "aec3": "0.1.30"},
    )
    assert rec["installs"] == [] and rec["reexec"] == []
    print("  orchestration manifest-None no-op: OK")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    print(f"running {len(tests)} self-update tests\n")
    for t in tests:
        try:
            t()
        except Exception as e:
            failed += 1
            print(f"  FAIL: {t.__name__}: {e!r}")
    print()
    if failed:
        print(f"❌ {failed}/{len(tests)} FAILED")
        sys.exit(1)
    print(f"✓ all {len(tests)} self-update tests passed")
