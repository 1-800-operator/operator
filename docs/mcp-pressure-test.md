# MCP Pressure Test — Living Document

*Last updated: April 18, 2026 (session 128 — dropped Slack + Brave Search after upstream deprecation)*

Running record of tool discovery, test results, and hints derived for each MCP server.
Update in place as tests are executed. Hints column feeds directly into `config.yaml` per-server `hints` fields (step 10.11).

**Legend:** ⬜ not started · 🔄 in progress · ✅ pass · ❌ fail · ⚠️ partial/quirky

---

## Servers at a Glance

| Server | Transport | Auth | Tools | Status |
|--------|-----------|------|-------|--------|
| Linear | Remote HTTP via `mcp-remote` | OAuth (token cached) | 31 | Active |
| GitHub | Local binary (`github-mcp-server` v0.32.0) | PAT (`GITHUB_TOKEN`) | 41 | Active |
| Notion | stdio via npx | API key (`NOTION_API_KEY`) | — | **Needs auth** |
| Figma | stdio via npx (GLips) | PAT (`FIGMA_TOKEN`) | 2 | Active (designer) |
| Delegate | local Python script | — (Claude Code CLI) | 2 | Active (engineer) |

*Slack and Brave Search were dropped from the baseline in session 128 — both upstream `@modelcontextprotocol` packages were deprecated mid-2025. Revisit as a post-launch power-up once actively-maintained community forks are evaluated.*

---

## Linear (31 tools)

### Tool Inventory

| Tool | Description | Required Params | Notes |
|------|-------------|-----------------|-------|
| `list_issues` | List issues in workspace | — | `assignee="me"` for own issues; `state`, `team`, `project` filters |
| `get_issue` | Retrieve issue by ID | `id` | Includes attachments and git branch name |
| `save_issue` | Create or update issue | `title` (create) / `id` (update) | Upsert pattern — same tool for create+update |
| `list_issue_statuses` | List statuses in a team | `team` | Required to know valid state values before saving |
| `get_issue_status` | Get status by name or ID | `id`, `name`, `team` | Useful for resolving "Done" → actual ID |
| `list_issue_labels` | List labels in workspace/team | — | Optional `team` filter |
| `create_issue_label` | Create a new label | `name` | Optional color, team |
| `list_teams` | List all teams | — | — |
| `get_team` | Get team details | `query` | Accepts name or ID |
| `list_users` | List workspace users | — | Optional `team` filter |
| `get_user` | Get user details | `query` | Accepts name or ID |
| `list_projects` | List projects | — | Optional `team`, `state` filters |
| `get_project` | Get project details | `query` | Accepts name or ID |
| `save_project` | Create or update project | `name` (create) / `id` (update) | Upsert |
| `list_project_labels` | List project labels | — | Separate from issue labels |
| `list_milestones` | List milestones in a project | `project` | — |
| `get_milestone` | Get milestone by name or ID | `project`, `query` | — |
| `save_milestone` | Create or update milestone | `project` | Upsert |
| `list_cycles` | List cycles for a team | `teamId` | — |
| `list_comments` | List comments on an issue | `issueId` | Supports pagination |
| `save_comment` | Create or update comment | `body` | `id` for update; `issueId` for create |
| `delete_comment` | Delete a comment | `id` | — |
| `get_document` | Get document by ID or slug | `id` | — |
| `list_documents` | List workspace documents | — | Filterable by project, creator |
| `create_document` | Create a new document | `title` | Optional project link |
| `update_document` | Update an existing document | `id` | — |
| `get_attachment` | Get attachment by ID | `id` | — |
| `create_attachment` | Attach file to issue (base64) | `issue`, `base64Content`, `filename`, `contentType` | Base64 upload — large files will be a problem |
| `delete_attachment` | Delete attachment by ID | `id` | — |
| `extract_images` | Fetch images from markdown | `markdown` | Used to view embedded screenshots |
| `search_documentation` | Search Linear's own docs | `query` | Meta-tool — LLM can learn Linear features |

### Workspace Snapshot (tested April 11, 2026)

- **Teams:** Product (PRO), Engineering (ENG)
- **Projects:** "DevTeam | Product Docs" (Product only), "Devteam" (cross-team, both Product + Engineering)
- **Users:** jojo, michael, nate, ariel, tyler (display names are lowercase email prefixes), plus Linear + Codex bots

