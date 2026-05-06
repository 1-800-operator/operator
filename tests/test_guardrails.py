"""
Unit tests for step 10.4 — BYOMCP guard rails.

Tests the text extension allowlist, binary content detection, pre-execution
blocking in MCPClient, and post-execution validation in LLMClient.

Run:
    source venv/bin/activate
    python tests/test_guardrails.py
"""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))
os.environ.setdefault("OPERATOR_BOT", "pm")

from unittest.mock import MagicMock
from _1_800_operator.pipeline.guardrails import is_text_file_path, validate_tool_result


# ---------------------------------------------------------------------------
# is_text_file_path — text extensions allowed
# ---------------------------------------------------------------------------

def test_text_extensions_allowed():
    for ext in [".py", ".md", ".json", ".yaml", ".js", ".ts", ".go", ".rs",
                ".java", ".html", ".css", ".sh", ".sql", ".toml", ".xml",
                ".csv", ".log", ".svg", ".diff", ".lock"]:
        path = f"src/file{ext}"
        assert is_text_file_path(path), f"Expected allowed: {path}"
    print("PASS  test_text_extensions_allowed")


# ---------------------------------------------------------------------------
# is_text_file_path — binary extensions blocked
# ---------------------------------------------------------------------------

def test_binary_extensions_blocked():
    for ext in [".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".bmp",
                ".tiff", ".mp4", ".mov", ".avi", ".mp3", ".wav", ".flac",
                ".zip", ".tar", ".gz", ".rar", ".7z",
                ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
                ".bin", ".exe", ".dll", ".so", ".dylib", ".o", ".a",
                ".onnx", ".pyc", ".class", ".woff", ".woff2", ".ttf", ".eot"]:
        path = f"assets/file{ext}"
        assert not is_text_file_path(path), f"Expected blocked: {path}"
    print("PASS  test_binary_extensions_blocked")


# ---------------------------------------------------------------------------
# is_text_file_path — extensionless files allowed
# ---------------------------------------------------------------------------

def test_extensionless_allowed():
    for name in ["README", "LICENSE", "Makefile", "Dockerfile", "Procfile",
                 "Gemfile", "somefile", "CODEOWNERS", ".gitignore"]:
        # .gitignore has an extension in TEXT_EXTENSIONS, but the rest are extensionless
        assert is_text_file_path(name), f"Expected allowed: {name}"
    # Also with directory prefix
    assert is_text_file_path("src/README"), "Expected allowed: src/README"
    print("PASS  test_extensionless_allowed")


# ---------------------------------------------------------------------------
# is_text_file_path — case insensitive
# ---------------------------------------------------------------------------

def test_case_insensitive():
    for path in ["file.PY", "file.Md", "file.JSON", "file.YAML", "FILE.JS"]:
        assert is_text_file_path(path), f"Expected allowed (case insensitive): {path}"
    for path in ["file.PNG", "file.Jpg", "file.PDF"]:
        assert not is_text_file_path(path), f"Expected blocked (case insensitive): {path}"
    print("PASS  test_case_insensitive")


# ---------------------------------------------------------------------------
# is_text_file_path — paths with directories
# ---------------------------------------------------------------------------

def test_paths_with_directories():
    assert is_text_file_path("src/main.py")
    assert is_text_file_path("docs/guide.md")
    assert not is_text_file_path("images/logo.png")
    assert not is_text_file_path("build/output.bin")
    assert is_text_file_path("config/settings.yaml")
    print("PASS  test_paths_with_directories")


# ---------------------------------------------------------------------------
# validate_tool_result — clean text passes
# ---------------------------------------------------------------------------

def test_validate_clean_text():
    ok, reason = validate_tool_result("def hello():\n    print('hi')\n")
    assert ok, f"Clean text should pass, got reason: {reason}"
    ok, reason = validate_tool_result("")
    assert ok, "Empty string should pass"
    print("PASS  test_validate_clean_text")


# ---------------------------------------------------------------------------
# validate_tool_result — null bytes flagged
# ---------------------------------------------------------------------------

def test_validate_null_bytes():
    ok, reason = validate_tool_result("hello\x00world")
    assert not ok, "Null bytes should be flagged"
    assert "null bytes" in reason
    print("PASS  test_validate_null_bytes")


# ---------------------------------------------------------------------------
# validate_tool_result — base64 image prefixes flagged
# ---------------------------------------------------------------------------

def test_validate_base64_png():
    ok, reason = validate_tool_result("iVBORw0KGgoAAAANSUhEUgAAA...")
    assert not ok, "Base64 PNG should be flagged"
    assert "iVBOR" in reason
    print("PASS  test_validate_base64_png")


