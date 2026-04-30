# Security

Thanks for taking the time to report a security issue in Brainchild.

## Reporting a vulnerability

Email **shapirojojo@gmail.com** with:

- A description of the issue and its impact.
- Steps to reproduce (ideally a minimal agent config or chat transcript).
- The commit hash or release you tested against.

Please **do not** open a public GitHub issue for security-sensitive reports.
Use a GitHub Security Advisory (Security → Advisories → New draft advisory)
if you prefer GitHub's flow over email.

## Response SLA

- **Acknowledgement** within 72 hours.
- **Triage and initial assessment** within 7 days.
- **Fix or mitigation plan** within 30 days for high/critical issues; lower
  severity may be batched with a regular release.

If I go longer than 72 hours without acknowledging, assume the email was
missed and nudge the same address.

## Recognition

Reporters who follow coordinated disclosure are credited by name (or handle,
your preference) in the release notes and GitHub Security Advisory that ships
the fix. No bug bounty — this is a solo open-source project.

## Scope

In scope:

- Code in this repository (`brainchild` CLI, connectors, pipeline, agents).
- Default agent configs shipped under `agents/`.

Out of scope:

- Issues in upstream dependencies — report those to the dependency owner.
  Brainchild's own pinned versions are tracked via `pip-audit`; see
  `docs/security.md`.
- Google Meet itself, or Meet's chat/participant controls.
- Third-party MCP servers invoked via user-supplied configs.

## Threat model and hardening

`docs/security.md` documents the threat model, known residual risks, and the
mitigations already in place. Read it before filing a report — the issue you
are seeing may be a known, documented residual risk with a recommended
operational workaround (e.g. Meet's "host manages chat" setting).

### Recent hardening — local credential hygiene (session 173)

Triggered by the Vercel/Context AI incident (malware on a developer machine
exfiltrated OAuth bearer tokens). Brainchild does not run a real OAuth flow —
it persists Google session via a Playwright Chrome profile plus a
`storage_state()` JSON export — so the analogous mitigation is "harden local
artifacts." Shipped:

- `os.umask(0o077)` set at process start so any new file under
  `~/.brainchild/` is born `0o600` by default. Closes the mkdir → chmod race
  for callers that didn't pass `mode=` explicitly.
- Explicit `chmod 0o600` on `auth_state.json` and `google_account.json` after
  the wizard writes them — the two files that contain Google session
  material.
- `mode=0o700` passed to every `os.makedirs` under `~/.brainchild/`. Applies
  to `agents/`, `history/`, `debug/`, `browser_profile/`, and the home dir
  itself.
- `0o600` on each `save_debug` screenshot/HTML dump and `0o700` on
  `DEBUG_DIR` — meeting screenshots can leak chat content, so they get the
  same tier as session material.

Residual risks not covered (tracked for v2):

- `~/.brainchild/history/` — chat JSONL is currently written without an
  explicit mode override; pre-existing files retain their old perms. Walk
  every `MeetingRecord` write site + add a one-shot retroactive chmod in
  `_migrate_legacy_user_artifacts` before launch.
- macOS Keychain / Linux Secret Service for `auth_state.json` is the right
  v2 move — protects against malware running as the current user, which the
  chmod fix doesn't cover. Cross-platform UX has real wrinkles
  (kwallet/keyring integration, unlock prompts) so it's deferred.