**Status names by team** — these differ and are a primary source of LLM errors:

| Status Name | Type | Product | Engineering |
|-------------|------|---------|-------------|
| Backlog | backlog | ✅ | ✅ |
| Sprint Ready | backlog | ❌ | ✅ |
| Todo | unstarted | ✅ | ❌ |
| Ready for dev | unstarted | ❌ | ✅ |
| In Progress | started | ✅ | ✅ |
| Done | completed | ✅ | ✅ |
| Canceled | canceled | ✅ | ✅ |
| Duplicate | canceled | ✅ | ✅ |

### Test Matrix

| ID | Description | Type | Status | Fail Mode | Hint Derived |
|----|-------------|------|--------|-----------|--------------|
| L1 | "What's on my plate?" | Indirect | ✅ | `includeArchived=true` (default) silently swamps results with archived/completed work — 50 issues returned, all stale. With `includeArchived=false`: 3 active issues, ~3KB, clean. State type filter `state="started"` works correctly across teams (returns "In Progress" issues regardless of display name). Earlier empty result was genuine — no active issues existed at test time, not a filter bug. | Always pass `includeArchived=false`; state types (started/unstarted/backlog) are reliable cross-team filters |
| L2 | "Create an issue titled X" (no team specified) | Indirect | ✅ | Clean error: `"team is required when creating an issue"` — LLM can recover | Ask which team before creating; two teams exist: Product and Engineering |
| L3 | "Create an issue titled X in [team]" (named team) | Direct | ✅ | None — defaults to Backlog, unassigned. No assignee in response if not set. | Confirm assignee and priority before creating if not specified |
| L4 | "Show me all open issues" (no filters) | Indirect | ❌ | `limit=250` → **185k chars** — always exceeds context window; tool result archived before LLM sees it | Never call `list_issues` without at minimum a `team` or `project` filter; always add `includeArchived=false` |
| L5 | "What issues are in [project]?" | Indirect | ⚠️ | `project="Devteam"` + `includeArchived=false` → 55.1KB — still over 50k truncation threshold | Add `state` or `assignee` filter alongside project; or reduce `limit` to 20–25 for summaries |
| L6 | "Mark issue X as done / canceled" | Direct | ⚠️ | `state="Canceled"` resolves to "Duplicate" — two canceled-type statuses cause non-deterministic resolution. Retrying with same name doesn't fix it. | After `list_issue_statuses`, use exact status name from that team's list; if two statuses share a type, use the status ID to avoid ambiguity |
| L7 | "Assign issue X to [person]" | Chained | ✅ | `get_user(query="ariel")` resolves correctly from first name. User list has clear display names. | No special handling needed; first name lookup reliable |
| L8 | "Add a comment to issue X" | Direct | ✅ | None — response includes id, body, timestamps, author. Clean round-trip. | No special handling needed |
| L9 | "Create an issue with a very long description" (~1700 chars) | Edge | ✅ | None — full description echoed back intact. No truncation on write. | No special handling needed |
| L10 | Paginated results — large boards | Edge | ✅ | `hasNextPage: true` with cursor returned when results truncated at 50; LLM won't auto-follow pages | Always check `hasNextPage`; for "show all" requests, follow cursor or warn user results may be partial |
| L11 | Team name — partial / wrong case | Edge | ✅ | None — `team="eng"` resolves correctly. Case-insensitive, partial-match tolerant. | No special handling needed |
| L12 | "What projects are we working on?" | Indirect | ✅ | Projects have empty `description` and `summary` fields; `lead` is `{}`. Metadata alone is not useful. | Follow up with `list_issues(project=..., state=unstarted, includeArchived=false)` for meaningful summary |
| L13 | "Create a document about X" | Direct | ⚠️ | `create_document(title=...)` with no project/issue → error: `"Either project or issue must be specified"` — schema only marks `title` required, so this is a silent schema mismatch. Works correctly once project is provided. No `delete_document` tool exists — documents must be deleted manually from Linear UI. | Always ask which project (or issue) to attach a document to before calling; warn user documents cannot be deleted via Operator |

### Fail Modes Found