def test_validate_base64_jpeg():
    ok, reason = validate_tool_result("/9j/4AAQSkZJRgABAQ...")
    assert not ok, "Base64 JPEG should be flagged"
    assert "/9j/" in reason
    print("PASS  test_validate_base64_jpeg")


def test_validate_data_uri():
    ok, reason = validate_tool_result("data:image/png;base64,iVBORw0KGgo...")
    assert not ok, "data:image URI should be flagged"
    print("PASS  test_validate_data_uri")


# ---------------------------------------------------------------------------
# validate_tool_result — high non-printable ratio flagged
# ---------------------------------------------------------------------------

def test_validate_high_nonprintable():
    # 50% non-printable
    content = "a\x01" * 500
    ok, reason = validate_tool_result(content)
    assert not ok, "High non-printable ratio should be flagged"
    assert "non-printable" in reason
    print("PASS  test_validate_high_nonprintable")


# ---------------------------------------------------------------------------
# validate_tool_result — low non-printable passes
# ---------------------------------------------------------------------------

def test_validate_low_nonprintable():
    # Normal text with tabs and newlines — well under 10%
    content = "hello\tworld\n" * 100
    ok, reason = validate_tool_result(content)
    assert ok, f"Low non-printable should pass, got reason: {reason}"
    print("PASS  test_validate_low_nonprintable")


# ---------------------------------------------------------------------------
# Integration: LLM.send_tool_result replaces binary content in history
# ---------------------------------------------------------------------------

def test_llm_blocks_binary_result():
    from _1_800_operator.pipeline.llm import LLMClient

    provider = MagicMock()
    llm = LLMClient(provider)

    # Seed an in-flight tool call in the scratchpad
    from _1_800_operator.pipeline.providers import ProviderResponse, ToolCall
    llm._scratch = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [ToolCall(id="call_abc", name="some__tool", args={})],
    }]

    # Mock the provider to return a text summary message
    provider.complete.return_value = ProviderResponse(
        text="I couldn't read that.", tool_calls=[], stop_reason="end",
    )

    # Send a result with null bytes (simulating binary content)
    llm.send_tool_result("call_abc", "some__tool", "binary\x00content\x00here")

    # Verify the blocked content was sent to the provider, not the binary.
    # (_collapse_tool_exchange removes the tool_result message from history
    # after the summary, so we check what was sent to the provider instead.)
    call_args = provider.complete.call_args
    messages = call_args.kwargs["messages"]
    tool_msg = next(m for m in messages if m.get("role") == "tool_result")
    assert "blocked" in tool_msg["content"], f"Expected block message, got: {tool_msg['content'][:100]}"
    assert "\x00" not in tool_msg["content"], "Binary content leaked into API call"
    print("PASS  test_llm_blocks_binary_result")


# ---------------------------------------------------------------------------
# Integration: LLM.send_tool_result passes clean content
# ---------------------------------------------------------------------------

def test_llm_passes_clean_result():
    from _1_800_operator.pipeline.llm import LLMClient

    provider = MagicMock()
    llm = LLMClient(provider)

    from _1_800_operator.pipeline.providers import ProviderResponse, ToolCall
    llm._scratch = [{
        "role": "assistant",
        "content": None,
        "tool_calls": [ToolCall(id="call_xyz", name="some__tool", args={})],
    }]

    provider.complete.return_value = ProviderResponse(
        text="Here's the file.", tool_calls=[], stop_reason="end",
    )

    clean_content = "def hello():\n    return 'world'\n"
    llm.send_tool_result("call_xyz", "some__tool", clean_content)

    # Check what was sent to the provider (collapse removes tool_result from history)
    call_args = provider.complete.call_args
    messages = call_args.kwargs["messages"]
    tool_msg = next(m for m in messages if m.get("role") == "tool_result")
    assert clean_content in tool_msg["content"], f"Clean content should pass through unchanged inside the wrapper"
    assert tool_msg["content"].startswith('<tool_result tool="some__tool">'), \
        f"Tool result should be wrapped: got {tool_msg['content'][:80]}"
    print("PASS  test_llm_passes_clean_result")


# ---------------------------------------------------------------------------
# Gap-fill: base64 prefix coverage
# ---------------------------------------------------------------------------

def test_validate_base64_gif_and_webp():
    """GIF (`R0lGOD`) and WebP (`UklGR`) prefixes must also be flagged."""
    ok, reason = validate_tool_result("R0lGODlhAQABAAAAACH5BAEKAAEALAAA...")
    assert not ok, "GIF base64 prefix should be flagged"
    assert "R0lGOD" in reason
    ok, reason = validate_tool_result("UklGRnoGAABXRUJQVlA4IG4GAADwIACdASo...")
    assert not ok, "WebP (RIFF) base64 prefix should be flagged"
    assert "UklGR" in reason
    print("PASS  test_validate_base64_gif_and_webp")


