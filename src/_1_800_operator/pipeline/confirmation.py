"""Shared yes/no detection for tool-confirmation prompts.

Both confirmation surfaces — `chat_runner._handle_confirmation` (track B,
openai/anthropic providers) and `permission_chat_handler._is_yes` (track A,
claude_cli's PreToolUse hook) — read the user's free-form reply to a "Run X?
OK?" prompt and decide whether it counts as approval. Pre-session-178 they
each had their own matcher with subtly different vocab (track A accepted
bare `y`; track B did not). Same user-facing contract, divergent behavior.
This module is the single source of truth — both call sites import `is_yes`.

Affirmative tokens accepted (word-boundary): yes, ok, okay, sure, approve,
approved, confirmed, yep, yeah, y. Plus the phrases "go ahead" and "do it".

Negation gate: any of {no, nope, nah, stop, cancel} or {don't, dont, do not}
present in the reply forces a False return even when an affirmative token is
also present. Catches "ok no don't do that", "yes don't", "go ahead no
actually" — replies where the user pairs an affirmative cue with an explicit
veto. Falls through to the LLM correction branch on those.
"""
import re


_AFFIRM_RE = re.compile(
    r"\b(yes|ok|okay|sure|approve|approved|confirmed|yep|yeah|y)\b",
    re.I,
)
_NEGATION_RE = re.compile(
    r"\b(no|nope|nah|stop|cancel)\b",
    re.I,
)


def is_yes(text: str) -> bool:
    """Return True if `text` is an unambiguous affirmation of a tool call.

    Returns False on any of:
      - empty / whitespace input
      - reply contains a negation token (forces correction branch)
      - no affirmative token or phrase present
    """
    if not text or not text.strip():
        return False
    lower = text.lower()
    has_negative = (
        _NEGATION_RE.search(lower) is not None
        or "don't" in lower
        or "dont" in lower
        or "do not" in lower
    )
    if has_negative:
        return False
    if "go ahead" in lower or "do it" in lower:
        return True
    return _AFFIRM_RE.search(lower) is not None
