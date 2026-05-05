# Copy Scratchpad

*Loose pool of taglines, framing, command-naming logic, and marketing copy candidates. Drop lines in here as they emerge in conversation. The good ones surface naturally; the rest get pruned when the landing page goes from outline to draft.*

---

## Command verbs (locked-in for v0.0.1)

**slip → dial → deploy** — the three-rung ladder.

Each rung escalates one variable while holding others constant:
- **slip** — Claude responds as you; no second account; no separate participant. Lowest stakes. Try-it-out mode.
- **dial** — Claude has its own identity; you and Claude join a fresh Meet together; no other participants. Private one-on-one with AI.
- **deploy** — Claude has its own identity; joins your meeting with real participants. Full commitment, full visibility.

The ladder is the product. Slip is the entry rung. Most users will start there. Some graduate to dial. A subset reach deploy.

## One-line product description candidates

- The Claude you already trust, but in your meeting.
- Bring Claude into your Google Meet.
- Operator brings Claude into your Google Meet. Three commands.
- AI in your meeting, on your terms.

## Three-rung framing copy

- Start with slip, see what it feels like.
- Start with slip, graduate to dial once you trust it, deploy when you're ready.
- Slip Claude in. Dial Claude up. Deploy Claude.

## Verb-by-verb mental models

- **slip** — light, sneaky, low-commit. "Slip Claude into this meeting." You're in the meeting; Claude is along for the ride. Replies surface through your identity (with a marker so the room knows). The meeting feels normal; Claude's a quiet co-pilot you can summon with `@claude`.
- **dial** — purposeful, deliberate. "Dial up Claude." Echoes the 1-800-Operator brand metaphor — you're picking up the phone, calling Claude. Private call: just you and the AI in a fresh Meet room. Good for solo brainstorms, voice-style sessions, transcribed interviews with yourself.
- **deploy** — decisive, full-commitment. "Deploy Claude to my meeting." Military/devops cadence — you're sending Claude in. Claude joins the meeting as a separate named participant; everyone sees it; everyone hears it. Used for note-taking, project management, real-time research while you focus on the people.

## Honesty / AI-presence framing

- Operator is honest about AI presence in meetings. Claude's replies always carry a marker so the room knows what's you and what's Claude. (For slip mode.)
- No hidden AI. No deepfakes. Claude shows up labeled. (Stronger version.)

## Install/onboarding one-liner

```
curl -fsSL 1-800-operator.com/install | sh
```

Then `operator slip claude <meet-url>` — no signup, no API keys, no setup. (Assumes Claude Code already installed and logged in.)

## Trust ladder narration

- "You don't trust meeting bots yet. Neither do we. Start with slip — Claude responds as you, with a marker, so the room always knows. When that feels normal, dial up Claude alone for a private session. When dial feels normal, deploy Claude into your real meetings."

## Anti-pitch (what we're explicitly not)

- Not Otter. Not a transcription tool with AI bolted on.
- Not Pika. Not a UI skin over a hosted model.
- Not Recall. Not infrastructure for someone else's bot.
- Not yet a bot-builder. (That's the next product.)

## Rejected verb candidates

For the slip-rung, considered and dropped:
- **merge** — git collision; doesn't carry low-stakes/try feel.
- **channel** — strong identity-merging semantic but too metaphysical/woo for some audiences.
- **patch** — switchboard-aligned with brand but more about *connecting* than *blending*.
- **tag** — wrestling/basketball substitution; wrong shape (tag-in implies tag-out).
- **pair** — too software-jargon-coded; feels too committed.
- **shadow / ghost** — dark vibes; "ghost" especially carries social-media baggage.

For the deploy-rung, considered:
- **send** — clean but slightly weaker than deploy; deploy carries more commitment-weight.

---

## Open questions / parking lot

- Reply styling in slip mode: brackets `[Claude]`, robot emoji `🤖 `, italics `_..._` — we'll mock all three side-by-side and pick by feel.
- Does the landing page lead with the install command (function-first) or the ladder (story-first)? Probably ladder, then install, then demo.
- What's the visual for the demo asset? Three short clips, one per rung? Or one clip showing slip mode (the entry rung)?
