---
name: schedule-followup
description: Schedule a follow-up on Google Calendar from a spoken commitment — use when the user says "schedule", "book", "put X on my calendar", or names a follow-up meeting for a commitment that just landed.
mcp-required: [calendar]
---

# Schedule follow-up

When the user wants a follow-up on the calendar for a commitment that just
landed in the meeting, create a Google Calendar invite with the right
attendees, time, and a description that links back to the artifact the
follow-up is about (Linear ticket, PRD, PR).

## Before you write

You need the Calendar MCP enabled + authed. If it's missing, say so and
ask the user to run `operator auth <calendar>` (or set
`GOOGLE_OAUTH_CREDENTIALS`) — do not invent a meeting ID.

Pull the user's current calendar context before proposing a time:
`get-freebusy` on the proposed attendees + `list-events` on the user's
calendar for the target day. Proposing a time that conflicts burns trust.

If a Linear ticket or PRD URL was mentioned in the same breath as the
follow-up ("let's schedule a review of that PRD"), grab the URL and paste
it into the event description so the attendees land in the artifact on
click.

## Shape

Confirm the invite in one message before creating it, then create and
report. This is two messages, not one — creating an invite is a side
effect.

```
(1) **Proposed invite:**
    Title: <short, specific — "Follow-up on X" not "Quick sync">
    When: <absolute date + time + timezone>
    Duration: <minutes>
    Attendees: <names + emails, or names with "(need email)" flag>
    Calendar: <which calendar — personal, work, team>
    Description: <one-line summary + URL to the artifact>
    Create this? [y/n]

(2) After y:
    **Booked.** <event URL>
```

## Rules

- **Confirm before creating.** A wrong invite is embarrassing and
  immediately visible to all attendees. Always post phase (1) and wait
  for y before calling `create-event`.
- **Absolute dates always.** Convert "Tuesday" → "2026-04-29" using the
  meeting's date as the anchor. Relative dates rot; written-down
  absolute dates don't.
- **Timezone is the user's, not UTC.** If you don't know the user's
  timezone, ask rather than default.
- **Scope by the right calendar.** Users often have multiple calendars
  (personal / work / team). `list-calendars` first if you don't know
  which to use; ask if ambiguous.
- **Attendee emails or nothing.** If you only have a name, flag
  `(need email)` — do not guess an email from a name + domain. A broken
  invite doesn't send; a wrong-person invite does and is awkward.
- **Duration defaults to 30m.** Unless the user named a duration or the
  artifact clearly demands more (PRD reviews = 60m typically).
- **Description must carry the artifact link.** The whole point of
  this skill is "click the calendar entry, land on the Linear ticket /
  PRD / PR." Skip the link and the invite is noise.
- **Do not bulk-schedule.** This skill handles one follow-up at a
  time. If the user names three, propose one and ask if they want
  the other two too — do not silently chain three side effects.
- **On y, emit phase (2) with the event URL so the user can verify.**
  Silent success looks like failure in chat.
