"""
Skill loader — reads Claude Code-style SKILL.md folders for Operator.

Skill library model (Phase 15.11, session 152):
  - Shared library at ~/.operator/skills/<name>/SKILL.md (one per user,
    shared across all agents). Seeded on first run from the bundled package.
  - Optional per-agent `external_paths` list declared in config.yaml
    (skills.external_paths). Entries must be tilde-prefixed (`~/…`) or
    absolute (`/…`); relative paths are CWD-dependent and WARN + skipped.
  - Agent config names the skills it uses via `skills.enabled: [names]`.

Resolution order (last-wins for name collisions):
  1. Shared library.
  2. Each external_paths entry, in list order.

So an agent can override a library skill by pointing at an external path
containing a same-named SKILL.md — the external one wins. Useful for
testing a local skill variant without touching the library copy.

SKILL.md frontmatter:
  - `name` — required. Unique identifier shown in the wizard + LLM prompts.
  - `description` — required. Trigger-phrase-first one-liner the LLM
    matches against. Lead with the phrases that should fire the skill.
  - `allowed-tools` — optional list/csv. Non-MCP tool hints; anything
    outside SUPPORTED_ALLOWED_TOOLS logs a WARN but still loads.
  - `mcp-required` (alias `mcp_required`) — optional list/csv of MCP
    server names this skill fundamentally relies on. Consumed by the
    build wizard to lock matching MCP toggles on. Missing = no declared
    deps (honest default). User-authored skills that omit this field
    load unconditionally; the runtime safety net in
    mcp_client.disabled_server_for_tool raises an actionable
    "server disabled" error if the LLM actually tries to call a tool
    from a disabled server.

The remainder of the file is the skill body — free-form instructions fed
to the LLM when the skill is invoked.

A skill location can be either shape:
  - A folder containing SKILL.md  → single skill.
  - A parent folder               → scanned one level deep for */SKILL.md.

Malformed or missing entries WARN and are skipped rather than crashing.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

log = logging.getLogger(__name__)

# MCP-tool-aware subset. We WARN on anything outside this (e.g. Claude Code's
# Bash/Edit/Write/Read) because the meeting bot can't honour those — the skill
# still loads; the model may decline to use it.
SUPPORTED_ALLOWED_TOOLS = {"load_skill"}

# Default shared-library location. Overridable for tests.
DEFAULT_SHARED_LIBRARY = Path.home() / ".operator" / "skills"


@dataclass
class Skill:
    name: str
    description: str
    body: str
    allowed_tools: list[str] = field(default_factory=list)
    # MCP servers whose tools this skill fundamentally relies on. Consumed by
    # the build wizard to preseed enabled=true on the MCP step and to grey out
    # toggles the user can't safely disable. Missing field → empty list → no
    # declared deps (honest default; runtime falls back on the granular
    # "disabled server" error in mcp_client.disabled_server_for_tool).
    mcp_required: list[str] = field(default_factory=list)
    source_path: str = ""


def _parse_skill_md(path: Path) -> Skill | None:
    """Parse a SKILL.md file. Returns None on any failure (with WARN logged)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as e:
        log.warning(f"SKILLS: could not read {path}: {e}")
        return None

    if not text.startswith("---"):
        log.warning(f"SKILLS: {path} missing frontmatter — skipping")
        return None

    # Split frontmatter out of the body.
    parts = text.split("---", 2)
    if len(parts) < 3:
        log.warning(f"SKILLS: {path} has unterminated frontmatter — skipping")
        return None
    _, fm_text, body = parts

    try:
        fm = yaml.safe_load(fm_text) or {}
    except yaml.YAMLError as e:
        log.warning(f"SKILLS: {path} has malformed YAML frontmatter: {e} — skipping")
        return None

    if not isinstance(fm, dict):
        log.warning(f"SKILLS: {path} frontmatter is not a mapping — skipping")
        return None

    name = (fm.get("name") or "").strip()
    description = (fm.get("description") or "").strip()
    if not name or not description:
        log.warning(f"SKILLS: {path} missing name or description — skipping")
        return None

    raw_tools = fm.get("allowed-tools") or []
    if isinstance(raw_tools, str):
        allowed = [t.strip() for t in raw_tools.split(",") if t.strip()]
    elif isinstance(raw_tools, list):
        allowed = [str(t).strip() for t in raw_tools if str(t).strip()]
    else:
        log.warning(f"SKILLS: {path} allowed-tools has unexpected type — ignoring")
        allowed = []

    unsupported = [t for t in allowed if t not in SUPPORTED_ALLOWED_TOOLS]
    if unsupported:
        log.warning(
            f"SKILLS: {path} declares unsupported allowed-tools {unsupported} — "
            f"loading anyway; model may decline to use them"
        )

    raw_required = fm.get("mcp-required") or fm.get("mcp_required") or []
    if isinstance(raw_required, str):
        mcp_required = [t.strip() for t in raw_required.split(",") if t.strip()]
    elif isinstance(raw_required, list):
        mcp_required = [str(t).strip() for t in raw_required if str(t).strip()]
    else:
        log.warning(f"SKILLS: {path} mcp-required has unexpected type — ignoring")
        mcp_required = []

    return Skill(
        name=name,
        description=description,
        body=body.strip(),
        allowed_tools=allowed,
        mcp_required=mcp_required,
        source_path=str(path),
    )


