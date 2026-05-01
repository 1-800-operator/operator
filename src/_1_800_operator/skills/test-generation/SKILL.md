---
name: test-generation
description: Generate tests for a function or module — use when the user says "write tests for X", "add tests for Y", "we need tests here", or asks for test coverage on a specific symbol.
mcp-required: [claude-code]
---

# Test generation

When the user asks for tests on a specific function, class, or module,
delegate to `claude-code` to generate a test file in a worktree branch — the
delegated session reads sibling test files first to match the project's
existing style, then writes tests that actually pass before returning.

## Before you write

Delegate to `claude-code` with a task string that names: (a) the symbol to
test (file + function/class), (b) the explicit instruction to **read 3–5
existing test files in the same project first** and match their framework,
fixtures, assertion idiom, and naming conventions, (c) the test cases to
cover (happy path + edge cases — see Shape), (d) the requirement that
tests must actually pass before the worktree returns.

If the user only named a function ("test `validate_payload`") without a
file, ask which file before delegating — guessing wastes the worktree's
turnaround time.

If `claude-code` is not enabled on this agent, fall back to the chat-only
path: read the function's source via whatever read-capable MCP is
available, compose a test file as inline chat content, and explicitly tell
the user "this is unverified — paste it into your project and run the
tests; I couldn't validate it ran cleanly without claude-code."

## Shape

Emit the chat-side reply as a short summary; the actual test code lives in
the worktree branch.

```
**Branch** — `<worktree-branch-name>` — the test file lands here.

**File** — `<test/file/path>` — where the new tests live.

**Cases** — bulleted list of the test cases, one line each:
  - `test_<happy_path_name>` — happy path: <input shape> → <expected output>.
  - `test_<edge_case_1>` — edge: <boundary or error condition>.
  - `test_<edge_case_2>` — edge: <another condition>.
  - (more if the function's surface justifies it)

**Style match** — one line on what existing pattern was followed (e.g. "matched `tests/test_session.py` — pytest + factory fixtures + `assert` style, no `unittest.TestCase`").

**Status** — "passing locally in worktree" if verified, "see notes" if the worktree returned with caveats.
```

## Rules

- **Read sibling tests first.** This is the killer feature and the
  reason `claude-code` is the right primitive: real test files import
  fixtures, conftest helpers, factories, mocks. Matching the project's
  existing patterns is what makes generated tests useful instead of
  dropped on the floor. The delegated task string must say "read 3–5
  existing test files first" — not optional.
- **Cover happy path + 2–3 edges, not 12.** Tests that exhaustively
  enumerate every possible input are a smell. Pick: one happy case,
  the boundary conditions (empty input, max size, off-by-one), and the
  obvious error paths (bad type, missing field). If the function's
  contract justifies more, say so in the chat reply and offer to extend.
- **Verify pass before returning.** The delegated session runs the test
  suite in the worktree before returning. If tests fail, the session
  fixes the failures first — never return a worktree with red tests.
  If the failure is on existing tests (not the new ones), flag that
  separately in the chat summary; do not silently fix unrelated test
  bugs.
- **No mocking the system under test.** Tests that mock the function
  they're testing teach nothing. Mock at the dependency boundary
  (DB, HTTP, time, randomness) — not the function itself.
- **Match assertion idiom.** Project uses `assert x == y`? Don't emit
  `self.assertEqual`. Project uses pytest fixtures? Don't add
  `setUp`/`tearDown`. The delegated session reads to match — chat
  reply confirms the match.
- **One message in chat.** The tests live in the branch; chat
  carries the summary. If the cases list is long, lead with branch +
  file + first three cases, then "(cont.)" with the rest.
- **Do not edit the function being tested.** This skill writes tests,
  not refactors. If the function is genuinely untestable as written
  (e.g., reaches into globals), say so in the chat reply and offer to
  delegate a separate refactor — do not silently rewrite the code in
  the same worktree.
