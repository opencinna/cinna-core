# Bash Tool

Executes shell commands in the workspace directory (`CLAUDE_CODE_WORKSPACE`).

**When to use**:
- Running Python scripts: `uv run python scripts/script.py`
- Installing packages: `uv pip install package-name`
- Listing files: `ls ./scripts/`

**When NOT to use**:
- Reading file contents (use Read tool instead)
- Answering simple questions that don't require commands

**Important**: Quote paths with spaces. Use `&&` to chain dependent commands.
