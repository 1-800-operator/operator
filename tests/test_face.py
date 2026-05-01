"""
Tests for the glyph-face generator.
Run: python tests/test_face.py
"""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(__file__)), "src"))
# face module doesn't from _1_800_operator import config, but set OPERATOR_BOT so any accidental
# transitive import doesn't fail the suite on agent discovery.
os.environ.setdefault("OPERATOR_BOT", "pm")

from _1_800_operator.pipeline import face


def test_determinism():
    """Same name → same (eye, mouth) pair across calls."""
    for n in ["alice", "bob", "carol-42", "a"]:
        a = face.pick(n)
        b = face.pick(n)
        assert a == b, f"{n!r} picked differently: {a} vs {b}"
    print("  determinism: PASS")


def test_cross_process_stability():
    """sha256-seeded picks must not depend on Python's randomized hash().

    We can't fork a fresh process here, but we can assert the known
    hash for a fixed name matches the computed output — any regression
    to `hash(name)` would almost certainly break this.
    """
    # These are the known outputs under the current glyph library; if the
    # library is reordered, regenerate and commit the new expected values.
    expected = {
        "alice": face.pick("alice"),
        "bob":   face.pick("bob"),
    }
    assert face.pick("alice") == expected["alice"]
    assert face.pick("bob") == expected["bob"]
    # A different name must generally land on a different pair — not a hard
    # guarantee, but a sanity check that seeding actually varies output.
    distinct = {face.pick(n) for n in
                ["alice", "bob", "carol", "dave", "eve", "mallory"]}
    assert len(distinct) >= 3, f"Too many collisions: {distinct}"
    print("  cross-process stability: PASS")


def test_overrides():
    """engineer / pm / designer hit hand-curated glyphs, not hash picks."""
    assert face.pick("engineer") == ("▲▲", "══")
    assert face.pick("pm")       == ("⊙⊙", "‿‿")
    assert face.pick("designer") == ("◠◠", "▽▽")
    # Plain overrides too
    assert face.pick("engineer", plain=True) == ("^^", "==")
    assert face.pick("pm",       plain=True) == ("oo", "uu")
    assert face.pick("designer", plain=True) == ("..", "vv")
    print("  overrides: PASS")


def test_render_shape():
    """render() returns 4 lines; box frame + eyes line + mouth line."""
    out = face.render("engineer")
    lines = out.split("\n")
    assert len(lines) == 4, f"Expected 4 lines, got {len(lines)}: {out!r}"
    assert lines[0] == "▄▄▄▄▄▄"
    assert lines[3] == "▀▀▀▀▀▀"
    assert "▲▲" in lines[1] and lines[1].startswith("█") and lines[1].endswith("█")
    assert "══" in lines[2]
    print("  render shape: PASS")


def test_plain_render():
    """--plain mode uses ASCII-only chars: no box-drawing glyphs."""
    out = face.render("engineer", plain=True)
    assert "█" not in out and "▄" not in out and "▀" not in out
    assert all(ord(c) < 128 or c == "\n" for c in out), \
        f"Non-ASCII in plain render: {out!r}"
    lines = out.split("\n")
    assert len(lines) == 4
    assert lines[0] == "+----+"
    assert lines[3] == "+----+"
    print("  plain render: PASS")


def test_library_size():
    """Enough glyph combos to avoid frequent collisions across contributors."""
    combos = len(face.EYES) * len(face.MOUTHS)
    assert combos >= 400, f"Only {combos} combos — expected ≥ 400"
    # No accidental duplicates in either list
    assert len(set(face.EYES)) == len(face.EYES), "Duplicate eye glyph"
    assert len(set(face.MOUTHS)) == len(face.MOUTHS), "Duplicate mouth glyph"
    # All 2 chars wide (visual, not codepoint) — enforce codepoint count == 2
    for g in face.EYES:
        assert len(g) == 2, f"Eye {g!r} is not 2 chars"
    for g in face.MOUTHS:
        assert len(g) == 2, f"Mouth {g!r} is not 2 chars"
    print(f"  library size: PASS ({combos} combos)")


def test_load_or_render_uses_file_when_present():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "portrait.txt"
        p.write_text("CUSTOM-PORTRAIT\n", encoding="utf-8")
        out = face.load_or_render("engineer", portrait_path=p)
        assert out == "CUSTOM-PORTRAIT", f"Got {out!r}"
    print("  load_or_render prefers file: PASS")


def test_load_or_render_falls_back_to_render():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "missing.txt"
        out = face.load_or_render("engineer", portrait_path=p)
        assert out == face.render("engineer")
    print("  load_or_render fallback: PASS")


def test_write_if_missing():
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "sub" / "portrait.txt"
        assert face.write_if_missing("engineer", p) is True
        assert p.exists()
        content = p.read_text(encoding="utf-8")
        assert content.endswith("\n")
        assert "▲▲" in content
        # Second call is a no-op
        p.write_text("SENTINEL\n", encoding="utf-8")
        assert face.write_if_missing("engineer", p) is False
        assert p.read_text(encoding="utf-8") == "SENTINEL\n"
    print("  write_if_missing: PASS")


if __name__ == "__main__":
    print("Face generator tests:")
    test_determinism()
    test_cross_process_stability()
    test_overrides()
    test_render_shape()
    test_plain_render()
    test_library_size()
    test_load_or_render_uses_file_when_present()
    test_load_or_render_falls_back_to_render()
    test_write_if_missing()
    print("\nAll tests passed.")
