"""
Guard rails for MCP tool execution.

Pre-execution: block known-dangerous tool calls (e.g. binary file reads).
Post-execution: validate tool results before they enter LLM history.

These protect against session-bricking failures discovered in pressure
testing (G7: binary file poisons context, G6: large file blows context).
Works for any MCP server, not just the ones we ship with.
"""
import json
import logging
import os

log = logging.getLogger(__name__)

# ── Text file extension allowlist ─────────────────────────────────────
# Inclusive approach: only extensions on this list are allowed through
# the pre-execution file path check. Extensionless files are always
# allowed (README, LICENSE, Makefile, etc.). This is more durable than
# a binary blocklist because text formats are finite.

TEXT_EXTENSIONS = frozenset({
    # Plain text / docs
    ".txt", ".md", ".rst", ".rtf", ".tex", ".bib", ".sty", ".adoc",
    # Data / config
    ".json", ".yaml", ".yml", ".toml", ".xml", ".csv", ".tsv",
    ".ini", ".cfg", ".conf", ".properties", ".env",
    # Web
    ".html", ".htm", ".css", ".js", ".ts", ".jsx", ".tsx",
    ".vue", ".svelte", ".astro", ".mdx",
    # Python
    ".py", ".pyi", ".pyx", ".pxd",
    # Ruby
    ".rb", ".erb", ".rake", ".gemspec",
    # Go
    ".go", ".mod", ".sum",
    # Rust
    ".rs",
    # Java / JVM
    ".java", ".kt", ".kts", ".scala", ".groovy", ".gradle",
    # C / C++
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",
    # C# / .NET
    ".cs", ".fs", ".fsx", ".csproj", ".fsproj", ".sln",
    # Swift / Obj-C
    ".swift", ".m", ".mm",
    # Shell
    ".sh", ".bash", ".zsh", ".fish", ".ps1", ".bat", ".cmd",
    # Query / schema
    ".sql", ".graphql", ".gql", ".proto", ".thrift",
    # Lisp / functional
    ".el", ".clj", ".cljs", ".cljc", ".edn", ".hs", ".ml", ".mli",
    # Scripting
    ".lua", ".pl", ".pm", ".php", ".r", ".R", ".jl",
    # Elixir / Erlang
    ".ex", ".exs", ".erl", ".hrl",
    # Infrastructure
    ".tf", ".hcl", ".nix", ".dhall",
    # Misc code
    ".wasm", ".wat",
    # Diff / patch
    ".diff", ".patch",
    # Log
    ".log",
    # Lock files (text-based)
    ".lock",
    # SVG (XML-based text)
    ".svg",
    # Dotfiles / config
    ".gitignore", ".gitattributes", ".gitmodules",
    ".dockerignore", ".editorconfig",
    ".eslintrc", ".prettierrc", ".babelrc",
    ".flake8", ".pylintrc",
    ".npmrc", ".nvmrc", ".yarnrc",
})

# Base64 prefixes for common binary image formats
_BASE64_IMAGE_PREFIXES = (
    "iVBOR",    # PNG
    "/9j/",     # JPEG
    "R0lGOD",   # GIF
    "UklGR",    # WEBP (RIFF container)
    "data:image/",
)


def is_text_file_path(path: str) -> bool:
    """Check if a file path looks like a text file.

    Returns True for text extensions and extensionless files.
    Returns False for anything else (binary/unknown extensions).
    """
    _, ext = os.path.splitext(path)
    if not ext:
        return True  # extensionless files are allowed (README, Makefile, etc.)
    return ext.lower() in TEXT_EXTENSIONS


def validate_tool_result(content: str) -> tuple[bool, str]:
    """Validate tool result content before it enters LLM history.

    Returns (is_safe, reason). If is_safe is False, the content should
    be replaced with a safe error message before entering history.
    """
    if not content:
        return True, ""

    # Check for null bytes (binary data)
    if "\x00" in content:
        return False, "content contains null bytes (binary data)"

    # Check for base64-encoded image data in the first 1000 chars
    head = content[:1000]
    for prefix in _BASE64_IMAGE_PREFIXES:
        if prefix in head:
            return False, f"content contains base64 image data ({prefix}...)"

    # Check for high ratio of non-printable characters
    sample = content[:4096]
    non_printable = sum(
        1 for c in sample
        if not c.isprintable() and c not in "\n\r\t"
    )
    if len(sample) > 0 and non_printable / len(sample) > 0.10:
        return False, f"content is {non_printable / len(sample):.0%} non-printable characters (likely binary)"

    return True, ""


def log_rejection(tool_name: str, arguments: dict, reason: str, stage: str):
    """Log a guard rail rejection with full diagnostic detail.

    Logs enough context for a user debugging their own MCP server to
    understand what happened and how to fix it (e.g. by adding hints).
    """
    args_str = json.dumps(arguments, default=str)[:500]
    log.warning(
        f"GUARDRAIL [{stage}] blocked tool={tool_name} | "
        f"reason={reason} | args={args_str}"
    )