def test_validate_base64_prefix_beyond_window_passes():
    """Base64 image prefix appearing past the first 1000 chars is NOT flagged.

    The scanner only inspects content[:1000] for image signatures, so text
    that legitimately mentions e.g. "iVBOR" deep in a file body shouldn't
    blow up. Pad well past 1000 and verify the result passes.
    """
    content = ("abcde " * 200) + "iVBORw0KGgo"  # 1200+ chars, prefix at the tail
    assert len(content) > 1000
    ok, reason = validate_tool_result(content)
    assert ok, f"prefix beyond 1000-char window should pass, got: {reason}"
    print("PASS  test_validate_base64_prefix_beyond_window_passes")


# ---------------------------------------------------------------------------
# Gap-fill: non-printable ratio boundary + whitespace carve-outs
# ---------------------------------------------------------------------------

def test_validate_nonprintable_at_threshold_boundary():
    """The check is strict `>` 0.10 — exactly 10% should still pass.

    Build a 100-byte sample with exactly 10 non-printable bytes. 10 / 100 = 0.10,
    which fails the `> 0.10` guard, so the content is accepted.
    """
    content = ("\x01" * 10) + ("a" * 90)
    assert len(content) == 100
    ok, reason = validate_tool_result(content)
    assert ok, f"exactly 10% non-printable should pass (strict >), got: {reason}"

    # Just above the boundary: 11/100 fails.
    over = ("\x01" * 11) + ("a" * 89)
    ok2, _ = validate_tool_result(over)
    assert not ok2, "11% non-printable should be flagged"
    print("PASS  test_validate_nonprintable_at_threshold_boundary")


def test_validate_tab_newline_cr_not_counted_as_nonprintable():
    """\\n, \\r, \\t are whitelisted and must not count toward the ratio."""
    # 100% whitespace control chars — would be flagged if counted as non-printable.
    content = "\n\r\t" * 500
    ok, reason = validate_tool_result(content)
    assert ok, f"tabs/newlines/CR must be allowed, got: {reason}"
    print("PASS  test_validate_tab_newline_cr_not_counted_as_nonprintable")


# ---------------------------------------------------------------------------
# Gap-fill: log_rejection formatting
# ---------------------------------------------------------------------------

def test_log_rejection_emits_warning_with_context():
    """log_rejection must emit a WARNING carrying stage, tool, reason, and args."""
    import logging
    from _1_800_operator.pipeline.guardrails import log_rejection

    # Capture records from the guardrails logger at WARNING level.
    captured = []

    class _Handler(logging.Handler):
        def emit(self, record):
            captured.append(record)

    guard_log = logging.getLogger("_1_800_operator.pipeline.guardrails")
    h = _Handler(level=logging.WARNING)
    saved_level = guard_log.level
    guard_log.setLevel(logging.WARNING)
    guard_log.addHandler(h)
    try:
        # args include a non-JSON-serializable object to exercise `default=str`.
        log_rejection(
            tool_name="github__get_file_contents",
            arguments={"path": "images/logo.png", "weird": object()},
            reason="non-text file",
            stage="pre",
        )
    finally:
        guard_log.removeHandler(h)
        guard_log.setLevel(saved_level)

    assert len(captured) == 1, f"expected 1 warning, got {len(captured)}"
    rec = captured[0]
    assert rec.levelno == logging.WARNING
    msg = rec.getMessage()
    assert "GUARDRAIL [pre]" in msg
    assert "tool=github__get_file_contents" in msg
    assert "reason=non-text file" in msg
    assert "images/logo.png" in msg   # args must be rendered
    print("PASS  test_log_rejection_emits_warning_with_context")


# ---------------------------------------------------------------------------
# Run all
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    tests = [
        test_text_extensions_allowed,
        test_binary_extensions_blocked,
        test_extensionless_allowed,
        test_case_insensitive,
        test_paths_with_directories,
        test_validate_clean_text,
        test_validate_null_bytes,
        test_validate_base64_png,
        test_validate_base64_jpeg,
        test_validate_data_uri,
        test_validate_high_nonprintable,
        test_validate_low_nonprintable,
        test_llm_blocks_binary_result,
        test_llm_passes_clean_result,
        test_validate_base64_gif_and_webp,
        test_validate_base64_prefix_beyond_window_passes,
        test_validate_nonprintable_at_threshold_boundary,
        test_validate_tab_newline_cr_not_counted_as_nonprintable,
        test_log_rejection_emits_warning_with_context,
    ]
    failures = []
    for t in tests:
        try:
            t()
        except Exception as e:
            print(f"FAIL  {t.__name__}: {e}")
            failures.append(t.__name__)

    print(f"\n{len(tests) - len(failures)}/{len(tests)} passed")
    sys.exit(1 if failures else 0)