| # | Fail Mode | Trigger | Severity |
|---|-----------|---------|----------|
| 1 | **Unfiltered `list_issues` blows context** | Any call without team/project/assignee filter | 🔴 Critical — 185k chars, always archived |
| 2 | **`includeArchived=true` default returns stale work** | `assignee="me"` with no archive filter | 🔴 Critical — LLM presents completed issues as current |
| 3 | **State display names differ by team — use types instead** | `state="Todo"` returns empty on Engineering (uses "Ready for dev"). State types (started/unstarted/backlog/completed/canceled) work reliably across both teams. | 🟠 High — silent empty result when using wrong display name; types are safe |
| 4 | **Dual canceled-type statuses cause wrong state resolution** | `state="Canceled"` in Engineering team resolves to "Duplicate" | 🟠 High — issue set to wrong terminal state |
| 5 | **Project-level `list_issues` still over truncation limit** | Any active project with 50+ issues | 🟡 Medium — result archived, LLM gets partial data |
| 6 | **Issues created unassigned by default** | `save_issue` without `assignee` | 🟡 Medium — LLM doesn't prompt for assignee |
| 7 | **`hasNextPage` silently truncates board queries** | Any query returning 50+ issues | 🟡 Medium — LLM presents partial list as complete |
| 8 | **Issue descriptions truncated in `list_issues`** | Issues with long descriptions or images | 🟢 Low — message says "use `get_issue` for full description" |
| 9 | **Image URLs in descriptions are long JWT-signed blobs** | Issues with embedded screenshots | 🟢 Low — eats context budget; use `get_issue` + `extract_images` if needed |
| 10 | **`create_document` requires project or issue — not in schema** | `create_document(title=...)` alone | 🟡 Medium — LLM will try to create standalone docs and fail |
| 11 | **No `delete_document` tool** | Any document created in error | 🟢 Low — user must delete manually from Linear UI |

### Hints (draft — feeds into step 10.11)

```
Linear hints:
- Never call list_issues without at least one filter (team, project, or assignee). Unfiltered results exceed the context window.
- Always set includeArchived=false unless the user explicitly asks for archived issues.
- Use state types (unstarted, started, backlog, completed, canceled) not display names — status names differ by team. "Todo" is Product team only; Engineering uses "Ready for dev".
- When filtering by a specific status name (e.g. to cancel an issue), call list_issue_statuses first and use the exact name. If two statuses share a type, use the status ID.
- For issue detail, use get_issue not list_issues — list_issues truncates descriptions.
- For project summaries, follow up list_projects with list_issues(project=..., state=unstarted, includeArchived=false).
- User lookup by first name works reliably (get_user with "ariel" resolves to Ariel Spiegel).
- If hasNextPage=true, the result is partial — tell the user and offer to fetch more.
- When creating an issue, confirm team, assignee, and priority if not stated.
- create_document requires a project or issue attachment — ask which project before calling. There is no delete_document tool; warn user if a document is created in error.
- Long issue descriptions (1700+ chars) are handled correctly — no special treatment needed.
```

---

## GitHub (41 tools)

### Tool Inventory

