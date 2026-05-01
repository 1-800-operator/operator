---
name: standup-summary
description: Post a structured recap of the meeting — use when the user asks to "summarize", "wrap up", "recap", or "send the standup notes".
---

# Standup summary

When the user asks for a recap, wrap-up, or standup summary, post a
structured version of what happened in the meeting. Pull from chat history
and any `<spoken>` caption blocks in context.

## Shape

```
**Decisions**
- <decision> — <who made it, if named>

**Action items**
- <owner>: <action> (<due date, if given>)

**Blockers**
- <person>: <what they're stuck on, who/what they need>

**Tickets filed**
- <LINEAR-ID>: <title> — <url>

**Open questions**
- <unresolved thing the group raised>
```

## Rules

- **Only include sections with content.** An empty "Blockers" section is
  noise — drop it. A meeting with no decisions is a valid recap of no
  decisions; just skip the header.
- **Owners by name.** If someone committed ("I'll draft the spec by
  Thursday"), attribute by their display name. If the commitment was
  anonymous ("we should probably look at that"), put it under open
  questions, not action items.
- **Only list tickets this session filed.** Don't dump the whole Linear
  backlog. If the bot filed tickets during this meeting, list them with
  URLs. If it didn't, skip the section.
- **Absolute dates.** Convert "Friday" → "2026-04-17" relative to the
  meeting date. Relative dates rot.
- **One message.** If it's longer than chat can comfortably render, split
  into two messages with the second prefixed "(cont.)". Don't truncate.
- **Do not editorialize.** No "great meeting, team!" — just the structure.
