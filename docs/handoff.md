# Session 251 handoff (2026-05-19)

**What got done.** Built **launch-time precision self-update** — operator now
auto-ships wheel/Python fixes (especially the brittle Meet/Chat-DOM scraping)
with no reinstall and no `/operator:update`: at the top of `operator dial`/
`wiretap` (before fork) it fetches `release-manifest.json` over HTTPS and, if a
newer wheel is published, swaps just the ~472 KB wheel (`uv tool install
--force`, ~0.2–0.6s) and re-execs into it. Hardened with a full security model
(as trustworthy as a fresh `install.sh` run, never weaker — see `selfupdate.py`
docstring) and **validated end-to-end with real releases v0.1.39→v0.1.42** on the
public repo via a fresh-install dry run on this machine. The dry run caught **3
real bugs the spike's mocks missed** — non-PEP427 wheel filename, missing git-ref
fallback, and observer-staleness on reused pages — all fixed + shipped in v0.1.41
(observer JS is now version-stamped). v0.1.42 reverts the test instrumentation
back to clean code; throwaway test releases v0.1.40/v0.1.41 were deleted, `origin`
synced, and the process is documented in `docs/release-runbook.md`. The whole
feature is live on **v0.1.42** (manifest + install.sh point at it).

**Exact next step.** No required next step — self-update is complete and validated.
Pick from carry-forwards. If continuing self-update hardening, the one deferred
piece is **client-side health-check rollback** (N failed joins → fall back to
last-known-good wheel; today's lever is the server-side manifest roll-forward in
`docs/release-runbook.md`). Otherwise the parked items below.

**Open items / no blockers.** Carry-forwards still parked: multi-speaker
speaker-attribution fix (replay corpus + loader ready at `debug/14_34_audio_replay/`),
AEC 150ms pre-shift re-eval post-Tap-migration (gated on the `docs/qa-monday.md`
§9 built-in-vs-AirPods A/B), pre-launch audit Pass 5/6/7/8, and enabling
macos-13-large Actions billing (local `scripts/build_aec3_universal.sh` covers it
meanwhile). **Reminder for any future release:** helper/aec3 changes do NOT
auto-ship (wheel-only) — follow `docs/release-runbook.md` gotcha #4. And mind the
~1–2 min raw.githubusercontent CDN lag when validating a manifest change.
