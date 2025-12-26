"""
Agent Environment Service - Business logic for agent environment operations.
"""
import logging
from pathlib import Path
from typing import Optional, Tuple

logger = logging.getLogger(__name__)


class AgentEnvService:
    """
    Handles business logic for agent environment operations.

    Responsibilities:
    - Read/write agent prompt files (WORKFLOW_PROMPT.md, ENTRYPOINT_PROMPT.md)
    - Manage workspace configuration
    - Validate file operations
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

        return {
            "workspace_dir": str(self.workspace_dir),
            "docs_dir": str(self.docs_dir),
            "has_scripts_dir": scripts_dir.exists(),
            "has_files_dir": files_dir.exists(),
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