| Tool | Description | Required Params | Notes |
|------|-------------|-----------------|-------|
| `get_me` | Get authenticated user details | — | Call at startup to resolve login; LLM guesses wrong owner without this |
| `get_file_contents` | Get file or directory contents | `owner`, `repo` | Returns `EmbeddedResource` for file content — extract via `c.resource.text`, not `c.text` |
| `search_code` | Search code across GitHub | `query` | Unreliable for small/new repos — returns empty without error |
| `search_repositories` | Find repos by metadata | `query` | `minimal_output=true` reduces result size |
| `search_issues` | Search issues with query syntax | `query` | Scoped to `is:issue` automatically |
| `search_pull_requests` | Search PRs with query syntax | `query` | Scoped to `is:pr` automatically |
| `search_users` | Find GitHub users | `query` | — |
| `list_issues` | List issues in a repo | `owner`, `repo` | Pagination via `after` cursor |
| `issue_read` | Get issue details | `owner`, `repo`, `issue_number`, `method` | `method` controls what's returned |
| `issue_write` | Create or update issue | `owner`, `repo`, `method` | `method` controls create vs update |
| `add_issue_comment` | Add comment to issue/PR | `owner`, `repo`, `issue_number`, `body` | Works on PRs too (same number space) |
| `list_pull_requests` | List PRs in a repo | `owner`, `repo` | Do NOT use if filtering by author — use `search_pull_requests` instead (per tool desc) |
| `pull_request_read` | Get PR details | `owner`, `repo`, `pullNumber`, `method` | — |
| `create_pull_request` | Create a PR | `owner`, `repo`, `head`, `base`, `title` | — |
| `update_pull_request` | Update PR metadata | `owner`, `repo`, `pullNumber` | — |
| `update_pull_request_branch` | Sync PR branch with base | `owner`, `repo`, `pullNumber` | — |
| `merge_pull_request` | Merge a PR | `owner`, `repo`, `pullNumber` | — |
| `pull_request_review_write` | Create/submit/delete review | `owner`, `repo`, `pullNumber`, `method` | — |
| `add_comment_to_pending_review` | Add inline review comment | `owner`, `repo`, `pullNumber`, `path`, `body`, `subjectType` | Requires pending review first |
| `add_reply_to_pull_request_comment` | Reply to a review comment | `owner`, `repo`, `pullNumber`, `commentId`, `body` | — |
| `request_copilot_review` | Request Copilot review | `owner`, `repo`, `pullNumber` | — |
| `assign_copilot_to_issue` | Assign Copilot to issue | `owner`, `repo`, `issue_number` | — |
| `create_branch` | Create a branch | `owner`, `repo`, `branch` | Optional `from_branch` |
| `list_branches` | List branches | `owner`, `repo` | — |
| `create_or_update_file` | Create/update single file | `owner`, `repo`, `path`, `branch`, `content`, `message` | Content must be base64 |
| `push_files` | Push multiple files in one commit | `owner`, `repo`, `branch`, `files`, `message` | — |
| `delete_file` | Delete a file | `owner`, `repo`, `path`, `branch`, `message` | — |
| `get_commit` | Get commit details | `owner`, `repo`, `sha` | Optional diff |
| `list_commits` | List commits on a branch | `owner`, `repo` | 30+ per page default |
| `create_repository` | Create a new repo | `name` | — |
| `fork_repository` | Fork a repo | `owner`, `repo` | — |
| `get_latest_release` | Get latest release | `owner`, `repo` | — |
| `get_release_by_tag` | Get release by tag | `owner`, `repo`, `tag` | — |
| `list_releases` | List releases | `owner`, `repo` | — |
| `get_tag` | Get tag details | `owner`, `repo`, `tag` | — |
| `list_tags` | List tags | `owner`, `repo` | — |
| `get_label` | Get a specific label | `owner`, `repo`, `name` | — |
| `list_issue_types` | List issue types for org | `owner` | — |
| `get_teams` | Get teams user is member of | — | Limited to accessible orgs |
| `get_team_members` | Get team member usernames | `org`, `team_slug` | — |
| `sub_issue_write` | Add sub-issue to parent | `owner`, `repo`, `issue_number`, `sub_issue_id`, `method` | — |

### Known Fail Modes (pre-testing)

These were discovered during Phase 9 hardening:

| Fail Mode | Trigger | Fix Applied | Hint Needed? |
|-----------|---------|-------------|--------------|
| `search_code` returns empty for small repos | Any `search_code` on low-activity repo | Startup hint: steer LLM away | ✅ already in system prompt |
| LLM guesses `owner` from display name | Any tool requiring `owner` param | `resolve_github_user()` at startup injects login | ✅ already injected |
| `get_file_contents` returns `EmbeddedResource` | Reading any file | Extract `c.resource.text` in `_execute_tool` | Fixed in code |
| Deprecated npm package fails `search_code` auth | Using old `@modelcontextprotocol/server-github` | Replaced with Go binary | N/A |
| `parallel_tool_calls` crashes on multi-tool response | LLM returns 2+ tool calls | `parallel_tool_calls=False` | Fixed in code |
| Follow-up tool call loses tools list | Chained calls via `send_tool_result` | Pass `tools` kwarg in follow-up | Fixed in code |

### Test Matrix

