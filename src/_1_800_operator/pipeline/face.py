"""Deterministic glyph-face generator for agents.

Each bot gets a 4-line face: a box frame with a 2-char eye glyph and 2-char
mouth glyph. Assignment is seeded by sha256(name) so the same bot name
always renders the same face across runs and contributors. Hand-curated
overrides let the MVP bots (engineer / pm / designer) match a fixed look.

    ▄▄▄▄▄▄
    █ ▲▲ █
    █ ══ █
    ▀▀▀▀▀▀

Plain mode swaps the Unicode frame + glyphs for ASCII-safe substitutes so
the banner stays legible in screen readers and hostile terminals.
"""
import hashlib
from pathlib import Path

# Glyph library. Kept to 2-column chars that render as single-width in
# common monospace terminals (Menlo, Monaco, iTerm/Terminal default).
EYES = [
    "▲▲", "⊙⊙", "◠◠", "●●", "◉◉", "◔◔", "◐◐", "⨯⨯",
    "░░", "▓▓", "◈◈", "✦✦", "★★", "◆◆", "○○", "⬥⬥",
    "⬢⬢", "▣▣", "◑◑", "◒◒", "◓◓", "▢▢", "◬◬", "∆∆",
    "◇◇",
]

MOUTHS = [
    "══", "‿‿", "▽▽", "◡◡", "──", "⌢⌢", "⌣⌣",
    "⟂⟂", "≈≈", "∽∽", "∿∿", "▁▁", "▔▔", "▬▬",
    "◠◠", "⎵⎵", "⎴⎴", "▂▂", "▃▃", "═─",
]

# Hand-curated overrides for the three MVP bots. Matches the fighter-select
# reference mock in the session 117 design discussion.
OVERRIDES = {
    "engineer": ("▲▲", "══"),
    "pm":       ("⊙⊙", "‿‿"),
    "designer": ("◠◠", "▽▽"),
}

PLAIN_EYES   = ["oo", "OO", "**", "..", "^^", "xx", "++", "@@"]
PLAIN_MOUTHS = ["--", "__", "==", "vv", "()", "~~"]

PLAIN_OVERRIDES = {
    "engineer": ("^^", "=="),
    "pm":       ("oo", "uu"),
    "designer": ("..", "vv"),
}


def _seeded_index(name: str, modulus: int, salt: str) -> int:
    h = hashlib.sha256(f"{name}:{salt}".encode("utf-8")).digest()
    return int.from_bytes(h[:4], "big") % modulus


def pick(name: str, plain: bool = False) -> tuple[str, str]:
    """Return the (eye, mouth) pair for `name`. Deterministic across runs."""
    if plain:
        if name in PLAIN_OVERRIDES:
            return PLAIN_OVERRIDES[name]
        return (
            PLAIN_EYES[_seeded_index(name, len(PLAIN_EYES), "eye")],
            PLAIN_MOUTHS[_seeded_index(name, len(PLAIN_MOUTHS), "mouth")],
        )
    if name in OVERRIDES:
        return OVERRIDES[name]
    return (
        EYES[_seeded_index(name, len(EYES), "eye")],
        MOUTHS[_seeded_index(name, len(MOUTHS), "mouth")],
    )


def render(name: str, plain: bool = False) -> str:
    """Return the 4-line face as a newline-separated string (no trailing \\n)."""
    eyes, mouth = pick(name, plain=plain)
    if plain:
        return (
            "+----+\n"
            f"| {eyes} |\n"
            f"| {mouth} |\n"
            "+----+"
        )
    return (
        "▄▄▄▄▄▄\n"
        f"█ {eyes} █\n"
        f"█ {mouth} █\n"
        "▀▀▀▀▀▀"
    )


def load_or_render(name: str, portrait_path: Path | None = None,
                   plain: bool = False) -> str:
    """Return the portrait: file contents if present, else freshly rendered."""
    if portrait_path is not None and portrait_path.exists():
        try:
            return portrait_path.read_text(encoding="utf-8").rstrip("\n")
        except OSError:
            pass
    return render(name, plain=plain)


def write_if_missing(name: str, portrait_path: Path,
                     plain: bool = False) -> bool:
    """Write a freshly-rendered portrait to disk if the file doesn't exist.

    Returns True if a file was written, False if one already existed.
    """
    if portrait_path.exists():
        return False
    portrait_path.parent.mkdir(parents=True, exist_ok=True)
    portrait_path.write_text(render(name, plain=plain) + "\n", encoding="utf-8")
    return True
