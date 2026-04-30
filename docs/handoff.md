# Session 173 handoff (2026-04-29) — local credential hygiene

This session was a security audit + small hardening pass, prompted by the recent Vercel/Context AI incident (malware on a developer machine exfiltrated OAuth bearer tokens with overly broad scopes → Gmail compromise). Important framing surfaced and recorded in Hard Won Knowledge: we don't run a real OAuth flow at all — we keep a Playwright persistent Chrome profile plus a `storage_state()` JSON export. So "trim scopes" doesn't apply; the analogous mitigation is "harden local artifacts." Audit confirmed `auth_state.json` is NOT removable — Linux uses it as the *primary* auth source and macOS uses it as a real recovery fallback when Google rotates cookies; verified by walking every reader (`google_signin.py:88`, `linux_adapter.py:298-330`, `macos_adapter.py:609-636`, `session.py:199-246`).

**Done this session (commit `9a6d02e`, five files):** `os.umask(0o077)` at `__main__.main()` so any new file under `~/.brainchild/` is born `0o600` by default; explicit `chmod 0o600` on `auth_state.json` + `google_account.json` after the wizard writes them; `mode=0o700` passed to every `os.makedirs` under `~/.brainchild/` (closes the mkdir → chmod race window); `0o600` on each `save_debug` screenshot/HTML and `0o700` on `DEBUG_DIR`. `docs/security.md` L2 updated to reflect the new ground truth. No retroactive migration shipped (pre-prod, single machine, user explicitly out-of-scoped that work).

**Exact next step: Phase 14.13.1 — `gh repo create 1-800-operator/operator --public` and push current `main`** (carries over unchanged from session 170 / 171 / 172). Open question to settle first: fresh start vs. GitHub repo-transfer flow on `shapirojojo/operator` to preserve history, open PRs, and inbound-link redirects. Pre-existing untriaged code changes in the working tree are now larger than the session 170 list — `claude_code_import.py`, `setup.py`, `tests/test_claude_code_import.py`, `agent-context.md`, `roadmap.md` joined the carry-over set during sessions 171/172. User should triage these before Phase 14.13.2 (the actual `src/brainchild/` → `src/operator/` rename) so they don't get tangled in a giant rename diff.

**Open blockers / carry-overs:**
1. **`~/.brainchild/history/` still world-readable** — contains every meeting's chat JSONL, same sensitivity tier as `debug/` was. Pre-prod acceptable, but harden before launch (walk every `MeetingRecord` write site, add `mode=0o700` + `0o600` on JSONL writes, plus a one-shot retroactive chmod in `_migrate_legacy_user_artifacts`).
2. **No automated test for the perm hardening** — manual smoke (`rm` artifacts → wizard → `ls -la`) is the validation today. Worth a small unit test in `tests/test_perm_hardening.py` that monkey-patches `os.umask` / `os.chmod` and asserts the calls fire. Deferred.
3. **macOS Keychain / Linux Secret Service for `auth_state.json`** is the right v2 move — protects against malware-as-current-user, which the chmod fix doesn't cover. Punted; cross-platform UX has real wrinkles (kwallet / keyring integration, unlock prompts).
4. **Pre-existing uncommitted changes still in the tree** (carries from sessions 170 / 171 / 172): `README.md`, `docs/live-skill-tests.md`, `chat_runner.py`, `claude_code_import.py`, `setup.py`, `tests/test_claude_code_import.py`, `tests/test_stdout_heartbeat.py`, plus the `debug/*` screenshot dump. None are from session 173.
5. **USPTO TESS sweep + DNS for `1-800-operator.com`** still open from session 170 — unchanged.

---

## Phase 14.16 — build/edit split + CLAUDE.md mirror data model (parallel session 173 work, not yet committed)

A second uncommitted thread layered on top of the credential-hygiene commit `9a6d02e`. Three slices, all unit-tested + ready for live validation:

**1. `build` reset semantics (`pipeline/setup.py:_maybe_reset_to_bundled`).** `brainchild build <name>` now detects when user-scope cfg differs from bundled, prompts `Reset <name> to defaults? [y/n] (n)` with off-ramp `For surgical changes, use brainchild edit <name>`. On `y`: backup → `<name>/config.yaml.bak.<YYYYMMDD-HHMMSS>` + reset to bundled + walk wizard. On `n`: `WizardCancel`. **Per-agent surgical** — sibling agents' configs untouched. Pristine state (user==bundled) skips the prompt silently.