| ID | Description | Type | Status | Fail Mode | Hint Derived |
|----|-------------|------|--------|-----------|--------------|
| G1 | "What PRs are open on my repo?" (no repo specified) | Indirect | ✅ | LLM resolves owner via `get_me` (already injected at startup), then needs a repo — user disambiguates. No crash. | Keep owner injection; prompt for repo name when ambiguous |
| G2 | "What PRs are open on [repo]?" (repo specified) | Direct | ✅ | Clean — `list_pull_requests` returns expected shape. | None |
| G3 | "Find where auth is handled in [repo]" | Indirect | ❌ | `search_code` returns 0 results on small/new repo (operator fixture) even when terms exist in the tree. Confirms pre-existing known fail mode. No error raised — silent empty. | Already covered: steer LLM to `get_file_contents` + directory traversal instead of `search_code` on small repos |
| G4 | "Read [file] and explain it" | Chained | ✅ | `get_file_contents` on small text file works cleanly. Content extracted from `EmbeddedResource.resource.text`. | None |
| G5 | "Read [file], then read the file it imports" | Deep chain | ⚠️ | Works, but required explicit `ref=<branch>` — on non-default branch (`test/pr-fixture`), calling without `ref` silently returned main's copy of the path (404 since file only existed on the feature branch). | Always pass `ref` when file lives on a non-default branch; don't trust implicit default |
| G6 | Large file retrieval (>50k chars) | Edge | ❌ | 109,337-char `test/large_log.txt` → **single tool result consumed 164.3k tokens / 82% of context window** in one call. Claude Code auto-archived the result but Operator's pipeline has no equivalent. No `max_bytes` / `offset` / `limit` params on `get_file_contents`. | **Critical:** Check file size via parent directory listing (returns `size` field) before calling `get_file_contents`. Refuse or warn on files >~30KB. |
| G7 | Binary/image file retrieval | Edge | 🔴 | `test/screenshot.png` (351KB PNG) returned as inline `[image]` block in tool result. **Image got embedded in conversation context and poisoned subsequent API calls with HTTP 400 `"Could not process image"` on every message until `/compact`** — even `/exit` and `/end-session` failed. No way to recover mid-session. | **Critical:** Never call `get_file_contents` on binary/image files. Detect by extension (`.png .jpg .jpeg .gif .webp .pdf .zip .tar .gz .onnx .bin` etc.) and refuse before invoking. |
| G8 | "List commits on main" | Direct | ✅ | `list_commits` returns 30 commits with full author/committer/verification blocks per entry. Reasonable size (~15KB for 30). | Use `perPage` to cap when only recent commits needed |
| G9 | "Create a GitHub issue titled X" | Direct | ❌ | Fine-grained PAT (`GITHUB_TOKEN`) is git-protocol scoped; REST calls for `issue_write`, `create_pull_request`, `create_branch`, `create_or_update_file` all return **HTTP 403 "Resource not accessible by personal access token."** User completed these via web UI. | PAT token scope is an env-setup problem, not a tool-hint problem. Document in setup: fine-grained PATs need explicit Contents/Issues/Pull Requests permissions |
| G10 | "Show open issues assigned to me" | Indirect | ⚠️ | `list_issues` with `assignee=me` returns each issue with a **full user object for creator + assignees + repository object** — ~3KB per issue of redundant nesting. 3 issues ≈ 9KB. Scales poorly. | Use `minimal_output=true` when available; prefer `search_issues` with tight query over `list_issues` for summaries |
| G11 | Directory listing (no file path) | Edge | ⚠️ | Directory listing returns each entry with `_links.self`, `_links.git`, `_links.html`, `url`, `git_url`, `html_url`, `download_url` — **~10× context bloat vs. just name+path+size+sha**. 30 entries ≈ 8KB. | Directory listings are safe but verbose; fine for single-level navigation. Don't recursively list large trees. |
| G12 | Multi-repo ambiguity ("my operator repo") | Edge | ✅ | Unscoped `search_repositories(query="operator in:name")` returns **63,680 repos** — first 5 all Kubernetes brainchilds, none the user's. Scoping with `user:dufis1` narrows to 1 result cleanly. | Always scope repo search with `user:<login>` (from `get_me`) or `org:<org>` when the user refers to "my" or "our" repos |
| G13 | "What repos do I have?" | Indirect | ✅ | `search_repositories(query="user:dufis1", perPage=100)` → 2 repos, ~500 bytes with `minimal_output=true`. Clean. | Use `search_repositories` with `user:<login>` and `minimal_output=true` rather than any listing call |

### Fail Modes Found (session 74)

