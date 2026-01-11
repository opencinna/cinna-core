"""
Agent Environment Service - Business logic for agent environment operations.
"""
import json
import logging
import zipfile
import uuid
from pathlib import Path
from typing import Optional, Tuple
from datetime import datetime

from .models import FileNode, FolderSummary, WorkspaceTreeResponse

logger = logging.getLogger(__name__)


class AgentEnvService:
    """
    Handles business logic for agent environment operations.

    Responsibilities:
    - Read/write agent prompt files (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md)
    - Manage workspace configuration
    - Validate file operations
    - Manage plugins
    """

    def __init__(self, workspace_dir: str):
        """
        Initialize AgentEnvService.

        Args:
            workspace_dir: Path to workspace directory
        """
        self.workspace_dir = Path(workspace_dir)
        self.docs_dir = self.workspace_dir / "docs"
        self.credentials_dir = self.workspace_dir / "credentials"
        self.plugins_dir = self.workspace_dir / "plugins"

    def get_agent_prompts(self) -> Tuple[Optional[str], Optional[str]]:
        """
        Get current agent prompts from docs files.

        Returns:
            Tuple of (workflow_prompt, entrypoint_prompt)
            Either value can be None if file doesn't exist or is empty
        """
        workflow_prompt = self._read_prompt_file("WORKFLOW_PROMPT.md")
        entrypoint_prompt = self._read_prompt_file("ENTRYPOINT_PROMPT.md")

        return workflow_prompt, entrypoint_prompt

    def update_agent_prompts(
        self,
        workflow_prompt: Optional[str] = None,
        entrypoint_prompt: Optional[str] = None
    ) -> list[str]:
        """
        Update agent prompts in docs files.

        Args:
            workflow_prompt: New content for WORKFLOW_PROMPT.md (None to skip)
            entrypoint_prompt: New content for ENTRYPOINT_PROMPT.md (None to skip)

        Returns:
            List of updated filenames

        Raises:
            IOError: If file write fails
        """
        # Ensure docs directory exists
        self.docs_dir.mkdir(parents=True, exist_ok=True)

        updated_files = []

        if workflow_prompt is not None:
            self._write_prompt_file("WORKFLOW_PROMPT.md", workflow_prompt)
            updated_files.append("WORKFLOW_PROMPT.md")
            logger.info(f"Updated WORKFLOW_PROMPT.md ({len(workflow_prompt)} chars)")

        if entrypoint_prompt is not None:
            self._write_prompt_file("ENTRYPOINT_PROMPT.md", entrypoint_prompt)
            updated_files.append("ENTRYPOINT_PROMPT.md")
            logger.info(f"Updated ENTRYPOINT_PROMPT.md ({len(entrypoint_prompt)} chars)")

        return updated_files

    def _read_prompt_file(self, filename: str) -> Optional[str]:
        """
        Read a prompt file from docs directory.

        Args:
            filename: Name of the file to read

        Returns:
            File content if exists and not empty, None otherwise
        """
        file_path = self.docs_dir / filename

        if not file_path.exists():
            logger.debug(f"{filename} not found at {file_path}")
            return None

        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                content = f.read()
                if content.strip():
                    logger.info(f"Read {filename} ({len(content)} chars)")
                    return content
                else:
                    logger.debug(f"{filename} is empty")
                    return None
        except Exception as e:
            logger.error(f"Failed to read {filename}: {e}")
            return None

    def _write_prompt_file(self, filename: str, content: str):
        """
        Write content to a prompt file in docs directory.

        Args:
            filename: Name of the file to write
            content: Content to write

        Raises:
            IOError: If write operation fails
        """
        file_path = self.docs_dir / filename

        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(content)
            logger.info(f"Wrote {filename} ({len(content)} chars)")
        except Exception as e:
            logger.error(f"Failed to write {filename}: {e}")
            raise IOError(f"Failed to write {filename}: {str(e)}")

    def validate_workspace(self) -> bool:
        """
        Validate that workspace directory exists and is accessible.

        Returns:
            True if workspace is valid, False otherwise
        """
        if not self.workspace_dir.exists():
            logger.error(f"Workspace directory does not exist: {self.workspace_dir}")
            return False

        if not self.workspace_dir.is_dir():
            logger.error(f"Workspace path is not a directory: {self.workspace_dir}")
            return False

        # Check if we can write to workspace
        try:
            test_file = self.workspace_dir / ".workspace_test"
            test_file.touch()
            test_file.unlink()
            return True
        except Exception as e:
            logger.error(f"Workspace is not writable: {e}")
            return False

    def ensure_docs_directory(self):
        """
        Ensure docs directory exists in workspace.

        Creates the directory if it doesn't exist.
        """
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        logger.debug(f"Ensured docs directory exists: {self.docs_dir}")

    def get_workspace_info(self) -> dict:
        """
        Get information about the workspace.

        Returns:
            Dictionary with workspace metadata
        """
        scripts_dir = self.workspace_dir / "scripts"
        files_dir = self.workspace_dir / "files"
        uploads_dir = self.workspace_dir / "uploads"

        return {
            "workspace_dir": str(self.workspace_dir),
            "docs_dir": str(self.docs_dir),
            "has_scripts_dir": scripts_dir.exists(),
            "has_files_dir": files_dir.exists(),
            "has_uploads_dir": uploads_dir.exists(),
            "has_docs_dir": self.docs_dir.exists(),
            "has_workflow_prompt": (self.docs_dir / "WORKFLOW_PROMPT.md").exists(),
            "has_entrypoint_prompt": (self.docs_dir / "ENTRYPOINT_PROMPT.md").exists(),
        }

    def update_credentials(
        self,
        credentials_json: list[dict],
        credentials_readme: str
    ) -> list[str]:
        """
        Update credentials in workspace credentials directory.

        Creates two files:
        - credentials/credentials.json: Full credentials data with actual values
        - credentials/README.md: Redacted documentation for agent prompt

        Args:
            credentials_json: List of credentials with full data
            credentials_readme: Markdown content with redacted credentials

        Returns:
            List of updated filenames

        Raises:
            IOError: If file write fails
        """
        import json

        # Ensure credentials directory exists
        self.credentials_dir.mkdir(parents=True, exist_ok=True)

        updated_files = []

        try:
            # Write credentials.json with full data
            credentials_file = self.credentials_dir / "credentials.json"
            with open(credentials_file, 'w', encoding='utf-8') as f:
                json.dump(credentials_json, f, indent=2)
            updated_files.append("credentials.json")
            logger.info(f"Updated credentials.json ({len(credentials_json)} credentials)")

            # Write README.md with redacted data
            readme_file = self.credentials_dir / "README.md"
            with open(readme_file, 'w', encoding='utf-8') as f:
                f.write(credentials_readme)
            updated_files.append("README.md")
            logger.info(f"Updated credentials/README.md ({len(credentials_readme)} chars)")

            return updated_files

        except Exception as e:
            logger.error(f"Failed to update credentials: {e}")
            raise IOError(f"Failed to update credentials: {str(e)}")

    def get_credentials_readme(self) -> Optional[str]:
        """
        Get credentials README content.

        Returns:
            Content of credentials/README.md if exists and not empty, None otherwise
        """
        readme_file = self.credentials_dir / "README.md"

        if not readme_file.exists():
            logger.debug(f"credentials/README.md not found at {readme_file}")
            return None

        try:
            with open(readme_file, 'r', encoding='utf-8') as f:
                content = f.read()
                if content.strip():
                    logger.info(f"Read credentials/README.md ({len(content)} chars)")
                    return content
                else:
                    logger.debug("credentials/README.md is empty")
                    return None
        except Exception as e:
            logger.error(f"Failed to read credentials/README.md: {e}")
            return None

    def get_agent_handover_config(self) -> dict:
        """
        Get agent handover configuration from JSON file.

        Returns:
            Dictionary with handovers list and handover_prompt, or empty structure if file doesn't exist
        """
        config_file = self.docs_dir / "agent_handover_config.json"

        if not config_file.exists():
            logger.debug(f"agent_handover_config.json not found at {config_file}")
            return {"handovers": [], "handover_prompt": ""}

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                logger.info(f"Read agent_handover_config.json ({len(config.get('handovers', []))} handovers)")
                return config
        except Exception as e:
            logger.error(f"Failed to read agent_handover_config.json: {e}")
            return {"handovers": [], "handover_prompt": ""}

    def update_agent_handover_config(
        self,
        handovers: list[dict],
        handover_prompt: str
    ) -> bool:
        """
        Update agent handover configuration in JSON file.

        Creates/updates docs/agent_handover_config.json with:
        - handovers: Array of {id, name, prompt} objects
        - handover_prompt: Prompt text to append to conversation mode system prompt

        Args:
            handovers: List of handover configs with id, name, prompt fields
            handover_prompt: Instructions for handover tool usage

        Returns:
            True if successful, False otherwise

        Raises:
            IOError: If file write fails
        """
        # Ensure docs directory exists
        self.docs_dir.mkdir(parents=True, exist_ok=True)

        config_file = self.docs_dir / "agent_handover_config.json"
        config = {
            "handovers": handovers,
            "handover_prompt": handover_prompt
        }

        try:
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2)
            logger.info(f"Updated agent_handover_config.json ({len(handovers)} handovers)")
            return True
        except Exception as e:
            logger.error(f"Failed to write agent_handover_config.json: {e}")
            raise IOError(f"Failed to write agent_handover_config.json: {str(e)}")

    def validate_workspace_path(self, relative_path: str) -> Path:
        """
        Validate a relative path is safe and within workspace.

        Args:
            relative_path: User-provided path (e.g., "files/data.csv")

        Returns:
            Resolved absolute Path if valid

        Security Checks:
        1. Reject absolute paths (starts with /)
        2. Reject paths with .. components
        3. Resolve to absolute path
        4. Verify resolved path is under workspace_dir
        5. Check for symlinks pointing outside workspace

        Raises:
            ValueError: If path is invalid or unsafe
        """
        # 1. Reject absolute paths
        if relative_path.startswith('/'):
            raise ValueError("Absolute paths not allowed")

        # 2. Reject .. components
        if '..' in Path(relative_path).parts:
            raise ValueError("Parent directory references (..) not allowed")

        # 3. Resolve to absolute path
        full_path = (self.workspace_dir / relative_path).resolve()

        # 4. Verify within workspace boundary
        try:
            full_path.relative_to(self.workspace_dir.resolve())
        except ValueError:
            raise ValueError("Path outside workspace boundary")

        # 5. Check symlinks don't escape workspace
        if full_path.is_symlink():
            link_target = full_path.readlink()
            if link_target.is_absolute():
                resolved_target = link_target.resolve()
            else:
                resolved_target = (full_path.parent / link_target).resolve()

            try:
                resolved_target.relative_to(self.workspace_dir.resolve())
            except ValueError:
                raise ValueError("Symlink points outside workspace")

        return full_path

    def get_workspace_tree(self) -> WorkspaceTreeResponse:
        """
        Build complete workspace tree for files, logs, scripts, docs, uploads folders.

        Returns:
            WorkspaceTreeResponse with full tree structure and summaries

        Raises:
            IOError: If workspace directory doesn't exist or isn't accessible
        """
        if not self.workspace_dir.exists():
            raise IOError(f"Workspace directory does not exist: {self.workspace_dir}")

        if not self.workspace_dir.is_dir():
            raise IOError(f"Workspace path is not a directory: {self.workspace_dir}")

        # Define the 5 main folders to scan
        folders = ["files", "logs", "scripts", "docs", "uploads"]
        tree_nodes = {}
        summaries = {}

        for folder_name in folders:
            folder_path = self.workspace_dir / folder_name

            if not folder_path.exists():
                # Create empty folder node if directory doesn't exist
                logger.warning(f"Folder {folder_name} does not exist, creating empty node")
                tree_nodes[folder_name] = FileNode(
                    name=folder_name,
                    type="folder",
                    path=folder_name,
                    size=0,
                    modified=None,
                    children=[]
                )
                summaries[folder_name] = FolderSummary(fileCount=0, totalSize=0)
            else:
                # Build tree for existing folder
                logger.debug(f"Building tree for {folder_name}")
                node = self._build_tree_recursive(folder_path, self.workspace_dir)
                tree_nodes[folder_name] = node

                # Calculate summary
                summary = self._calculate_folder_summary(node)
                summaries[folder_name] = summary
                logger.info(f"{folder_name}: {summary.fileCount} files, {summary.totalSize} bytes")

        return WorkspaceTreeResponse(
            files=tree_nodes["files"],
            logs=tree_nodes["logs"],
            scripts=tree_nodes["scripts"],
            docs=tree_nodes["docs"],
            uploads=tree_nodes["uploads"],
            summaries=summaries
        )

    def _build_tree_recursive(self, dir_path: Path, relative_to: Path) -> FileNode:
        """
        Recursively build tree structure for a directory.

        Args:
            dir_path: Absolute path to directory
            relative_to: Base path for calculating relative paths

        Returns:
            FileNode with children populated recursively
        """
        # Get relative path
        try:
            rel_path = dir_path.relative_to(relative_to)
            path_str = str(rel_path)
        except ValueError:
            # Shouldn't happen if we're called correctly, but handle gracefully
            path_str = dir_path.name

        # Get directory metadata
        try:
            stat = dir_path.stat()
            modified = datetime.fromtimestamp(stat.st_mtime)
        except Exception as e:
            logger.warning(f"Failed to get metadata for {dir_path}: {e}")
            modified = None

        # Create folder node
        node = FileNode(
            name=dir_path.name,
            type="folder",
            path=path_str,
            size=None,  # Will be calculated later if needed
            modified=modified,
            children=[]
        )

        # List directory contents
        try:
            items = list(dir_path.iterdir())
        except PermissionError as e:
            logger.warning(f"Permission denied reading {dir_path}: {e}")
            return node
        except Exception as e:
            logger.error(f"Error reading directory {dir_path}: {e}")
            return node

        # Separate files and folders
        files = []
        folders = []

        for item in items:
            # Skip hidden files and __pycache__
            if item.name.startswith('.') or item.name == '__pycache__':
                continue

            if item.is_file():
                try:
                    stat = item.stat()
                    file_node = FileNode(
                        name=item.name,
                        type="file",
                        path=str(item.relative_to(relative_to)),
                        size=stat.st_size,
                        modified=datetime.fromtimestamp(stat.st_mtime),
                        children=None
                    )
                    files.append(file_node)
                except Exception as e:
                    logger.warning(f"Failed to process file {item}: {e}")

            elif item.is_dir():
                # Recursively process subdirectory
                try:
                    folder_node = self._build_tree_recursive(item, relative_to)
                    folders.append(folder_node)
                except Exception as e:
                    logger.warning(f"Failed to process directory {item}: {e}")

        # Sort: folders alphabetically first, then files alphabetically
        folders.sort(key=lambda x: x.name.lower())
        files.sort(key=lambda x: x.name.lower())

        # Combine into children list
        node.children = folders + files

        return node

    def _calculate_folder_summary(self, node: FileNode) -> FolderSummary:
        """
        Calculate fileCount and totalSize for a folder tree.

        Args:
            node: Root FileNode (must be type="folder")

        Returns:
            FolderSummary with counts and sizes
        """
        if node.type != "folder":
            # If it's a file, count it
            return FolderSummary(fileCount=1, totalSize=node.size or 0)

        file_count = 0
        total_size = 0

        # Recursively traverse children
        if node.children:
            for child in node.children:
                if child.type == "file":
                    file_count += 1
                    total_size += child.size or 0
                else:
                    # Recursively process subfolder
                    sub_summary = self._calculate_folder_summary(child)
                    file_count += sub_summary.fileCount
                    total_size += sub_summary.totalSize

        return FolderSummary(fileCount=file_count, totalSize=total_size)

    def create_workspace_zip(self, relative_path: str) -> Path:
        """
        Create a zip archive of a workspace folder or file.

        Args:
            relative_path: Path relative to workspace root (e.g., "files/project1")

        Returns:
            Path to created zip file in /tmp

        Security:
        1. Validate relative_path doesn't escape workspace (no .., absolute paths)
        2. Resolve to absolute path and verify it's under workspace_dir
        3. Check path exists

        Implementation:
        1. Validate and resolve path
        2. Create temporary zip file: /tmp/workspace_{uuid}.zip
        3. If path is file: add single file to zip
        4. If path is folder: recursively add all contents
        5. Return zip file path

        Raises:
            IOError: If path invalid, doesn't exist, or zip creation fails
        """
        # Validate path
        try:
            absolute_path = self.validate_workspace_path(relative_path)
        except ValueError as e:
            raise IOError(f"Invalid path: {e}")

        if not absolute_path.exists():
            raise IOError(f"Path does not exist: {relative_path}")

        # Create temp zip file
        zip_id = str(uuid.uuid4())[:8]
        zip_path = Path(f"/tmp/workspace_{zip_id}.zip")

        try:
            with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
                if absolute_path.is_file():
                    # Add single file
                    arcname = absolute_path.name
                    zipf.write(absolute_path, arcname=arcname)
                    logger.debug(f"Added file {arcname} to zip")
                else:
                    # Add folder recursively
                    for item in absolute_path.rglob('*'):
                        if item.is_file():
                            # Skip hidden files and __pycache__
                            if any(part.startswith('.') or part == '__pycache__' for part in item.parts):
                                continue

                            # Calculate relative path within the zip
                            arcname = item.relative_to(absolute_path)
                            zipf.write(item, arcname=str(arcname))

                    logger.debug(f"Added folder {absolute_path.name} to zip")

            logger.info(f"Created zip archive: {zip_path} ({zip_path.stat().st_size} bytes)")
            return zip_path

        except Exception as e:
            # Clean up zip file if creation failed
            if zip_path.exists():
                zip_path.unlink()
            logger.error(f"Failed to create zip archive: {e}")
            raise IOError(f"Failed to create zip archive: {str(e)}")

    @staticmethod
    def sanitize_filename(filename: str) -> str:
        """
        Sanitize filename for agent-env storage.

        Rules:
        - Remove/replace dangerous characters
        - Preserve extension
        - Limit length to 255 characters
        - Replace spaces with underscores
        """
        import re
        import unicodedata
        import os

        # Normalize unicode
        filename = unicodedata.normalize('NFKD', filename)
        filename = filename.encode('ascii', 'ignore').decode('ascii')

        # Remove path separators and dangerous chars
        filename = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', filename)

        # Replace spaces with underscores
        filename = filename.replace(' ', '_')

        # Remove multiple underscores
        filename = re.sub(r'_+', '_', filename)

        # Truncate to 255 chars while preserving extension
        if len(filename) > 255:
            name, ext = os.path.splitext(filename)
            max_name_len = 255 - len(ext)
            filename = name[:max_name_len] + ext

        # Ensure filename is not empty
        if not filename:
            filename = "file"

        return filename

    @staticmethod
    def resolve_filename_conflict(
        filename: str,
        directory: Path,
        max_attempts: int = 100
    ) -> str:
        """
        Generate unique filename if conflict exists.

        If document.pdf exists, tries:
        - document_1.pdf
        - document_2.pdf
        - ...
        - document_100.pdf

        Raises HTTPException if max_attempts exceeded.
        """
        from fastapi import HTTPException
        import os

        base_path = directory / filename
        if not base_path.exists():
            return filename

        name, ext = os.path.splitext(filename)

        for i in range(1, max_attempts + 1):
            new_filename = f"{name}_{i}{ext}"
            new_path = directory / new_filename
            if not new_path.exists():
                return new_filename

        raise HTTPException(
            status_code=500,
            detail=f"Could not resolve filename conflict for {filename} after {max_attempts} attempts"
        )

    # SQLite Database Methods

    SQLITE_EXTENSIONS = [".db", ".sqlite", ".sqlite3"]

    @staticmethod
    def is_sqlite_file(filename: str) -> bool:
        """Check if filename has SQLite extension."""
        lower = filename.lower()
        return any(lower.endswith(ext) for ext in AgentEnvService.SQLITE_EXTENSIONS)

    def get_database_tables(self, relative_path: str) -> list[dict]:
        """
        Get list of tables and views from SQLite database.

        Args:
            relative_path: Path to SQLite file relative to workspace

        Returns:
            List of dicts with 'name' and 'type' keys (type is 'table' or 'view')

        Raises:
            ValueError: If path is invalid
            IOError: If file doesn't exist or can't be read
        """
        import sqlite3

        absolute_path = self.validate_workspace_path(relative_path)

        if not absolute_path.exists():
            raise IOError(f"Database file not found: {relative_path}")

        if not absolute_path.is_file():
            raise IOError(f"Path is not a file: {relative_path}")

        try:
            conn = sqlite3.connect(str(absolute_path), timeout=5.0)
            cursor = conn.cursor()

            # Get tables
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = [{"name": row[0], "type": "table"} for row in cursor.fetchall()]

            # Get views
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
            )
            views = [{"name": row[0], "type": "view"} for row in cursor.fetchall()]

            conn.close()

            return tables + views

        except sqlite3.Error as e:
            logger.error(f"SQLite error reading {relative_path}: {e}")
            raise IOError(f"Failed to read database: {str(e)}")

    def get_database_schema(self, relative_path: str) -> dict:
        """
        Get complete schema for SQLite database including tables, views, and columns.

        Args:
            relative_path: Path to SQLite file relative to workspace

        Returns:
            Dict with path, tables, and views (each with columns)

        Raises:
            ValueError: If path is invalid
            IOError: If file doesn't exist or can't be read
        """
        import sqlite3

        absolute_path = self.validate_workspace_path(relative_path)

        if not absolute_path.exists():
            raise IOError(f"Database file not found: {relative_path}")

        if not absolute_path.is_file():
            raise IOError(f"Path is not a file: {relative_path}")

        try:
            conn = sqlite3.connect(str(absolute_path), timeout=5.0)
            cursor = conn.cursor()

            def get_columns(table_name: str) -> list[dict]:
                """Get column info for a table/view."""
                cursor.execute(f"PRAGMA table_info('{table_name}')")
                columns = []
                for row in cursor.fetchall():
                    columns.append({
                        "name": row[1],
                        "type": row[2] or "TEXT",
                        "nullable": row[3] == 0,  # notnull = 0 means nullable
                        "primary_key": row[5] > 0
                    })
                return columns

            # Get tables with columns
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
            tables = []
            for row in cursor.fetchall():
                table_name = row[0]
                tables.append({
                    "name": table_name,
                    "type": "table",
                    "columns": get_columns(table_name)
                })

            # Get views with columns
            cursor.execute(
                "SELECT name FROM sqlite_master WHERE type='view' ORDER BY name"
            )
            views = []
            for row in cursor.fetchall():
                view_name = row[0]
                views.append({
                    "name": view_name,
                    "type": "view",
                    "columns": get_columns(view_name)
                })

            conn.close()

            return {
                "path": relative_path,
                "tables": tables,
                "views": views
            }

        except sqlite3.Error as e:
            logger.error(f"SQLite error reading schema from {relative_path}: {e}")
            raise IOError(f"Failed to read database schema: {str(e)}")

    def execute_query(
        self,
        relative_path: str,
        query: str,
        page: int | None = None,
        page_size: int | None = None,
        timeout_seconds: int = 30
    ) -> dict:
        """
        Execute SQL query on SQLite database.

        Args:
            relative_path: Path to SQLite file relative to workspace
            query: SQL query to execute
            page: Page number (1-based) for SELECT queries, None = no pagination
            page_size: Number of rows per page, None = no pagination
            timeout_seconds: Query timeout in seconds

        Returns:
            Dict with columns, rows, pagination info, and execution stats

        For SELECT queries: returns paginated results (if page/page_size provided)
        For DML queries: returns rows_affected count
        """
        import sqlite3
        import time

        absolute_path = self.validate_workspace_path(relative_path)

        if not absolute_path.exists():
            return {
                "columns": [],
                "rows": [],
                "total_rows": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "execution_time_ms": 0,
                "query_type": "OTHER",
                "rows_affected": None,
                "error": f"Database file not found: {relative_path}",
                "error_type": "file_error"
            }

        # Detect query type
        query_stripped = query.strip().upper()
        if query_stripped.startswith("SELECT"):
            query_type = "SELECT"
        elif query_stripped.startswith("INSERT"):
            query_type = "INSERT"
        elif query_stripped.startswith("UPDATE"):
            query_type = "UPDATE"
        elif query_stripped.startswith("DELETE"):
            query_type = "DELETE"
        else:
            query_type = "OTHER"

        try:
            conn = sqlite3.connect(str(absolute_path), timeout=float(timeout_seconds))
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()

            start_time = time.time()

            if query_type == "SELECT":
                # Check if pagination is requested
                use_pagination = page is not None and page_size is not None

                if use_pagination:
                    # For paginated SELECT queries, get total count first
                    # Wrap in subquery to handle complex queries
                    try:
                        count_query = f"SELECT COUNT(*) FROM ({query})"
                        cursor.execute(count_query)
                        total_rows = cursor.fetchone()[0]
                    except sqlite3.Error:
                        # If count fails (e.g., UNION queries), execute without count
                        total_rows = -1

                    # Execute with pagination
                    offset = (page - 1) * page_size
                    paginated_query = f"{query} LIMIT {page_size} OFFSET {offset}"
                    cursor.execute(paginated_query)
                else:
                    # No pagination - execute query as-is
                    cursor.execute(query)
                    total_rows = -1

                # Get column names
                columns = [description[0] for description in cursor.description] if cursor.description else []

                # Fetch rows
                rows = []
                for row in cursor.fetchall():
                    rows.append(list(row))

                execution_time_ms = (time.time() - start_time) * 1000

                # Calculate has_more
                if use_pagination:
                    if total_rows >= 0:
                        has_more = (offset + len(rows)) < total_rows
                    else:
                        # If we couldn't get count, check if we got a full page
                        has_more = len(rows) == page_size
                        total_rows = offset + len(rows)
                        if has_more:
                            total_rows = -1  # Unknown total
                else:
                    has_more = False
                    total_rows = len(rows)

                conn.close()

                return {
                    "columns": columns,
                    "rows": rows,
                    "total_rows": total_rows,
                    "page": page,
                    "page_size": page_size,
                    "has_more": has_more,
                    "execution_time_ms": round(execution_time_ms, 2),
                    "query_type": query_type,
                    "rows_affected": None,
                    "error": None,
                    "error_type": None
                }

            else:
                # DML or other queries
                cursor.execute(query)
                rows_affected = cursor.rowcount
                conn.commit()

                execution_time_ms = (time.time() - start_time) * 1000
                conn.close()

                return {
                    "columns": [],
                    "rows": [],
                    "total_rows": 0,
                    "page": 1,
                    "page_size": page_size,
                    "has_more": False,
                    "execution_time_ms": round(execution_time_ms, 2),
                    "query_type": query_type,
                    "rows_affected": rows_affected,
                    "error": None,
                    "error_type": None
                }

        except sqlite3.OperationalError as e:
            error_str = str(e).lower()
            if "timeout" in error_str or "locked" in error_str:
                error_type = "timeout"
            else:
                error_type = "execution_error"

            logger.error(f"SQLite OperationalError on {relative_path}: {e}, original_query={query!r}, page={page}, page_size={page_size}")
            return {
                "columns": [],
                "rows": [],
                "total_rows": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "execution_time_ms": 0,
                "query_type": query_type,
                "rows_affected": None,
                "error": str(e),
                "error_type": error_type
            }

        except sqlite3.Error as e:
            logger.error(f"SQLite error executing query on {relative_path}: {e}")
            return {
                "columns": [],
                "rows": [],
                "total_rows": 0,
                "page": page,
                "page_size": page_size,
                "has_more": False,
                "execution_time_ms": 0,
                "query_type": query_type,
                "rows_affected": None,
                "error": str(e),
                "error_type": "syntax_error" if "syntax" in str(e).lower() else "execution_error"
            }

    # =========================================================================
    # Plugin Management Methods
    # =========================================================================

    def update_plugins(
        self,
        active_plugins: list[dict],
        settings_json: dict,
        plugin_files: dict[str, dict[str, str]]
    ) -> list[str]:
        """
        Update plugins in workspace plugins directory.

        Creates the following structure:
        /app/workspace/plugins/
        ├── settings.json
        └── [marketplace_name]/
            └── [plugin_name]/
                └── (plugin files)

        Args:
            active_plugins: List of plugin info dicts with marketplace_name, plugin_name, etc.
            settings_json: Settings dictionary to write as settings.json
            plugin_files: Dict mapping "marketplace/plugin" -> {relative_path: base64_content}

        Returns:
            List of updated paths

        Raises:
            IOError: If file operations fail
        """
        import base64

        # Ensure plugins directory exists
        self.plugins_dir.mkdir(parents=True, exist_ok=True)

        updated_paths = []

        try:
            # Write plugin files
            for plugin_key, files in plugin_files.items():
                # plugin_key format: "marketplace_name/plugin_name"
                parts = plugin_key.split("/", 1)
                if len(parts) != 2:
                    logger.warning(f"Invalid plugin key format: {plugin_key}")
                    continue

                marketplace_name, plugin_name = parts
                plugin_dir = self.plugins_dir / marketplace_name / plugin_name

                # Create plugin directory
                plugin_dir.mkdir(parents=True, exist_ok=True)

                # Write each file
                for relative_path, base64_content in files.items():
                    file_path = plugin_dir / relative_path

                    # Create parent directories if needed
                    file_path.parent.mkdir(parents=True, exist_ok=True)

                    # Decode and write file content
                    try:
                        content = base64.b64decode(base64_content)
                        file_path.write_bytes(content)
                        updated_paths.append(str(file_path.relative_to(self.workspace_dir)))
                        logger.debug(f"Wrote plugin file: {file_path}")
                    except Exception as e:
                        logger.error(f"Failed to write plugin file {file_path}: {e}")

                logger.info(f"Updated plugin: {marketplace_name}/{plugin_name}")

            # Write settings.json
            settings_file = self.plugins_dir / "settings.json"
            with open(settings_file, 'w', encoding='utf-8') as f:
                json.dump(settings_json, f, indent=2)
            updated_paths.append("plugins/settings.json")
            logger.info(f"Updated plugins/settings.json with {len(active_plugins)} active plugins")

            return updated_paths

        except Exception as e:
            logger.error(f"Failed to update plugins: {e}")
            raise IOError(f"Failed to update plugins: {str(e)}")

    def get_plugins_settings(self) -> dict:
        """
        Get current plugins settings from settings.json.

        Returns:
            Dictionary with active_plugins list, or empty structure if not found
        """
        settings_file = self.plugins_dir / "settings.json"

        if not settings_file.exists():
            logger.debug(f"plugins/settings.json not found at {settings_file}")
            return {"active_plugins": []}

        try:
            with open(settings_file, 'r', encoding='utf-8') as f:
                settings = json.load(f)
                logger.info(f"Read plugins/settings.json ({len(settings.get('active_plugins', []))} plugins)")
                return settings
        except Exception as e:
            logger.error(f"Failed to read plugins/settings.json: {e}")
            return {"active_plugins": []}

    def get_active_plugins_for_mode(self, mode: str) -> list[dict]:
        """
        Get active plugins filtered by mode.

        Args:
            mode: "conversation" or "building"

        Returns:
            List of plugin dicts that are active for the specified mode
        """
        settings = self.get_plugins_settings()
        active_plugins = settings.get("active_plugins", [])

        if mode == "conversation":
            return [p for p in active_plugins if p.get("conversation_mode", False)]
        elif mode == "building":
            return [p for p in active_plugins if p.get("building_mode", False)]
        else:
            # If mode is not specified, return all active plugins
            logger.warning(f"Unknown mode '{mode}', returning all active plugins")
            return active_plugins
