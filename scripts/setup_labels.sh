#!/usr/bin/env bash
# Reproducibly create the operator label scheme on a GitHub repo.
#
# Usage:
#   scripts/setup_labels.sh                          # defaults to 1-800-operator/operator
#   scripts/setup_labels.sh owner/repo               # custom repo
#
# Requires: gh CLI authenticated (`gh auth status`).
# Uses --force so re-runs update color/description without erroring on existing labels.

set -euo pipefail

REPO="${1:-1-800-operator/operator}"

# name|color|description
LABELS=(
  # Status / triage
  "bug|d73a4a|Something isn't working as documented or expected."
  "enhancement|a2eeef|New capability, integration, or behavior change."
  "question|d876e3|Usage / how-do-I question — usually redirects to Discussions."
  "needs-triage|fbca04|New issue awaiting initial assessment by a maintainer."
  "needs-repro|fef2c0|Cannot be acted on until reproduction steps are confirmed."
  "good-first-issue|7057ff|Approachable for someone new to the codebase."
  "help-wanted|008672|Maintainer would welcome a contributor PR on this."
  "wontfix|ffffff|Out of scope, intentional, or by design."
  "duplicate|cfd3d7|Already tracked elsewhere — link the original."

  # Area
  "area/connector|0e8a16|Browser / Meet integration (Playwright, Chrome profile, chat panel)."
  "area/mcp|0e8a16|MCP plumbing, tool execution, BYOMCP."
  "area/installer|0e8a16|curl|sh installer, packaging, uv tool install."
  "area/agent-claude|0e8a16|claude bundled agent — Claude Code in your meeting."
  "area/agent-pm|0e8a16|pm bundled agent."
  "area/agent-engineer|0e8a16|engineer bundled agent."
  "area/agent-designer|0e8a16|designer bundled agent."
)

echo "Setting up labels on $REPO"
for entry in "${LABELS[@]}"; do
  IFS='|' read -r name color description <<<"$entry"
  echo "  → $name"
  gh label create "$name" \
    --color "$color" \
    --description "$description" \
    --repo "$REPO" \
    --force >/dev/null
done

# Optional cleanup of GitHub's default labels we don't use.
# Comment out anything you want to keep.
DEFAULTS_TO_REMOVE=(
  "documentation"
  "good first issue"   # superseded by good-first-issue (hyphenated)
  "help wanted"        # superseded by help-wanted (hyphenated)
  "invalid"            # superseded by wontfix / duplicate
)
echo "Cleaning up default labels we don't use"
for name in "${DEFAULTS_TO_REMOVE[@]}"; do
  if gh label list --repo "$REPO" --limit 200 --json name --jq '.[].name' | grep -Fxq "$name"; then
    echo "  → removing $name"
    gh label delete "$name" --repo "$REPO" --yes >/dev/null
  fi
done

echo "Done."
