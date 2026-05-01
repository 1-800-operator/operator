"""Arrow-key picker primitives for the `operator build` wizard.

Two flavors share one rendering core:

- :func:`select_one` — single-select. ↑/↓ navigate, enter pick. An optional
  preview pane on the right updates as the cursor moves (used by step 1's
  fighter gallery to show portraits).
- :func:`select_many` — multi-select. ↑/↓ navigate, space toggle, enter
  confirm. Used by step 2 (MCPs) and step 3 (skills).

Both use :class:`rich.live.Live` to redraw one region in place and
``readchar`` for cross-platform key reads. Tests inject keys via the
``key_source`` parameter so the picker can run without a TTY.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Iterator

import readchar
from rich.console import Console, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# Right-pane renderer signature: receives (cursor, checked) and returns a
# Rich renderable. Used when the wizard wants the right pane to reflect
# global state (e.g., the build card in steps 2/3) instead of a per-choice
# preview.
RightPaneFn = Callable[[int, "list[bool] | None"], RenderableType]


@dataclass
class Choice:
    """One row in a picker.

    label       — main text shown on the left
    sublabel    — dim text shown next to label (e.g. tagline)
    value       — opaque payload returned to the caller
    preview     — rendered in the right pane (single-select only).
                  Plain string or any Rich RenderableType (Group/Align/etc.).
    locked      — multi-select only. When True the row renders dim with a
                  lock glyph, space is a no-op, and the row is forced checked
                  at init. Used for MCPs required by chosen skills.
    locked_note — dim text appended after the label on locked rows
                  (e.g. "required by: pr-review, release-notes").
    """

    label: str
    sublabel: str = ""
    value: Any = None
    preview: "str | RenderableType | None" = None
    locked: bool = False
    locked_note: str = ""


class PickerCancelled(Exception):
    """User pressed q / esc / ctrl+c while the picker was open."""


# ── Render core ───────────────────────────────────────────────────────────


def _viewport(cursor: int, total: int, max_visible: int) -> tuple[int, int]:
    """Return (start, end) half-open slice of choice indices to render so
    that ``cursor`` stays inside the window. When ``total`` fits, returns
    ``(0, total)`` — no clipping.

    Used by the skills picker in particular: a user with many skills in
    ~/.claude/skills/ can push the row count past the terminal height, and
    without a viewport the bottom rows just get cropped by rich.Live.
    """
    if total <= max_visible or max_visible <= 0:
        return 0, total
    start = max(0, cursor - max_visible // 2)
    start = min(start, total - max_visible)
    return start, start + max_visible


def _render_rows(
    title: str,
    choices: list[Choice],
    cursor: int,
    *,
    checked: list[bool] | None,
    hint: str,
    max_visible: int | None = None,
) -> Text:
    """Build the choice list as a single Rich Text block.

    When ``max_visible`` is set and the choice count exceeds it, render only
    a window around the cursor with ↑/↓ "more" markers above/below.
    """
    body = Text()
    if title:
        body.append(f"{title}\n\n", style="bold")
    sub_indent = "      " if checked is not None else "  "
    total = len(choices)
    if max_visible is not None:
        start, end = _viewport(cursor, total, max_visible)
    else:
        start, end = 0, total
    if start > 0:
        body.append(f"  ↑ {start} more above\n", style="dim")
    for i in range(start, end):
        ch = choices[i]
        is_cursor = i == cursor
        is_locked = bool(ch.locked) and checked is not None
        cursor_glyph = "▶ " if is_cursor else "  "
        check_glyph = ""
        if checked is not None:
            # Locked rows always show ✓ — they're forced-on and can't be toggled.
            check_glyph = "[✓] " if (checked[i] or is_locked) else "[ ] "
        line = Text()
        line.append(cursor_glyph, style="bold" if is_cursor else "")
        line.append(check_glyph, style="dim" if is_locked else "")
        label_style = "bold" if is_cursor else ""
        if is_locked:
            # Dim the whole row so it reads as "present but you can't change it".
            label_style = "dim"
        line.append(ch.label, style=label_style)
        if is_locked and ch.locked_note:
            line.append(f"  ({ch.locked_note})", style="dim")
        body.append(line)
        body.append("\n")
        if ch.sublabel:
            sub = Text()
            sub.append(sub_indent)
            sub.append(ch.sublabel, style="dim")
            body.append(sub)
            body.append("\n")
    if end < total:
        body.append(f"  ↓ {total - end} more below\n", style="dim")
    if hint:
        body.append(f"\n{hint}", style="dim")
    return body


def _layout(
    title: str,
    choices: list[Choice],
    cursor: int,
    *,
    checked: list[bool] | None,
    hint: str,
    right_pane: RightPaneFn | None,
    max_visible: int | None = None,
    right_pane_width: int = 40,
) -> RenderableType:
    rows = _render_rows(
        title, choices, cursor,
        checked=checked, hint=hint, max_visible=max_visible,
    )
    right: RenderableType | None = None
    if right_pane is not None:
        right = right_pane(cursor, checked)
    elif choices[cursor].preview is not None:
        preview = choices[cursor].preview
        inner: RenderableType = Text(preview) if isinstance(preview, str) else preview
        # Absolute width (expand=True + width=N) locks the panel to exactly
        # N cells no matter how short the "custom" preview is or how long a
        # preset's tagline runs. Caller passes a terminal-aware value so
        # narrow terminals shrink the pane instead of clipping its border.
        right = Panel(
            inner, border_style="dim", padding=(0, 2),
            width=right_pane_width, expand=True,
        )
    if right is None:
        return rows
    table = Table.grid(padding=(0, 4))
    table.add_column()
    table.add_column()
    table.add_row(rows, right)
    return table


# ── Key handling ──────────────────────────────────────────────────────────


def _default_keys() -> Iterator[str]:
    """Yield keys forever from readchar — used at runtime."""
    while True:
        yield readchar.readkey()


def _is_enter(key: str) -> bool:
    return key in ("\n", "\r", readchar.key.ENTER)


def _is_cancel(key: str) -> bool:
    return key in ("q", "Q", readchar.key.ESC, readchar.key.CTRL_C)


# ── Public API ────────────────────────────────────────────────────────────


def select_one(
    title: str,
    choices: list[Choice],
    *,
    right_pane: RightPaneFn | None = None,
    initial: int = 0,
    console: Console | None = None,
    key_source: Iterable[str] | None = None,
) -> Choice:
    """Single-select picker. Returns the picked Choice.

    Right pane behavior: ``right_pane`` callable wins; else falls back to
    each choice's ``preview`` field; else no right pane is drawn.

    Raises PickerCancelled on q/esc/ctrl+c.
    """
    if not choices:
        raise ValueError("select_one needs at least one choice")
    cursor = max(0, min(initial, len(choices) - 1))
    hint = "↑/↓ navigate · enter select · q to cancel"
    console = console or Console()
    keys: Iterator[str] = iter(key_source) if key_source is not None else _default_keys()

    # Shrink the preview panel on narrow terminals so it doesn't clip. Math
    # mirrors build_card.width_for(): reserve 26 cells for the left column
    # plus 8 for Table.grid padding, floor at 28 so the portrait still fits.
    right_pane_width = max(28, min(40, console.size.width - 26 - 8))

    def render() -> RenderableType:
        return _layout(
            title, choices, cursor,
            checked=None, hint=hint, right_pane=right_pane,
            right_pane_width=right_pane_width,
        )

    with Live(render(), console=console, refresh_per_second=30, transient=False) as live:
        for key in keys:
            if key == readchar.key.UP:
                cursor = (cursor - 1) % len(choices)
            elif key == readchar.key.DOWN:
                cursor = (cursor + 1) % len(choices)
            elif _is_enter(key):
                live.update(render())
                return choices[cursor]
            elif _is_cancel(key):
                raise PickerCancelled()
            live.update(render())
    raise PickerCancelled()  # key source exhausted without enter


def select_many(
    title: str,
    choices: list[Choice],
    *,
    initial_checked: list[bool] | None = None,
    right_pane: RightPaneFn | None = None,
    console: Console | None = None,
    key_source: Iterable[str] | None = None,
) -> list[bool]:
    """Multi-select picker. Returns aligned list[bool].

    Right pane (if provided) re-renders on every keypress so the wizard's
    build card can reflect live toggle state.

    Raises PickerCancelled on q/esc/ctrl+c. Empty choices returns [].
    """
    if not choices:
        return []
    if initial_checked is None:
        checked = [False] * len(choices)
    else:
        checked = list(initial_checked)
    # Locked choices are forced on regardless of initial_checked — the caller
    # has already decided the row is non-negotiable (e.g. an MCP required by a
    # chosen skill). Defensive: downstream code depends on locked ⇒ checked.
    for i, ch in enumerate(choices):
        if ch.locked:
            checked[i] = True
    cursor = 0
    hint = "↑/↓ navigate · space toggle · enter confirm · q to cancel"
    console = console or Console()
    keys: Iterator[str] = iter(key_source) if key_source is not None else _default_keys()

    # Size the viewport to the terminal: reserve rows for title/hint/markers/
    # wizard header, then assume the worst case of 2 rows per choice (label +
    # sublabel). Floor at 3 so a very short window still shows something.
    rows_per_choice = 2 if any(c.sublabel for c in choices) else 1
    overhead = 10
    max_visible = max(3, (console.size.height - overhead) // rows_per_choice)

    def render() -> RenderableType:
        return _layout(
            title, choices, cursor,
            checked=checked, hint=hint, right_pane=right_pane,
            max_visible=max_visible,
        )

    with Live(render(), console=console, refresh_per_second=30, transient=False) as live:
        for key in keys:
            if key == readchar.key.UP:
                cursor = (cursor - 1) % len(choices)
            elif key == readchar.key.DOWN:
                cursor = (cursor + 1) % len(choices)
            elif key in (" ", readchar.key.SPACE):
                # No-op on locked rows; the UX caption explains why.
                if not choices[cursor].locked:
                    checked[cursor] = not checked[cursor]
            elif _is_enter(key):
                live.update(render())
                return checked
            elif _is_cancel(key):
                raise PickerCancelled()
            live.update(render())
    raise PickerCancelled()
