"""
/files and /files-all commands - list workspace files with clickable links.

/files     - shows only the "files" folder (user-facing data files)
/files-all - shows all workspace folders (files, scripts, logs, docs, uploads)

Link generation varies by context:
- UI users: links to frontend file viewer route
- A2A clients: links with short-lived tokens for browser access
"""
import logging
import urllib.parse

from app.services.agents.command_service import CommandHandler, CommandContext, CommandResult
from app.services.environments.agent_workspace_token_service import AgentWorkspaceTokenService
from app.services.environments.environment_service import EnvironmentService
from app.models.environments.environment import AgentEnvironment
from app.core.db import create_session

logger = logging.getLogger(__name__)

# All sections in the workspace tree
ALL_SECTIONS = ["files", "scripts", "logs", "docs", "uploads"]

# Section display labels
SECTION_LABELS = {
    "files": "Files",
    "scripts": "Scripts",
    "logs": "Logs",
    "docs": "Docs",
    "uploads": "Uploads",
}


def _format_size(size_bytes: int | None) -> str:
    """Format file size in human-readable form."""
    if size_bytes is None:
        return ""
    if size_bytes < 1024:
        return f" ({size_bytes} B)"
    elif size_bytes < 1024 * 1024:
        return f" ({size_bytes / 1024:.1f} KB)"
    else:
        return f" ({size_bytes / (1024 * 1024):.1f} MB)"


def _collect_files(node: dict, files: list[dict]) -> None:
    """Recursively collect all files from a tree node."""
    if node.get("type") == "file":
        files.append(node)
    children = node.get("children")
    if children:
        for child in children:
            _collect_files(child, files)


def _build_link(
    context: CommandContext,
    file_path: str,
    is_a2a: bool,
    ws_token: str | None,
) -> str:
    """Build a context-aware link for a file."""
    encoded_path = urllib.parse.quote(file_path, safe="/")

    if is_a2a and ws_token:
        # A2A: link to public endpoint with token
        base = context.backend_base_url.rstrip("/")
        return f"{base}/api/v1/shared/workspace/{context.environment_id}/view/{encoded_path}?token={ws_token}"
    else:
        # UI: link to frontend file viewer
        host = context.frontend_host.rstrip("/")
        return f"{host}/environment/{context.environment_id}/file?path={urllib.parse.quote(file_path, safe='')}"


async def _execute_files_listing(
    context: CommandContext,
    sections: list[str],
) -> CommandResult:
    """Shared logic: get workspace tree and build markdown for given sections."""
    # Verify environment is running
    with create_session() as db:
        environment = db.get(AgentEnvironment, context.environment_id)
        if not environment:
            return CommandResult(content="Environment not found.", is_error=True)
        if environment.status != "running":
            return CommandResult(
                content=f"Environment is not running (status: {environment.status}). Start the environment first.",
                is_error=True,
            )

    # Get workspace tree via adapter
    try:
        lifecycle_manager = EnvironmentService.get_lifecycle_manager()
        adapter = lifecycle_manager.get_adapter(environment)
        tree_data = await adapter.get_workspace_tree()
    except Exception as e:
        logger.error(f"Failed to get workspace tree: {e}", exc_info=True)
        return CommandResult(
            content=f"Failed to get workspace files: {str(e)}",
            is_error=True,
        )

    # Generate workspace view token for A2A context
    is_a2a = context.access_token_id is not None
    ws_token = None
    if is_a2a:
        ws_token = AgentWorkspaceTokenService.create_workspace_view_token(
            env_id=context.environment_id,
            agent_id=context.agent_id,
        )

    # Build markdown
    lines: list[str] = []
    total_files = 0

    for section in sections:
        section_node = tree_data.get(section)
        if not section_node:
            continue

        files: list[dict] = []
        _collect_files(section_node, files)
        if not files:
            continue

        total_files += len(files)
        label = SECTION_LABELS.get(section, section.capitalize())
        lines.append(f"**{label}** ({len(files)})")

        for f in files:
            file_path = f.get("path", "")
            file_name = f.get("name", "")
            size_str = _format_size(f.get("size"))
            link = _build_link(
                context=context,
                file_path=file_path,
                is_a2a=is_a2a,
                ws_token=ws_token,
            )
            lines.append(f"- [{file_name}]({link}){size_str}")

        lines.append("")  # blank line between sections

    if total_files == 0:
        return CommandResult(content="No files found in workspace.")

    return CommandResult(content="\n".join(lines))


class FilesCommandHandler(CommandHandler):
    """Handler for /files — lists only the files folder."""

    @property
    def name(self) -> str:
        return "/files"

    @property
    def description(self) -> str:
        return "List files folder contents with clickable links"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        return await _execute_files_listing(context, sections=["files"])


class FilesAllCommandHandler(CommandHandler):
    """Handler for /files-all — lists all workspace folders."""

    @property
    def name(self) -> str:
        return "/files-all"

    @property
    def description(self) -> str:
        return "List all workspace files (files, scripts, logs, docs, uploads)"

    async def execute(self, context: CommandContext, args: str) -> CommandResult:
        return await _execute_files_listing(context, sections=ALL_SECTIONS)