**2. CLAUDE.md mirror data model (`config.py:_resolve_claude_md_path` + `_read_claude_md_imports`, `pipeline/setup.py:1048`).** Wizard step 4 retired the "bake content into ground_rules" approach. New shape: stores **paths** in `claude_md_imports: [~/.claude/CLAUDE.md, ./CLAUDE.md, ./.claude/CLAUDE.md]`; `config.py` reads each fresh at every boot. `~/...` resolves against current `$HOME`; `./...` resolves against current cwd; absolute paths used as-is. Missing paths warn-and-skip via stderr (`[<bot>] ⚠ claude_md_imports: skipped missing source ...`). **Default flipped to `y`** (mirror by default — opinionated; the agent's identity is "your Claude Code setup"). Decline persists empty list so re-runs don't silently re-mirror.

**3. `edit` repurposed (`__main__.py:_run_edit`, `pipeline/setup.py:run`).** `brainchild edit <bot>` no longer opens YAML in `$EDITOR` — it now invokes the same wizard as `brainchild build`, with two differences: skips the preset gallery (jumps straight to `_edit_preset(target_agent)`) and skips the reset gate (`reset_allowed=False`). `brainchild edit .env` keeps the `$EDITOR` flow (env vars are the wrong shape for a TUI). `brainchild edit <missing-bot>` fast-fails with `Run brainchild build <name> to create one`.

### Test plan (live validation, ~25 min)

Pre-step: confirm baseline ground_rules — `python3 -c "import yaml,hashlib; gr=yaml.safe_load(open('/Users/jojo/.brainchild/agents/claude/config.yaml'))['ground_rules']; print(hashlib.md5(gr.encode()).hexdigest())"` should return `88a4835054f3d4e2c70366c3d003fd3a` (length 2056). If not, restore from `~/.brainchild/agents/claude/config.yaml.bak.preDev-revert`.

#### T4 — `build` reset semantics

**T4.1 — Decline path.** `cd /Users/jojo/Desktop/operator && brainchild build claude` → ⚠ warning fires → reply `n`. Pass: `Cancelled. reset declined — brainchild edit claude is the surgical path` (or similar) appears in stderr; cfg untouched (md5 unchanged); no `.bak.*` files created. Currently expected to fire because session 172/173 wizard runs already poisoned ground_rules differently from bundled (or `~/.brainchild/agents/claude/config.yaml.bak.preDev-revert` exists alongside the current cfg). Confirm via `ls ~/.brainchild/agents/claude/*.bak* | wc -l` before vs. after — should be unchanged.

**T4.2 — Accept path.** Same command → reply `y`. Pass: console prints `✓ previous config backed up → <path>`; the timestamped `.bak` file exists at `~/.brainchild/agents/claude/config.yaml.bak.<YYYYMMDD-HHMMSS>` and contains the *pre-reset* state; the live cfg now matches `src/brainchild/agents/claude/config.yaml` (bundled) byte-for-byte until the wizard's later steps mutate it. Wizard's success line surfaces backup path: `Previous config saved at .../config.yaml.bak.<ts> — restore with cp ... if needed.`

**T4.3 — Pristine skip.** Reset to bundled first (T4.2), then immediately re-run `brainchild build claude` and `q` out of the gallery. Pass: no ⚠ warning between agent select and the next wizard step (the prompt gate hit silent-skip because user-scope == bundled). Also valid: a follow-up dirty re-run → ⚠ fires again. Verifies the byte-comparison shortcut.

**T4.4 — Per-agent isolation.** Edit `~/.brainchild/agents/pm/config.yaml` (touch a known field, e.g. add `# T4.4 marker` comment), then run `brainchild build claude` → accept the reset. Pass: `pm/config.yaml` is untouched (`grep "T4.4 marker" ~/.brainchild/agents/pm/config.yaml` still returns the line); no `pm/config.yaml.bak.*` created. Confirms `_maybe_reset_to_bundled` is surgical at the file level.

#### T5 — CLAUDE.md mirror data model

**T5.1 — Mirror prompt + storage shape.** Inside the `build claude` wizard (T4.2 continuation), reach the CLAUDE.md step. Pass: prompt reads `Mirror ./CLAUDE.md (~11k chars) into the bot's system prompt? [y/n] (y):`. Reply `y`. Inspect `~/.brainchild/agents/claude/config.yaml`: should contain `claude_md_imports: [./CLAUDE.md]` (or whatever sources auto-detected); `ground_rules` is NOT bloated with project content (still the personality/self-narration baseline length, ~2k chars).

**T5.2 — Boot reads fresh.** From operator dir, `brainchild run claude <meet-url>`. Pass: stderr near startup shows no `⚠ claude_md_imports` warnings (all paths resolve cleanly); `grep "Brainchild is a chat-based\|## What This Project Does" /tmp/brainchild.log` (or wherever SYSTEM_PROMPT is logged) shows project CLAUDE.md content present. Bonus: in chat, ask the bot "what does this project do?" — bot's reply should reflect the project CLAUDE.md content, not generic answers.

**T5.3 — True freshness.** While bot is running (or after leaving + re-joining), edit `/Users/jojo/Desktop/operator/CLAUDE.md` — add an obvious sentinel line like `## TEST SENTINEL: chai-tea-7`. Re-run `brainchild run claude <new-meet>`. Pass: ask bot "is there a test sentinel?" — bot quotes `chai-tea-7`. Proves content is read at boot, not baked at wizard-time. **Don't forget to revert the sentinel** with `git checkout CLAUDE.md` before committing.

**T5.4 — Decline persistence.** Run `brainchild build claude` → accept reset → at CLAUDE.md prompt, reply `n`. Pass: `claude_md_imports: []` in cfg (explicit empty list, not absent). Re-run `brainchild build claude` → accept reset → CLAUDE.md prompt should still fire (empty list doesn't suppress the prompt). The empty list is the persistence signal that the user *was* asked and *did* decline.

**T5.5 — cwd resolution.** From a *different* cwd: `cd ~ && brainchild run claude <meet>`. Pass: stderr shows `⚠ claude_md_imports: skipped missing source './CLAUDE.md' (resolved to /Users/jojo/CLAUDE.md)` since `~/CLAUDE.md` doesn't exist. Bot still boots; just project-scope content drops. Confirms `./...` resolves against current cwd, mirroring Claude Code's own behavior.

**T5.6 — Cross-machine sim (~/...).** Stays the same regardless of cwd. With `claude_md_imports: [~/.claude/CLAUDE.md]` + a fixture at `~/.claude/CLAUDE.md`, boot from any cwd → content present. Confirms tilde paths re-resolve against current `$HOME`.

#### T6 — `edit` as build-minus-reset

**T6.1 — Bot-name path.** After T4.2 + T5.1 (cfg now customized), run `brainchild edit claude`. Pass: title bar reads `Brainchild edit wizard` (not `build`); no ⚠ reset warning fires; preset gallery is skipped (no fighter-select picker); wizard walks straight into the agent's loaded current state (current MCP toggles, skill selections, mirrored CLAUDE.md sources, etc. all pre-loaded). Step through to the end and confirm cfg is preserved (no surprise mutations).

**T6.2 — `.env` path.** `brainchild edit .env` → still opens `$EDITOR` (vim/nano). Pass: behavior unchanged; `Saved /Users/jojo/.brainchild/.env` line appears on quit.

**T6.3 — Missing bot.** `brainchild edit no-such-bot`. Pass: prints `No agent named no-such-bot found. Run brainchild build no-such-bot to create one.` Returns rc=1; no wizard runs.

**T6.4 — claude prereq gate.** Temporarily move Claude Code out of PATH (`alias claude=:` or rename the binary), then `brainchild edit claude`. Pass: same red `✗ claude agent requires Claude Code:` block as `build` shows; rc=1; no wizard runs. Restore PATH after.

#### Regression — T1/T2/T3 should still pass

Project-scope walks for skills + CLAUDE.md from session 172 should still work end-to-end. Quick smoke: re-run T1 (plant `<cwd>/.claude/skills/operator-test-skill/SKILL.md`, ask bot to use it, expect `kingfisher-7`).

### Carry-over for next session
- Phase 14.16 is **not committed**. Files modified: `src/brainchild/config.py`, `src/brainchild/__main__.py`, `src/brainchild/pipeline/setup.py`, `src/brainchild/pipeline/claude_code_import.py`, `tests/test_setup.py`, `tests/test_config_loader.py`, `tests/test_claude_code_import.py`. Triage alongside the existing pre-existing uncommitted changes (carry-over #4 above) before Phase 14.13.2 rename.
- Live-validation outstanding (the test plan above). Suggest knocking it out before commit so the commit message can claim "live-validated" rather than just "unit-tested."
- `normalize_path_for_storage` helper added to `claude_code_import.py` is unused in critical path today — reserved for a future "add custom CLAUDE.md path" UI in `edit`. Defer until a real user asks for it.
