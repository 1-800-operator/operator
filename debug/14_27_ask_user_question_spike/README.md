# 14.27 — AskUserQuestion-in-meeting spike

## The question

Claude Code has a built-in **`AskUserQuestion`** tool that pops a multiple-choice
UI in the desktop app / terminal client — the model calls it to disambiguate
("which framework do you want?"), the user clicks a choice, and the answer
comes back as the tool result. The tool accepts 1–4 questions per call, so
in the UI the user sees a paginated form ending in a Submit page.

What happens when the **inner-claude operator drives over a PTY** calls this
tool mid-meeting? Operator has no UI — participants interact via chat, not
buttons. Three things we need to know:

1. **What does the tool call look like on operator's two read channels?**
   - PTY tail (bytes operator captures into `_pty_dump` for forensics).
   - Claude Code transcript JSONL (the structured channel operator already
     tails for real-time narration in `_run_turn._poll_transcript`).
2. **What happens if nobody answers?** Does claude block indefinitely on the
   PTY, time out, or self-cancel and continue? This governs the failure mode
   in a meeting — a meeting can't hang forever.
3. **Can we answer it back?** What input does the TUI accept — arrow keys +
   enter, a digit, something else? If we can craft a synthetic answer, an
   eventual handler could bridge the choices into chat (post the choices,
   wait for a number reply, feed the keystroke).

Note: `--dangerously-skip-permissions` does **not** suppress `AskUserQuestion`
— it's not a permission gate. So operator's current spawn hits this code
path natively the moment the model decides to use the tool.

## Trigger

Confirmed working by hand:

> Use the AskUserQuestion tool to ask me whether X or Y, then ask whether
> A or B.

This produces the two-questions-then-submit shape and is the deterministic
trigger the spike uses.

## Run it

```bash
cd /Users/jojo/Desktop/operator
source venv/bin/activate
python debug/14_27_ask_user_question_spike/spike.py
```

Needs `claude` on PATH and logged in. The script spawns a naked
`claude --dangerously-skip-permissions` in a PTY exactly the way
`pipeline/providers/claude_cli.py` does (bracketed-paste, `ANTHROPIC_API_KEY`
stripped, 120x40 winsize), fires the trigger, then for 90s captures:

- `out/run_<ts>/pty.bin`  — raw PTY bytes
- `out/run_<ts>/pty.txt`  — ANSI-stripped text rendering
- `out/run_<ts>/transcript.jsonl` — copy of the session's Claude Code transcript
- `out/run_<ts>/events.log` — timeline of observations (tool_use blocks,
  text blocks, timing)

A fresh tmpdir is used as cwd so no project `CLAUDE.md` / hooks interfere —
this gives us baseline behaviour before testing what changes inside the
real operator spawn cwd.

## Observations we expect to record

For each captured run:

- Did `AskUserQuestion` appear in transcript JSONL as a `tool_use` block,
  with the `questions` array readable as structured data?
- Did claude block (no further events) waiting for a `tool_result`, or
  self-cancel after some interval?
- What did the PTY render (some bracketed UI? a plain prompt? nothing)?
- If we send no input, how long until claude does something?

## What this spike does NOT decide

Mitigation strategy. Once we know the shape, options are:

- **System prompt nudge** in the briefing: "you have no AskUserQuestion UI
  here — answer your best guess and move on."
- **A user-facing chat bridge**: detect the tool call in the transcript,
  post the questions to meeting chat, parse a reply, synthesize a
  tool_result back to claude somehow.
- **Block the tool**: register it as denied in settings so the model never
  reaches for it.

Picking one is a follow-up. This spike just answers "what does the failure
mode look like."
