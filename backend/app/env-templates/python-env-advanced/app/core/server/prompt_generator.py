import logging
from pathlib import Path
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class PromptGenerator:
    """
    Handles prompt generation for different agent modes.

    Responsibilities:
    - Load prompt files from workspace
    - Construct system prompts for building mode
    - Construct system prompts for conversation mode
    - Cache loaded prompts for performance
    """

    def __init__(self, workspace_dir: str):
        """
        Initialize PromptGenerator.

        Args:
            workspace_dir: Path to workspace directory
        """
        self.workspace_dir = Path(workspace_dir)

        # Load static prompts that don't change during runtime
        self.building_agent_prompt = self._load_building_agent_prompt()

    def _load_building_agent_prompt(self) -> Optional[str]:
        """
        Load BUILDING_AGENT.md file from app root (not workspace).

        Returns:
            Content of BUILDING_AGENT.md if exists, None otherwise
        """
        # BUILDING_AGENT.md is in /app root, not in workspace
        building_agent_path = Path("/app/BUILDING_AGENT.md")

        if building_agent_path.exists():
            try:
                with open(building_agent_path, 'r', encoding='utf-8') as f:
                    content = f.read()
                    logger.info(f"Loaded BUILDING_AGENT.md ({len(content)} chars)")
                    return content
            except Exception as e:
                logger.error(f"Failed to load BUILDING_AGENT.md: {e}")
                return None
        else:
            logger.debug(f"BUILDING_AGENT.md not found at {building_agent_path}")
            return None

    def _load_scripts_readme(self) -> Optional[str]:
        """
        Load scripts/README.md file from workspace if it exists and is not empty.

        This file contains the catalog of existing scripts and will be included
        in the system prompt so the agent knows about existing scripts.

        Returns:
            Content of scripts/README.md if exists and not empty, None otherwise
        """
        scripts_readme_path = self.workspace_dir / "scripts" / "README.md"

        if scripts_readme_path.exists():
            try:
                with open(scripts_readme_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        logger.info(f"Loaded scripts/README.md ({len(content)} chars)")
                        return content
                    else:
                        logger.debug("scripts/README.md is empty")
                        return None
            except Exception as e:
                logger.error(f"Failed to load scripts/README.md: {e}")
                return None
        else:
            logger.debug(f"scripts/README.md not found at {scripts_readme_path}")
            return None

    def _load_workflow_prompt(self) -> Optional[str]:
        """
        Load docs/WORKFLOW_PROMPT.md file from workspace if it exists and is not empty.

        This file describes the workflow's purpose, capabilities, and execution guidelines.
        The building agent should update this as it develops the workflow.

        Returns:
            Content of docs/WORKFLOW_PROMPT.md if exists and not empty, None otherwise
        """
        workflow_prompt_path = self.workspace_dir / "docs" / "WORKFLOW_PROMPT.md"

        if workflow_prompt_path.exists():
            try:
                with open(workflow_prompt_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        logger.info(f"Loaded docs/WORKFLOW_PROMPT.md ({len(content)} chars)")
                        return content
                    else:
                        logger.debug("docs/WORKFLOW_PROMPT.md is empty")
                        return None
            except Exception as e:
                logger.error(f"Failed to load docs/WORKFLOW_PROMPT.md: {e}")
                return None
        else:
            logger.debug(f"docs/WORKFLOW_PROMPT.md not found at {workflow_prompt_path}")
            return None

    def _load_entrypoint_prompt(self) -> Optional[str]:
        """
        Load docs/ENTRYPOINT_PROMPT.md file from workspace if it exists and is not empty.

        This file defines how the workflow should be triggered (entry point for scheduled/interactive modes).
        The building agent should update this as it defines the workflow's usage.

        Returns:
            Content of docs/ENTRYPOINT_PROMPT.md if exists and not empty, None otherwise
        """
        entrypoint_prompt_path = self.workspace_dir / "docs" / "ENTRYPOINT_PROMPT.md"

        if entrypoint_prompt_path.exists():
            try:
                with open(entrypoint_prompt_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        logger.info(f"Loaded docs/ENTRYPOINT_PROMPT.md ({len(content)} chars)")
                        return content
                    else:
                        logger.debug("docs/ENTRYPOINT_PROMPT.md is empty")
                        return None
            except Exception as e:
                logger.error(f"Failed to load docs/ENTRYPOINT_PROMPT.md: {e}")
                return None
        else:
            logger.debug(f"docs/ENTRYPOINT_PROMPT.md not found at {entrypoint_prompt_path}")
            return None

    def _load_credentials_readme(self) -> Optional[str]:
        """
        Load credentials/README.md file from workspace if it exists and is not empty.

        This file contains documentation of available credentials with redacted sensitive data.
        It's generated by the backend and synced to the environment.

        Returns:
            Content of credentials/README.md if exists and not empty, None otherwise
        """
        credentials_readme_path = self.workspace_dir / "credentials" / "README.md"

        if credentials_readme_path.exists():
            try:
                with open(credentials_readme_path, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                    if content:
                        logger.info(f"Loaded credentials/README.md ({len(content)} chars)")
                        return content
                    else:
                        logger.debug("credentials/README.md is empty")
                        return None
            except Exception as e:
                logger.error(f"Failed to load credentials/README.md: {e}")
                return None
        else:
            logger.debug(f"credentials/README.md not found at {credentials_readme_path}")
            return None

    def generate_building_mode_prompt(self) -> Optional[Dict[str, Any]]:
        """
        Generate system prompt for building mode.

        Building mode uses:
        - Claude Code preset
        - BUILDING_AGENT.md
        - scripts/README.md (if exists)
        - docs/WORKFLOW_PROMPT.md (if exists)
        - docs/ENTRYPOINT_PROMPT.md (if exists)
        - credentials/README.md (if exists)

        Returns:
            SystemPromptPreset dict for Claude SDK, or None if building agent prompt not available
        """
        if not self.building_agent_prompt:
            logger.warning("Building mode requested but BUILDING_AGENT.md not loaded")
            return {
                "type": "preset",
                "preset": "claude_code"
            }

        # Start with base building prompt
        building_prompt = self.building_agent_prompt

        # Append scripts README if it exists
        scripts_readme = self._load_scripts_readme()
        if scripts_readme:
            building_prompt += (
                f"\n\n---\n\n## Existing Scripts in Workspace\n\n"
                f"The following is the current contents of `./scripts/README.md` which catalogs "
                f"all existing scripts in this workspace:\n\n"
                f"```markdown\n{scripts_readme}\n```\n\n"
                f"**Important**: When you create, modify, or remove scripts, you MUST update this file to keep it accurate."
            )
            logger.info("Included scripts/README.md in building mode prompt")

        # Append workflow documentation if it exists
        workflow_prompt = self._load_workflow_prompt()
        if workflow_prompt:
            building_prompt += (
                f"\n\n---\n\n## Current Workflow Configuration\n\n"
                f"The following is the current contents of `./docs/WORKFLOW_PROMPT.md` which describes "
                f"the workflow's purpose and capabilities:\n\n"
                f"```markdown\n{workflow_prompt}\n```\n\n"
                f"**Important**: As you develop scripts and define the workflow's capabilities, you MUST "
                f"update this file to accurately reflect what the workflow can do."
            )
            logger.info("Included docs/WORKFLOW_PROMPT.md in building mode prompt")

        # Append entrypoint prompt if it exists
        entrypoint_prompt = self._load_entrypoint_prompt()
        if entrypoint_prompt:
            building_prompt += (
                f"\n\n---\n\n## Current Entry Point Configuration\n\n"
                f"The following is the current contents of `./docs/ENTRYPOINT_PROMPT.md` which defines "
                f"how this workflow should be invoked:\n\n"
                f"```markdown\n{entrypoint_prompt}\n```\n\n"
                f"**Important**: As you develop the workflow, you MUST update this file to define clear "
                f"examples of how to trigger this workflow in conversation mode."
            )
            logger.info("Included docs/ENTRYPOINT_PROMPT.md in building mode prompt")

        # Append credentials README if it exists
        credentials_readme = self._load_credentials_readme()
        if credentials_readme:
            building_prompt += (
                f"\n\n---\n\n## Available Credentials\n\n"
                f"The following is the current contents of `./credentials/README.md` which documents "
                f"credentials shared with this agent (with sensitive data redacted):\n\n"
                f"```markdown\n{credentials_readme}\n```\n\n"
                f"**CRITICAL SECURITY RULES**:\n"
                f"- **NEVER** read `./credentials/credentials.json` directly in this conversation\n"
                f"- **NEVER** log or print credential values in your messages\n"
                f"- **ONLY** access credentials programmatically in the scripts you create\n"
                f"- Use the structure information above to understand what credentials are available\n"
                f"- Scripts you create can read credentials.json to access the actual credential data"
            )
            logger.info("Included credentials/README.md in building mode prompt")

        # Return SystemPromptPreset dict
        logger.info("Generated building mode prompt with claude_code preset + BUILDING_AGENT.md + docs")
        return {
            "type": "preset",
            "preset": "claude_code",
            "append": building_prompt
        }

    def generate_conversation_mode_prompt(self) -> str:
        """
        Generate system prompt for conversation mode.

        Conversation mode uses:
        - docs/WORKFLOW_PROMPT.md (main system prompt)
        - scripts/README.md (available scripts context)
        - credentials/README.md (available credentials context)

        This is a lightweight mode focused on workflow execution, NOT building.

        Returns:
            Plain string system prompt
        """
        conversation_prompt_parts = []

        # Load workflow prompt (main system prompt for conversation mode)
        workflow_prompt = self._load_workflow_prompt()
        if workflow_prompt:
            conversation_prompt_parts.append(workflow_prompt)
            logger.info("Loaded WORKFLOW_PROMPT.md for conversation mode")
        else:
            logger.warning("WORKFLOW_PROMPT.md not found, conversation mode will have minimal context")

        # Append scripts README to give context about available scripts
        scripts_readme = self._load_scripts_readme()
        if scripts_readme:
            conversation_prompt_parts.append(
                f"\n\n---\n\n## Available Scripts\n\n"
                f"The following scripts are available in `./scripts/`:\n\n"
                f"```markdown\n{scripts_readme}\n```"
            )
            logger.info("Included scripts/README.md in conversation mode prompt")

        # Append credentials README to give context about available credentials
        credentials_readme = self._load_credentials_readme()
        if credentials_readme:
            conversation_prompt_parts.append(
                f"\n\n---\n\n## Available Credentials\n\n"
                f"The following credentials are available to use in your scripts:\n\n"
                f"```markdown\n{credentials_readme}\n```\n\n"
                f"**IMPORTANT**:\n"
                f"- The information above shows all available credentials\n"
                f"- **DO NOT** read `./credentials/credentials.json` directly - use the information above when discussing credentials with users\n"
                f"- Scripts you execute can read `./credentials/credentials.json` to access the actual credential data\n"
                f"- Sensitive values (passwords, tokens) are shown as [REDACTED] above but are available to scripts"
            )
            logger.info("Included credentials/README.md in conversation mode prompt")

        # Combine all parts into a single system prompt string
        if conversation_prompt_parts:
            prompt = "\n".join(conversation_prompt_parts)
            logger.info(f"Generated conversation mode prompt ({len(prompt)} chars)")
            return prompt
        else:
            # No prompts available, use empty string
            logger.warning("No workflow prompt or scripts found for conversation mode")
            return ""

    def generate_prompt(self, mode: str) -> Optional[Dict[str, Any]] | str:
        """
        Generate system prompt for specified mode.

        Args:
            mode: "building" or "conversation"

        Returns:
            SystemPromptPreset dict for building mode, or plain string for conversation mode

        Raises:
            ValueError: If mode is invalid
        """
        if mode == "building":
            return self.generate_building_mode_prompt()
        elif mode == "conversation":
            return self.generate_conversation_mode_prompt()
        else:
            raise ValueError(f"Invalid mode: {mode}. Must be 'building' or 'conversation'")
