"""
Credential Access Detector

Shared pattern matching module used by SDK interceptors to determine whether
a tool call targets credential files. Used by:

- credential_guard_hook.py (Claude Code PreToolUse hook)
- google_adk.py (inline ADK Bash/Read interceptors)

This module has no dependencies beyond the standard library so it can be used
in standalone hook scripts as well as within the environment server process.
"""
import re

# ── Patterns ────────────────────────────────────────────────────────────────

# File paths that are credential files (matched against Read/Write/Edit tool input)
CREDENTIAL_PATH_PATTERNS = [
    r"credentials/credentials\.json",
    r"credentials/[a-f0-9\-]+\.json",  # service account UUID files
]

# Bash commands that indicate credential file access
BASH_CREDENTIAL_PATTERNS = [
    r"(cat|less|head|tail|more|xxd|hexdump|strings)\s+.*credentials/",
    r"python[23]?\s+.*-c\s+.*credentials/",
    r"python[23]?\s+.*open\s*\(.*credentials/",
    r"jq\s+.*credentials/",
    r"cp\s+.*credentials/",
    r"curl.*file://.*credentials/",
    r"base64\s+.*credentials/",
]

# Pre-compiled for performance
_COMPILED_PATH_PATTERNS = [re.compile(p) for p in CREDENTIAL_PATH_PATTERNS]
_COMPILED_BASH_PATTERNS = [re.compile(p) for p in BASH_CREDENTIAL_PATTERNS]


def is_credential_access(input_value: str, tool_type: str) -> bool:
    """
    Check if a tool input targets credential files.

    Args:
        input_value: The file path (for Read/Write/Edit) or command (for Bash)
        tool_type: One of "read", "write", "edit", "bash"

    Returns:
        True if the input matches credential access patterns
    """
    if not input_value:
        return False

    if tool_type in ("read", "write", "edit"):
        return any(p.search(input_value) for p in _COMPILED_PATH_PATTERNS)

    if tool_type == "bash":
        return any(p.search(input_value) for p in _COMPILED_BASH_PATTERNS)

    return False


def get_event_type(tool_type: str) -> str:
    """
    Map tool type to security event type constant.

    Args:
        tool_type: One of "read", "write", "edit", "bash"

    Returns:
        Event type string for SecurityEvent.event_type
    """
    if tool_type == "bash":
        return "CREDENTIAL_BASH_ACCESS"
    if tool_type in ("write", "edit"):
        return "CREDENTIAL_WRITE_ATTEMPT"
    return "CREDENTIAL_READ_ATTEMPT"