| # | Fail Mode | Trigger | Severity |
|---|-----------|---------|----------|
| 1 | **Large text file poisons context** | `get_file_contents` on any file >~30KB | 🔴 Critical — single call can consume 80%+ of context window; Operator has no auto-archive fallback |
| 2 | **Binary/image read poisons entire session** | `get_file_contents` on `.png/.jpg/.pdf/.bin` etc. | 🔴 Critical — image embeds into context, API returns HTTP 400 on every subsequent request until `/compact`; session is bricked with no recovery |
| 3 | **`search_code` silently empty on small repos** | Any `search_code` on low-activity repo | 🟠 High — no error, just 0 results; LLM reports "not found" falsely (pre-existing, already hinted) |
| 4 | **Non-default branch reads return wrong content without `ref`** | `get_file_contents` on file that only exists on feature branch, no `ref` param | 🟠 High — silently returns main's version or 404; easy to miss |
| 5 | **Fine-grained PAT blocks all write tools with 403** | `issue_write`, `create_branch`, `create_or_update_file`, `create_pull_request` with git-protocol-only PAT | 🟠 High — env-setup issue, user sees opaque "Resource not accessible" |
| 6 | **`list_issues` response bloat** | Each issue carries full user + repository objects (~3KB/issue) | 🟡 Medium — 20 issues ≈ 60KB |
| 7 | **Directory listing returns 6 redundant URL fields per entry** | Any `get_file_contents` on a directory path | 🟡 Medium — ~10× bloat vs. name/path/size/sha only |
| 8 | **Unscoped repo search returns 60k+ results** | `search_repositories(query="operator")` without `user:` / `org:` scope | 🟡 Medium — LLM may pick wrong repo when user says "my repo" |

### Hints (draft)

```
GitHub hints:
- Never call get_file_contents on files larger than ~30KB. Check size first via a directory listing (parent path) — each entry includes a `size` field. Large files will blow the context window.
- Never call get_file_contents on binary/image files (.png .jpg .jpeg .gif .webp .pdf .zip .tar .gz .onnx .bin etc.). Inline image returns poison the conversation and cause HTTP 400 on all subsequent API calls until the session is compacted.
- When reading a file on a non-default branch, always pass ref=<branch>. Without ref, the API returns main's copy (or 404) silently.
- Avoid search_code on small/new repos — it returns 0 results without error. Use get_file_contents to browse the tree instead.
- When the user refers to "my repo" or "our repo", scope search_repositories with user:<login> (from get_me) or org:<org>. Unscoped queries return tens of thousands of unrelated results.
- Prefer search_issues / search_pull_requests with tight queries over list_issues / list_pull_requests — the list_* tools return verbose user+repository objects per entry (~3KB each).
- Set minimal_output=true on search_* tools whenever full API objects aren't needed.
- For directory navigation, a single-level listing is fine but verbose (~6 redundant URL fields per entry). Don't traverse recursively.
- For write tools (issue_write, create_branch, create_or_update_file, create_pull_request, merge_pull_request): if you get HTTP 403 "Resource not accessible by personal access token", the PAT is missing Contents/Issues/Pull Requests permissions. This is an env-setup issue — tell the user rather than retrying.
- Owner injection at startup (from get_me) is already handled; LLM should use the resolved login, not guess from display name.
```

---

## Notion (tools TBD)

> **Status: Needs auth.** Enable `notion` block in `config.yaml`, add `NOTION_API_KEY` to `.env`, then call `list_tools()`.

### Tool Inventory

*Not yet populated — pending auth setup.*

### Test Matrix

| ID | Description | Type | Status | Fail Mode | Hint Derived |
|----|-------------|------|--------|-----------|--------------|
| N1 | List all tools — catalog page vs. database vs. block tools | Discovery | ⬜ | | |
| N2 | "Create a meeting notes page" (no parent specified) | Indirect | ⬜ | | |
| N3 | "Create a page in [database]" | Direct | ⬜ | | |
| N4 | "Search for docs about X" | Direct | ⬜ | | |
| N5 | "Find our engineering runbook" (no exact title) | Indirect | ⬜ | | |
| N6 | "Make a note about this" | Indirect | ⬜ | | |
| N7 | Page with rich content (tables, embeds) — result size | Edge | ⬜ | | |
| N8 | Nested page retrieval — does it traverse or just top-level? | Edge | ⬜ | | |
| N9 | Page vs. database entry — does LLM pick correctly? | Edge | ⬜ | | |

### Fail Modes Found

*Not yet tested.*

### Hints (draft)

*To be filled in after testing.*

---
