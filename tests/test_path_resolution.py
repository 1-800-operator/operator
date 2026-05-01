"""
Unit tests for the path-relocation work across sessions 158–159.

Four user-scoped artifacts (`browser_profile/`, `auth_state.json`, `.env`,
`debug/`) used to be pinned at the repo root via `_BASE`/`_ROOT` walk-ups;
they now live under `~/.operator/`.

Covers:
  1. `config.BROWSER_PROFILE_DIR`, `AUTH_STATE_FILE`, `ENV_FILE`, `DEBUG_DIR`
     all resolve to absolute paths under `Path.home() / ".operator"`.
  2. `MacOSAdapter` picks up the config's absolute browser-profile path.
  3. `_migrate_legacy_user_artifacts()` moves repo-root legacy copies
     (profile + auth_state + .env) into `~/.operator/` on first run.
  4. The shim is a no-op when the destination already exists (doesn't
     clobber the user's active session or API keys).
  5. The shim is a no-op when the source is missing (fresh install).
  6. `.env` migration preserves file contents byte-for-byte.

Approach follows test_config_loader.py — redirect HOME to a tmp dir and
load `config.py` fresh via importlib so the module-level `Path.home()`
calls resolve inside the sandbox.

Run:
    source venv/bin/activate
    python tests/test_path_resolution.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

import importlib.util
import shutil
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
REAL_CONFIG_PY = REPO_ROOT / "src" / "_1_800_operator" / "config.py"
REAL_MAIN_PY = REPO_ROOT / "src" / "_1_800_operator" / "__main__.py"


def _sandbox(home: Path, bot: str = "testbot"):
    """Seed a minimal agents/<bot>/config.yaml under the sandbox HOME."""
    agents_root = home / ".operator" / "agents"
    (agents_root / bot).mkdir(parents=True)
    (agents_root / bot / "config.yaml").write_text(
        "agent: {name: Test}\nllm: {provider: openai, model: gpt-4o-mini}\n"
    )


def _load_config_with_home(home: Path, bot: str = "testbot"):
    saved = {k: os.environ.get(k) for k in ("HOME", "OPERATOR_BOT")}
    try:
        os.environ["HOME"] = str(home)
        os.environ["OPERATOR_BOT"] = bot
        spec = importlib.util.spec_from_file_location(f"config_path_test_{id(home)}", REAL_CONFIG_PY)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


# ---------------------------------------------------------------------------
# Test 1: config paths are absolute and under ~/.operator/
# ---------------------------------------------------------------------------

def test_config_paths_absolute_under_home_operator():
    tmp = Path(tempfile.mkdtemp())
    try:
        _sandbox(tmp)
        mod = _load_config_with_home(tmp)
        expected = {
            "BROWSER_PROFILE_DIR":  str(tmp / ".operator" / "browser_profile"),
            "AUTH_STATE_FILE":      str(tmp / ".operator" / "auth_state.json"),
            "GOOGLE_ACCOUNT_FILE":  str(tmp / ".operator" / "google_account.json"),
            "ENV_FILE":             str(tmp / ".operator" / ".env"),
            "DEBUG_DIR":            str(tmp / ".operator" / "debug"),
        }
        for name, want in expected.items():
            got = getattr(mod, name)
            assert os.path.isabs(got), f"{name} not absolute: {got!r}"
            assert got == want, f"{name}: expected {want}, got {got}"
        print("PASS  test_config_paths_absolute_under_home_operator")
    finally:
        shutil.rmtree(tmp)


def test_all_config_paths_rooted_under_home_or_tmp():
    """Regression guard for the class-of-bug that caused sessions 158/159.

    Enumerate every path-like public attribute on the config module and
    assert it is absolute AND rooted under the sandbox HOME (or `/tmp` /
    `/var`, which `/tmp` can symlink to on macOS). A new relative or
    repo-root-pinned constant can't be added without this test failing.

    The allow-list is deliberately explicit — if you add a new path
    constant, add its name here so the test keeps watching it. Silent
    escape (e.g. introducing a `DOWNLOADS_DIR` that points at
    `<repo>/downloads`) fails loud at the bottom of the function."""
    tmp = Path(tempfile.mkdtemp())
    try:
        _sandbox(tmp)
        mod = _load_config_with_home(tmp)
        home = str(tmp)

        # Canonical set — every filesystem path exposed on config.
        expected_path_attrs = {
            "BROWSER_PROFILE_DIR",
            "AUTH_STATE_FILE",
            "GOOGLE_ACCOUNT_FILE",
            "ENV_FILE",
            "DEBUG_DIR",
            "SKILLS_SHARED_LIBRARY",
            "BOT_DIR",
        }

        for name in expected_path_attrs:
            assert hasattr(mod, name), \
                f"expected path attr missing from config: {name!r}"
            val = getattr(mod, name)
            s = str(val)
            assert os.path.isabs(s), \
                f"config.{name} not absolute: {s!r} — relative paths silently " \
                f"resolve against CWD or site-packages"
            allowed_anchors = (home, "/tmp", "/var", "/private")
            assert s.startswith(allowed_anchors), \
                f"config.{name} escaped expected anchors {allowed_anchors!r}: {s}"

        # Anti-drift: scan the module for NEW `*_DIR` / `*_FILE` / `*_PATH`
        # public attrs and require they're in the allow-list. Forces a test
        # update when someone adds a new path — silent-escape prevention.
        suffixes = ("_DIR", "_FILE", "_PATH")
        discovered = {
            name for name in dir(mod)
            if not name.startswith("_")
            and any(name.endswith(sfx) for sfx in suffixes)
            and isinstance(getattr(mod, name), (str, Path))
        }
        unknown = discovered - expected_path_attrs
        assert not unknown, (
            f"new path-like config constants not in test allow-list: "
            f"{sorted(unknown)}. Add them to expected_path_attrs in "
            f"test_all_config_paths_rooted_under_home_or_tmp so the "
            f"regression guard watches them."
        )
        print("PASS  test_all_config_paths_rooted_under_home_or_tmp")
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Test 2: MacOSAdapter picks up the absolute config path (no walk-up)
# ---------------------------------------------------------------------------

def test_macos_adapter_uses_config_path_directly():
    """After the _BASE walk-up was removed, macos_adapter.BROWSER_PROFILE
    must equal config.BROWSER_PROFILE_DIR byte-for-byte."""
    tmp = Path(tempfile.mkdtemp())
    try:
        _sandbox(tmp)
        saved = {k: os.environ.get(k) for k in ("HOME", "OPERATOR_BOT")}
        try:
            os.environ["HOME"] = str(tmp)
            os.environ["OPERATOR_BOT"] = "testbot"
            # Force a fresh import of both modules so HOME is honored.
            for m in list(sys.modules):
                if m.startswith("_1_800_operator"):
                    del sys.modules[m]
            from _1_800_operator import config as cfg
            from _1_800_operator.connectors import macos_adapter
            assert macos_adapter.BROWSER_PROFILE == cfg.BROWSER_PROFILE_DIR, \
                f"mismatch: adapter={macos_adapter.BROWSER_PROFILE} vs config={cfg.BROWSER_PROFILE_DIR}"
            assert os.path.isabs(macos_adapter.BROWSER_PROFILE)
            print("PASS  test_macos_adapter_uses_config_path_directly")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Canonical-location round-trip — proves wizard-write == runtime-read
# ---------------------------------------------------------------------------

def test_env_file_canonical_across_modules():
    """The file the wizard writes `.env` to MUST equal the file config.py
    reads from. This was the exact session-158 divergence bug — wizard
    wrote to <repo>/.env while the claude auto-import and `operator edit
    env` targeted ~/.operator/.env, so API keys landed where nothing
    looked for them.

    Three touch points are checked for byte-exact agreement:
      1. config.ENV_FILE (what runtime readers see)
      2. setup._ENV_FILE (what the wizard writes into)
      3. __main__._resolve_config_target('env') (what `operator edit env`
         + `operator where env` open)

    If any of these drift apart again, this test fails with a direct diff."""
    tmp = Path(tempfile.mkdtemp())
    try:
        _sandbox(tmp)
        saved = {k: os.environ.get(k) for k in ("HOME", "OPERATOR_BOT")}
        try:
            os.environ["HOME"] = str(tmp)
            os.environ["OPERATOR_BOT"] = "testbot"
            # Fresh imports so module-level Path.home() calls honor HOME.
            for m in list(sys.modules):
                if m.startswith("_1_800_operator"):
                    del sys.modules[m]

            from _1_800_operator import config
            from _1_800_operator.pipeline import setup as setup_mod
            from _1_800_operator import __main__ as main_mod

            expected = str(tmp / ".operator" / ".env")

            # 1. Runtime read path (config.py load_dotenv target + ENV_FILE constant).
            assert config.ENV_FILE == expected, \
                f"config.ENV_FILE drift: {config.ENV_FILE!r} != {expected!r}"

            # 2. Wizard write path.
            assert str(setup_mod._ENV_FILE) == expected, \
                f"setup._ENV_FILE drift: {setup_mod._ENV_FILE!r} != {expected!r}"

            # 3. `operator edit env` / `operator where env` target.
            resolved, err = main_mod._resolve_config_target("env")
            assert err is None, f"_resolve_config_target returned error: {err}"
            assert str(resolved) == expected, \
                f"_resolve_config_target('env') drift: {resolved!r} != {expected!r}"

            # Cross-check all three agree pairwise — makes diagnostic output
            # explicit on failure even when `expected` itself is off.
            assert config.ENV_FILE == str(setup_mod._ENV_FILE) == str(resolved), \
                f"three-way divergence: config={config.ENV_FILE!r} " \
                f"setup={setup_mod._ENV_FILE!r} main={resolved!r}"

            print("PASS  test_env_file_canonical_across_modules")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(tmp)


def test_google_signin_paths_canonical_across_modules():
    """Wizard sign-in step inlines its path constants (BROWSER_PROFILE_DIR,
    AUTH_STATE_FILE, GOOGLE_ACCOUNT_FILE) because _1_800_operator.config asserts
    OPERATOR_BOT at import time and the wizard runs before any bot is
    chosen. Same compromise the wizard's _ENV_FILE makes — and same
    divergence risk: a future edit to config.py could miss
    google_signin.py. This test asserts byte-exact agreement."""
    tmp = Path(tempfile.mkdtemp())
    try:
        _sandbox(tmp)
        saved = {k: os.environ.get(k) for k in ("HOME", "OPERATOR_BOT")}
        try:
            os.environ["HOME"] = str(tmp)
            os.environ["OPERATOR_BOT"] = "testbot"
            for m in list(sys.modules):
                if m.startswith("_1_800_operator"):
                    del sys.modules[m]

            from _1_800_operator import config
            from _1_800_operator.pipeline import google_signin as gs

            pairs = (
                ("BROWSER_PROFILE_DIR", gs._BROWSER_PROFILE_DIR),
                ("AUTH_STATE_FILE",     gs._AUTH_STATE_FILE),
                ("GOOGLE_ACCOUNT_FILE", gs._GOOGLE_ACCOUNT_FILE),
            )
            for cfg_name, gs_val in pairs:
                cfg_val = getattr(config, cfg_name)
                assert str(gs_val) == cfg_val, \
                    f"google_signin._{cfg_name} drift: " \
                    f"{gs_val!r} (wizard) != {cfg_val!r} (config)"

            print("PASS  test_google_signin_paths_canonical_across_modules")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(tmp)


def test_debug_dir_canonical_across_modules():
    """Parallel guard for DEBUG_DIR: session.save_debug() and both adapters'
    failure-screenshot paths must all route through config.DEBUG_DIR.

    session.py and the adapters already `from _1_800_operator import config` and
    use `config.DEBUG_DIR` directly, so divergence would require someone to
    re-introduce a hardcoded string. This test imports each module and
    asserts the config attr it uses is the same object reference — any
    inlined literal would surface here."""
    tmp = Path(tempfile.mkdtemp())
    try:
        _sandbox(tmp)
        saved = {k: os.environ.get(k) for k in ("HOME", "OPERATOR_BOT")}
        try:
            os.environ["HOME"] = str(tmp)
            os.environ["OPERATOR_BOT"] = "testbot"
            for m in list(sys.modules):
                if m.startswith("_1_800_operator"):
                    del sys.modules[m]

            from _1_800_operator import config
            from _1_800_operator.connectors import session as session_mod
            from _1_800_operator.connectors import macos_adapter as mac_mod
            from _1_800_operator.connectors import linux_adapter as linux_mod

            expected = str(tmp / ".operator" / "debug")
            assert config.DEBUG_DIR == expected

            # Each module that writes to debug/ must reach config.DEBUG_DIR
            # (not a private copy or literal). Shared module reference is
            # the strongest proof.
            for mod in (session_mod, mac_mod, linux_mod):
                assert mod.config.DEBUG_DIR == expected, \
                    f"{mod.__name__}.config.DEBUG_DIR drift: " \
                    f"{mod.config.DEBUG_DIR!r} != {expected!r}"
                assert mod.config is config, \
                    f"{mod.__name__} imports a different config module " \
                    f"(id {id(mod.config)} vs {id(config)})"
            print("PASS  test_debug_dir_canonical_across_modules")
        finally:
            for k, v in saved.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v
    finally:
        shutil.rmtree(tmp)


# ---------------------------------------------------------------------------
# Migration-shim tests
# ---------------------------------------------------------------------------

def _load_main_module():
    """Import __main__.py as a plain module so we can call the shim directly.

    __main__.py does side-effect-free work at import time (only function defs
    and the Popen patch), so this is safe.
    """
    spec = importlib.util.spec_from_file_location("operator_main_under_test", REAL_MAIN_PY)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _fake_repo_root(tmp: Path) -> Path:
    """Lay out a fake src/_1_800_operator/ tree inside `tmp` so __main__.py loaded
    from there resolves `Path(__file__).resolve().parent.parent.parent` to
    `tmp`. Returns the path where we should copy __main__.py."""
    target_dir = tmp / "src" / "_1_800_operator"
    target_dir.mkdir(parents=True)
    shutil.copy(REAL_MAIN_PY, target_dir / "__main__.py")
    return target_dir / "__main__.py"


def _load_main_from(fake_main: Path):
    spec = importlib.util.spec_from_file_location(
        f"operator_main_shim_{id(fake_main)}", fake_main
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_moves_legacy_artifacts():
    """Legacy profile dir + auth_state + .env at the (fake) repo root all get
    moved to ~/.operator/ on first call. Exact byte content preserved for
    files (the .env coverage is critical — real users have live API keys)."""
    tmp = Path(tempfile.mkdtemp())
    try:
        fake_main = _fake_repo_root(tmp)
        # Seed legacy artifacts at the fake repo root.
        legacy_profile = tmp / "browser_profile"
        legacy_profile.mkdir()
        (legacy_profile / "marker").write_text("hello")
        legacy_auth = tmp / "auth_state.json"
        legacy_auth.write_text('{"cookies": []}')
        legacy_env = tmp / ".env"
        env_contents = "OPENAI_API_KEY=sk-test\nANTHROPIC_API_KEY=sk-ant-test\n"
        legacy_env.write_text(env_contents)

        # Redirect HOME so the shim writes into the sandbox.
        fake_home = tmp / "home"
        fake_home.mkdir()
        saved_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(fake_home)
            mod = _load_main_from(fake_main)
            mod._migrate_legacy_user_artifacts()

            migrated_profile = fake_home / ".operator" / "browser_profile"
            migrated_auth = fake_home / ".operator" / "auth_state.json"
            migrated_env = fake_home / ".operator" / ".env"
            assert migrated_profile.is_dir(), f"profile not moved: {migrated_profile}"
            assert (migrated_profile / "marker").read_text() == "hello"
            assert migrated_auth.is_file()
            assert migrated_auth.read_text() == '{"cookies": []}'
            assert migrated_env.is_file(), f".env not moved: {migrated_env}"
            assert migrated_env.read_text() == env_contents, \
                f".env contents changed during migration"
            assert not legacy_profile.exists(), "legacy profile should be gone"
            assert not legacy_auth.exists(), "legacy auth_state should be gone"
            assert not legacy_env.exists(), "legacy .env should be gone"
            print("PASS  test_migration_moves_legacy_artifacts")
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
    finally:
        shutil.rmtree(tmp)


def test_migration_preserves_existing_target():
    """If ~/.operator/browser_profile/ already exists, don't clobber it —
    leave the legacy copy in place so the user can reconcile manually."""
    tmp = Path(tempfile.mkdtemp())
    try:
        fake_main = _fake_repo_root(tmp)
        # Legacy copy with distinct content.
        legacy_profile = tmp / "browser_profile"
        legacy_profile.mkdir()
        (legacy_profile / "marker").write_text("legacy")

        fake_home = tmp / "home"
        (fake_home / ".operator" / "browser_profile").mkdir(parents=True)
        (fake_home / ".operator" / "browser_profile" / "marker").write_text("existing")

        saved_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(fake_home)
            mod = _load_main_from(fake_main)
            mod._migrate_legacy_user_artifacts()

            # Legacy copy still present (not moved, not deleted).
            assert legacy_profile.is_dir()
            assert (legacy_profile / "marker").read_text() == "legacy"
            # Existing user copy untouched.
            user_marker = fake_home / ".operator" / "browser_profile" / "marker"
            assert user_marker.read_text() == "existing"
            print("PASS  test_migration_preserves_existing_target")
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
    finally:
        shutil.rmtree(tmp)


def test_migration_preserves_existing_env():
    """If ~/.operator/.env already has user content, a repo-root .env must
    NOT overwrite it — the home copy is authoritative, legacy stays put for
    manual reconciliation (protects live API keys)."""
    tmp = Path(tempfile.mkdtemp())
    try:
        fake_main = _fake_repo_root(tmp)
        legacy_env = tmp / ".env"
        legacy_env.write_text("OPENAI_API_KEY=stale-legacy-key\n")

        fake_home = tmp / "home"
        (fake_home / ".operator").mkdir(parents=True)
        user_env = fake_home / ".operator" / ".env"
        user_env.write_text("OPENAI_API_KEY=live-home-key\n")

        saved_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(fake_home)
            mod = _load_main_from(fake_main)
            mod._migrate_legacy_user_artifacts()

            assert user_env.read_text() == "OPENAI_API_KEY=live-home-key\n", \
                "home .env was overwritten — would destroy user API keys"
            assert legacy_env.exists(), "legacy .env should remain for manual review"
            assert legacy_env.read_text() == "OPENAI_API_KEY=stale-legacy-key\n"
            print("PASS  test_migration_preserves_existing_env")
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
    finally:
        shutil.rmtree(tmp)


def test_migration_noop_on_fresh_install():
    """No legacy files → shim is silent, creates no artifacts beyond the
    home_dir directory itself."""
    tmp = Path(tempfile.mkdtemp())
    try:
        fake_main = _fake_repo_root(tmp)
        fake_home = tmp / "home"
        fake_home.mkdir()

        saved_home = os.environ.get("HOME")
        try:
            os.environ["HOME"] = str(fake_home)
            mod = _load_main_from(fake_main)
            mod._migrate_legacy_user_artifacts()

            operator_dir = fake_home / ".operator"
            assert operator_dir.is_dir(), "home_dir not created"
            assert not (operator_dir / "browser_profile").exists()
            assert not (operator_dir / "auth_state.json").exists()
            assert not (operator_dir / ".env").exists()
            print("PASS  test_migration_noop_on_fresh_install")
        finally:
            if saved_home is None:
                os.environ.pop("HOME", None)
            else:
                os.environ["HOME"] = saved_home
    finally:
        shutil.rmtree(tmp)


if __name__ == "__main__":
    test_config_paths_absolute_under_home_operator()
    test_all_config_paths_rooted_under_home_or_tmp()
    test_macos_adapter_uses_config_path_directly()
    test_env_file_canonical_across_modules()
    test_google_signin_paths_canonical_across_modules()
    test_debug_dir_canonical_across_modules()
    test_migration_moves_legacy_artifacts()
    test_migration_preserves_existing_target()
    test_migration_preserves_existing_env()
    test_migration_noop_on_fresh_install()
    print("\nAll path-resolution tests passed.")
