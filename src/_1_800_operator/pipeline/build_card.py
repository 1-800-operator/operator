"""The 'Your build' card — persistent right-pane preview during the
``operator build`` wizard's picker steps and the final reveal artifact.

All wrapping happens inside ``_compose_body`` so every emitted row is
already ≤ ``_INNER`` cells with the ``"  "`` left indent preserved
(hanging indent on continuations). ``render`` then wraps the body in a
plain white ``Panel`` and colorizes the ⚡ power-up and ★ skill glyphs.

Tagline is wrapped into the right-hand meta column *before* being
zipped with the portrait, so a long tagline extends meta_lines
downward without breaking the face grid.
"""
from __future__ import annotations

import re

from rich.cells import cell_len
from rich.console import Console, RenderableType
from rich.panel import Panel
from rich.text import Text


PLACEHOLDER_PORTRAIT = (
    "▄▄▄▄▄▄\n"
    "█ ?? █\n"
    "█ ?? █\n"
    "▀▀▀▀▀▀"
)

# Default card width when the caller doesn't pass one. Callers that share
# the terminal with a picker's left column should use ``width_for()`` so the
# card shrinks on narrow terminals instead of getting clipped by Rich.
DEFAULT_WIDTH = 40
MIN_WIDTH = 28

# Cap how many power-ups / skills we enumerate inline before collapsing the
# remainder into a "+N more" dim line. Keeps the card from stretching down
# the screen when the Claude agent pulls in every skill in ~/.claude/skills/
# while still signalling that the extras were loaded — not silently dropped.
MAX_LIST_ITEMS = 5


def width_for(console: Console, *, left_min: int = 26, padding: int = 8) -> int:
    """Return a card width that fits alongside a picker's left column.

    The card sits in the right column of a ``Table.grid`` with ``padding``
    cells of horizontal gutter and a left column that needs at least
    ``left_min`` cells for the choice label + checkbox. Anything the
    terminal can't spare comes out of the card width, floored at
    ``MIN_WIDTH`` so the portrait + a short meta column still fit.
    """
    available = console.size.width - left_min - padding
    return max(MIN_WIDTH, min(DEFAULT_WIDTH, available))


# Overhead per item line, in cells: ``"  "`` body indent + the 12-cell label
# column ("MCPs:       ") + ``"⚡ "`` glyph segment (the lightning-bolt is
# wide → 2 cells, plus a 1-cell separator) + the panel's two border cells.
# Tuned to match the constants used by ``_compose_body`` / ``render``; if
# either the label width or the glyph changes, recompute this.
_ITEM_LINE_OVERHEAD = 2 + 12 + 3 + 2 + 1


def width_for_reveal(
    console: Console,
    *,
    items: list[str],
    margin: int = 4,
) -> int:
    """Card width tuned for the final reveal — extends past DEFAULT_WIDTH
    so long power-up / skill names render on one line. Capped by the
    terminal width (minus ``margin``) so the right edge never clips.
    """
    longest = max((cell_len(it) for it in items), default=0)
    natural = longest + _ITEM_LINE_OVERHEAD
    cap = max(MIN_WIDTH, console.size.width - margin)
    return max(MIN_WIDTH, min(cap, max(DEFAULT_WIDTH, natural)))


def _wrap_cells(text: str, width: int) -> list[str]:
    """Wrap ``text`` on spaces so each line fits ``width`` cells.

    A single token wider than ``width`` is hard-split on code-point
    boundaries — acceptable for the build card since the only wide
    glyphs in play (⚡ ★) always sit next to short ASCII labels.
    """
    if width <= 0:
        return [text]
    if cell_len(text) <= width:
        return [text]
    out: list[str] = []
    cur = ""
    for word in text.split(" "):
        candidate = f"{cur} {word}" if cur else word
        if cell_len(candidate) <= width:
            cur = candidate
            continue
        if cur:
            out.append(cur)
            cur = ""
        while cell_len(word) > width:
            out.append(word[:width])
            word = word[width:]
        cur = word
    if cur:
        out.append(cur)
    return out


def _hang_wrap(prefix: str, body: str, inner: str = "", *, width: int) -> list[str]:
    """Wrap ``body`` under a label ``prefix``. First line renders
    ``prefix + body-segment``; continuation lines are padded so body
    text aligns under itself (hanging indent at ``len(prefix)``).

    ``inner`` is an optional left gutter applied to every emitted row
    (the card's ``"  "`` indent).
    """
    avail = width - cell_len(inner) - cell_len(prefix)
    segs = _wrap_cells(body, avail)
    if not segs:
        return [inner + prefix]
    hang = " " * cell_len(prefix)
    rows = [inner + prefix + segs[0]]
    for seg in segs[1:]:
        rows.append(inner + hang + seg)
    return rows