def _resolve_external_path(entry: str) -> Path | None:
    """Resolve an external_paths entry; None if invalid or missing.

    Entries must be tilde-prefixed (starts with `~`) or absolute (starts
    with `/`). Relative paths would resolve against the process CWD, which
    is unreliable for a long-running meeting bot — WARN and skip.
    """
    if not isinstance(entry, str) or not entry.strip():
        log.warning(f"SKILLS: external_paths entry is empty or non-string — skipping")
        return None
    raw = entry.strip()
    # Tilde-prefixed is treated as absolute once expanded.
    if not (raw.startswith("~") or raw.startswith("/")):
        log.warning(
            f"SKILLS: external_paths entry {raw!r} is not tilde-prefixed or "
            f"absolute — skipping. Use `~/...` or `/...` (relative paths are "
            f"CWD-dependent and unreliable)."
        )
        return None
    p = Path(os.path.expanduser(raw)).resolve()
    if not p.exists() or not p.is_dir():
        log.warning(f"SKILLS: external_paths entry {raw!r} not found — skipping")
        return None
    return p


def _scan_skills_dir(root: Path) -> list[Skill]:
    """Scan one directory for SKILL.md files. Returns all parsed Skill objects.

    Accepts both shapes:
      - root/SKILL.md       → a single-skill folder.
      - root/*/SKILL.md     → a parent folder with one skill per child dir.
    """
    results: list[Skill] = []
    if not root.is_dir():
        return results
    direct = root / "SKILL.md"
    if direct.is_file():
        sk = _parse_skill_md(direct)
        if sk:
            results.append(sk)
        return results
    for child in sorted(root.iterdir()):
        if child.is_dir() and (child / "SKILL.md").is_file():
            sk = _parse_skill_md(child / "SKILL.md")
            if sk:
                results.append(sk)
    return results


def load_skills(
    enabled_names: list[str] | None,
    external_paths: list[str] | None = None,
    shared_library_dir: Path | None = None,
    cwd: Path | None = None,
) -> list[Skill]:
    """Load skills from shared library + external_paths + project scope,
    filtered by enabled_names.

    Args:
      enabled_names: names to keep. None means "return all discovered."
      external_paths: list of extra paths to scan. Tilde-prefixed or
        absolute; relative paths WARN + skip.
      shared_library_dir: override ~/.operator/skills/ (used by tests).
      cwd: project working directory. Defaults to Path.cwd(). Used to
        walk `<cwd>/.claude/skills/` implicitly so project-scoped Claude
        Code skills surface without needing to be listed in
        external_paths. Mirrors the cwd-aware walk we do for project-
        scope MCPs.

    Returns at most one Skill per name. Name collisions resolve last-wins
    in source order: shared library first, then each external path, then
    project-scope. So a project skill overrides a same-named library or
    external skill.

    Unknown enabled names WARN once and are silently dropped from the
    result (the LLM is unaffected; they simply don't appear in the
    system prompt).
    """
    if shared_library_dir is None:
        shared_library_dir = DEFAULT_SHARED_LIBRARY
    if cwd is None:
        cwd = Path.cwd()

    source_dirs: list[Path] = []
    shared = Path(shared_library_dir).expanduser().resolve()
    if shared.exists() and shared.is_dir():
        source_dirs.append(shared)

    for entry in (external_paths or []):
        p = _resolve_external_path(entry)
        if p is not None:
            source_dirs.append(p)

    project_skills = (Path(cwd) / ".claude" / "skills").resolve()
    if project_skills.is_dir():
        source_dirs.append(project_skills)

    by_name: dict[str, Skill] = {}
    for src in source_dirs:
        for sk in _scan_skills_dir(src):
            if sk.name in by_name:
                prev = by_name[sk.name].source_path
                log.info(
                    f"SKILLS: {sk.name!r} from {sk.source_path} "
                    f"overrides {prev} (last-wins)"
                )
            by_name[sk.name] = sk

    discovered = list(by_name.values())

    if enabled_names is None:
        selected = discovered
    else:
        missing = [n for n in enabled_names if n not in by_name]
        for n in missing:
            log.warning(
                f"SKILLS: enabled skill {n!r} not found in library or "
                f"external_paths — skipping"
            )
        selected = [by_name[n] for n in enabled_names if n in by_name]

    _log_banner(selected, discovered)
    return selected


def _log_banner(selected: list[Skill], discovered: list[Skill]) -> None:
    """Mirror the MCP startup banner shape."""
    if not discovered:
        log.info("SKILLS: no skills discovered in library or external_paths")
        return
    names = ", ".join(f"{s.name} ✓" for s in selected)
    log.info(f"SKILLS: {len(selected)}/{len(discovered)} enabled ({names})")
