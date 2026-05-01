# Operator MVP Bar

*Established: April 12, 2026. Revisit and adjust as needed, but don't expand without a strong reason.*

---

## Positioning

The only open-source meeting bot that *does things*, not just watches. **"Meetings that produce artifacts, not just words."**

The AI power user's meeting bot — your Claude Code skills, your MCP servers, your model, in every meeting. Operator reads Claude Code skills natively, and any markdown skill file, with any model.

## The "Aha" Moment

You're in a Google Meet. You type "operator, create a ticket for the auth bug Alice just described." It creates the Linear ticket, right there in chat, in front of everyone. Nobody else does this.

## Three-Part Differentiation

1. **Action, not access.** OpenUtter/Vexa/Fireflies watch and record — the transcript category. Joinly exposes the meeting *to* agents as middleware — the access category. Operator *is* the agent, pre-wired. MCP tool use during a live meeting, producing concrete artifacts (tickets, PRs, docs), is the capability moat.
2. **Your AI, not a generic bot — and specifically, your Claude Code skills in a meeting.** Operator imports `~/.claude/skills/` natively, so the agent knows your codebase conventions, your team's ticket format, your preferences, from day one. Skill files are portable markdown; they work with OpenAI or Anthropic, with any skills directory a user points at. The AI-power-user customization layer, surfaced into meetings — this is the sharpest implicit differentiator and the lead framing for launch.
3. **Opinionated quickstart, not a framework.** "Choose your fighter → add power-ups → go." Ship ready-to-run agents in `agents/` (see `agents/README.md`) as the "choose your fighter" layer — copy a folder, fill in keys, run. An interactive `python -m operator build` wizard writes the config. README reads as Steps 1/2/3. No Docker, no daemon, no managed platform, no $20/mo SaaS. BYOK — paste your key, go. Open source, MIT licensed.

## What Must Work

- **Fresh install works in one sitting.** Clone, follow README, configure API keys, run, join a meeting. No wizard needed — the audience can follow a README — but the README must be complete and accurate.
- **Chat interaction in Google Meet.** Bot reads messages, responds intelligently, stays in character per the user's system prompt / skill files.
- **MCP tool use with Linear and GitHub.** These two are pressure-tested and work reliably. Known failure modes are handled gracefully.
- **Model choice: OpenAI + Anthropic.** Users pick their provider. No local model support at MVP — local models are too weak at agentic tool use and would undermine the demo.
- **Customization layer.** Users can provide a system prompt and/or skill files so the bot reflects their identity, preferences, and context. This is what makes it "your AI" and not Gemini.
- **Graceful failure.** When something goes wrong, the bot says so in chat ("I tried but Linear returned an error: ...") rather than hanging, crashing, or silently doing nothing.
- **BYOMCP doesn't brick the bot.** Users can add their own MCP servers. Untested servers get defensive defaults (result size caps, execution timeouts, conservative confirmation mode). README documents known failure patterns and how to write hints to mitigate them.
- **Linux works.** Mac + Linux is in the positioning. Dedicated testing session on a real Linux box before launch.
- **Clean presentation.** Stdout during normal operation is clean and readable (no debug spam, no stack traces for expected errors). Config file has no dead keys or confusing options. The detailed debug log stays in `/tmp/operator.log`.
- **The 3-step setup is obvious.** A non-expert reader of the README identifies the three steps (choose an agent → add your keys/power-ups → join a meeting) in under 30 seconds. The `agents/` gallery has at least 2 ready-to-run agents at launch — `claude-code` is the canonical example.

## What Is NOT in the MVP

- **Voice** — massive surface area, layered post-launch. Explicitly **not chasing voice parity with Joinly** at MVP; chat-with-artifacts is the wedge.
- **Zoom / Teams parity with Joinly** — Google Meet only at launch. Platform breadth is their lane; don't feature-match.
- **Local LLM support** — too weak at tool use, would create bad first impressions; add as experimental tier later
- **Setup wizard** — README is fine for this technical audience
- **More than 2 fully-tested MCP servers** — Linear + GitHub ship tested; others can be added by users with documented guidance
- **Zoom/Teams** — Google Meet only at launch; other platforms are a fast follow
- **Comprehensive edge case testing, DOM self-healing, regression suites**
- **Telemetry / diagnostics**
- **CI/CD pipeline, PyPI publishing**
- **Latency audit** — chat doesn't have voice-level latency sensitivity
- **Comprehensive error handling pass** — defensive MCP guardrails cover the scariest path

## Launch Strategy

Launch is a positioning fight more than a product fight. Joinly has shipped most of the surface 6 months ago; their posts got meager response because the pitch is abstract ("expose meetings to agents"). We win by being specific, visual, and seeded.

### Hero framings — specific JTBD posts, never a generic "open source X" post

Each framing maps to a primary channel and a concrete "aha" artifact. Post the specific framing, not the generic one.

- **"Claude Code in your Google Meet"** — target: r/ClaudeAI, r/LocalLLaMA, Claude-focused X/Bluesky creators. Aha: the bot adopts the user's existing `~/.claude/skills/` library with zero migration.
- **"The AI in my standup filed 3 Linear tickets before I finished talking"** — target: r/ExperiencedDevs, eng-leader newsletters, LinkedIn. Aha: the ticket appears in chat mid-sentence.
- **"I built a [role-specific] bot for my [role-specific] calls in 20 minutes"** — target: the relevant niche subreddit + LinkedIn. Aha: a tiny `agents/<name>/` folder becomes a working agent. (Exact use case TBD — translator is voice-native; needs a chat-native replacement: standup, triage, incident-commander, interview-notes, research are candidates.)

### Visual hooks — every demo must land at least one

- **Artifact appearing in chat while the speaker is still talking.** The surprise of a Linear ticket or GitHub comment materializing mid-sentence is the single most filmable thing this product does. Pika's anime-avatar moment is our artifact-in-chat moment.
- **Drag-and-drop a skills folder and watch behavior change.** Tangible proof of the "your AI" claim.
- **One `agents/` folder → working agent in three commands.** The opinionated-quickstart promise, on film.

Rule: before posting any demo, identify the *one* highest-value feature it showcases and cut anything that dilutes it. Joinly's posts failed partly by showing the full surface at once.

### Distribution levers, ranked

1. **Direct outreach to 10–20 Claude Code power users in SF.** In-person or DM > any subreddit. The Claude Code community is small, vocal, and co-located.
2. **Paid or earned mentions from AI / PM newsletter writers.** Pika's playbook — a single senior PM with a 90k-follower LinkedIn newsletter drove 2k likes on their demo. Prioritize AI newsletter brainchilds who also post product walkthroughs on LinkedIn.
3. **Seeded recipes in `agents/`.** Each seeded persona contributes one agents-member PR, then posts about their build. Real users browsing the repo see contributor activity on `main` — social proof as code.
4. **Reddit posts — specific-JTBD titles only.** Never "we built an open-source meeting bot." Always a concrete build or result.

### `agents/` gallery as distribution primitive

The `agents/` folder is both product and marketing. Each ready-to-run agent is a working demo *and* a post. The folder grows via PRs, which makes the repo visibly active and lowers contribution friction. Seed with 2 agents at launch (`claude-code` + one chat-native second), target 5 within week 1 via seeded PRs.

---

## The Reddit Test

A technical user clones it, follows the README, gets it into a meeting, asks it to do something with Linear or GitHub, and it works. They think "oh that's cool, I could customize this and hook up my own tools." They don't hit a crash. They see it's model-agnostic. That's a win.