def _render_list(
    label: str,
    glyph: str,
    items: list[str],
    *,
    inner: int,
    indent: str,
) -> list[str]:
    """Render one labelled list section of the card body.

    First item sits next to ``label``; continuation items align under it with
    a hanging indent. When the list exceeds ``MAX_LIST_ITEMS``, the first
    ``MAX_LIST_ITEMS`` render inline and the rest collapse into a single
    "+N more" line at the same hanging-indent column.
    """
    hang = " " * len(label)
    if not items:
        return _hang_wrap(label, "—", width=inner, inner=indent)

    visible = items[:MAX_LIST_ITEMS]
    hidden = len(items) - len(visible)

    rows: list[str] = []
    for i, name in enumerate(visible):
        prefix = label if i == 0 else hang
        rows.extend(_hang_wrap(prefix, f"{glyph} {name}", width=inner, inner=indent))
    if hidden > 0:
        rows.extend(_hang_wrap(hang, f"+{hidden} more", width=inner, inner=indent))
    return rows


def _compose_body(
    name: str,
    tagline: str,
    portrait: str,
    power_ups: list[str],
    skills: list[str],
    *,
    inner: int,
) -> list[str]:
    """Plain-text body rows — no ANSI, no Rich markup. Every row fits
    within ``inner`` cells and carries the ``"  "`` left indent."""
    indent = "  "
    portrait_lines = portrait.split("\n")
    portrait_w = max((cell_len(p) for p in portrait_lines), default=0)

    # Wrap tagline into the meta column BEFORE zipping with the portrait,
    # so a long tagline just adds rows under the name — it can't wrap
    # across the face.
    meta_col_w = inner - cell_len(indent) - portrait_w - 3
    meta_lines: list[str] = [name or "(unnamed)"]
    meta_lines += _wrap_cells(tagline or "(no tagline yet)", meta_col_w)

    rows: list[str] = []
    for i in range(max(len(portrait_lines), len(meta_lines))):
        p = portrait_lines[i] if i < len(portrait_lines) else ""
        m = meta_lines[i] if i < len(meta_lines) else ""
        left = indent + p + " " * (portrait_w - cell_len(p))
        row = left + "   " + m
        rows.append(row + " " * max(0, inner - cell_len(row)))

    rows.append(" " * inner)

    # Labels padded to the same width (12 cells) so ⚡ and ★ align vertically.
    rows.extend(_render_list("MCPs:       ", "⚡", power_ups, inner=inner, indent=indent))
    rows.extend(_render_list("skills:     ", "★", skills, inner=inner, indent=indent))

    # Normalize every row to exactly ``inner`` cells so the Panel renders
    # with clean right-edge padding and no Rich reflow.
    return [r + " " * max(0, inner - cell_len(r)) for r in rows]


def render(
    *,
    name: str,
    tagline: str,
    portrait: str,
    power_ups: list[str],
    skills: list[str],
    title: str = "Your build",
    width: int = DEFAULT_WIDTH,
) -> RenderableType:
    """Build the card as a plain white ``Panel`` with colorized icons.

    ``width`` is the total panel width including borders. Pass
    ``width_for(console)`` to shrink the card on narrow terminals so the
    right edge doesn't clip.
    """
    width = max(MIN_WIDTH, width)
    inner_w = width - 2
    body = _compose_body(name, tagline, portrait, power_ups, skills, inner=inner_w)
    blank = " " * inner_w
    framed_body = [blank] + body + [blank]
    # Pre-padded rows fit ``inner_w`` exactly — turn off Rich's reflow so
    # it doesn't strip leading indent on any row. Markup injection is safe
    # because ⚡/★ are only emitted by _compose_body itself.
    raw = "\n".join(framed_body)
    markup = raw.replace("⚡", "[bold magenta]⚡[/bold magenta]").replace("★", "[bold yellow]★[/bold yellow]")
    # Dim the "+N more" overflow marker so it reads as a count, not an item.
    markup = re.sub(r"(\+\d+ more)", r"[dim]\1[/dim]", markup)
    inner = Text.from_markup(markup, overflow="crop")
    inner.no_wrap = True
    return Panel(
        inner,
        title=title,
        border_style="white",
        width=width,
        padding=(0, 0),
        expand=True,
    )
