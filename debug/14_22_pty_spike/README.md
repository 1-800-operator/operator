# 14.22 PTY-drive interactive claude spike

## Why

Anthropic announced (2026-05-13) that starting **2026-06-15**, `claude -p`
and Agent SDK usage will be billed against a separate monthly credit
pool (Pro $20, Max 20x $200) instead of the user's interactive
subscription limits. Operator currently uses `claude -p --resume <id>`
for every meeting; under the new rules, a Max 20x user with operator
running on their daily calls would burn through $200/mo of credit fast,
then either pay-as-you-go API rates or stop.

This spike derisks the **engineering** side of a candidate workaround:
spawning interactive `claude` (no `-p`) under a pseudo-terminal, typing
the user message into it, and reading the rendered TUI back out as
bytes. If we can extract the four signals our `ClaudeCLIProvider`
callbacks need (reply text, tool-use, denial, EOF) from those bytes, we
have a path that may stay on the interactive subscription pool.

**We cannot verify the billing question until June 15.** The point of
running this now is to have a working byte-capture + parsing prototype
ready to flip on that date.

## Running

```bash
# Easy prompt — no tool use
python spike_pty.py "what is 2 plus 2"

# Tool-heavy prompt — forces a Read call
python spike_pty.py "read spike_pty.py and tell me how many lines it has"
```

Outputs land in `./out/`:
- `raw_bytes.bin` — exact bytes read off the PTY
- `raw_bytes.hex` — hex+ASCII dump
- `clean_text.txt` — ANSI-stripped best-effort
- `summary.txt` — metadata

## What to look for

Open `raw_bytes.hex` and `clean_text.txt` side by side. Questions:

1. Is the assistant's final reply text recognizable as a contiguous run
   somewhere in the stream? Or is it broken across redraws?
2. When claude calls a tool, what pattern marks tool-use start/end?
   (Bullets, box-drawing chars, specific labels?)
3. Are there distinct markers for permission prompts or denials?
4. Does claude exit cleanly on Ctrl-D / `/exit`, or just hang?
5. How big is the byte volume per turn? (Streaming redraws can balloon it.)

Notes from this pass should feed the next iteration — a parser that
extracts those signals into the same shape `ClaudeCLIProvider`'s stream
callbacks consume today.

## Caveats

- Requires `claude` already authenticated on this machine.
- Cwd matters: `claude` reads `CLAUDE.md` from where it's launched. Run
  from a neutral dir if you don't want the operator project context to
  bleed in.
- Quiet-detection (`quiet_threshold` in the script) is a heuristic. A
  long tool loop with a thinking pause may trip it; bump the threshold
  or pass `--timeout` if you see truncated captures.
- **Does not** test billing classification. That has to wait for
  2026-06-15 and a separate verification pass.
