"""
Agent generator - creates agent configuration from user description.

This module generates:
1. Agent name (concise, descriptive)
2. Entrypoint prompt (human-like trigger message)
3. Workflow prompt (system prompt for conversation mode)

Prompts are loaded from external .md files for easy maintenance.
Uses the provider manager for cascade provider selection.
"""
from pathlib import Path

from .provider_manager import get_provider_manager


# Paths to prompt template files
PROMPTS_DIR = Path(__file__).parent / "prompts"
ENTRYPOINT_GENERATOR_PROMPT = PROMPTS_DIR / "entrypoint_generator_prompt.md"
WORKFLOW_GENERATOR_PROMPT = PROMPTS_DIR / "workflow_generator_prompt.md"


def _load_prompt_template(file_path: Path) -> str:
    """Load prompt template from file."""
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to load prompt template from {file_path}: {e}")


def generate_agent_name(description: str) -> str:
    """
    Generate a concise agent name from description.

    Args:
        description: User's description of what the agent should do

    Returns:
        str: Concise agent name (max 50 characters)
    """
    manager = get_provider_manager()

    prompt = f"""Generate a concise name for an agent based on this description:

{description}

Requirements:
- Maximum 50 characters
- Descriptive and clear
- No quotes or extra formatting

Return ONLY the name, nothing else.

Example: "Sales Report Generator"
"""

    response = manager.generate_content(prompt)

    name = response.text.strip('"').strip("'")
    return name[:50]  # Ensure max length


def generate_entrypoint_prompt(description: str) -> str:
    """
    Generate human-like entrypoint prompt from description.

    Args:
        description: User's description of what the agent should do

    Returns:
        str: Natural, conversational trigger message
    """
    manager = get_provider_manager()

    # Load entrypoint generator prompt template
    template = _load_prompt_template(ENTRYPOINT_GENERATOR_PROMPT)

    # Combine template with user's description
    prompt = f"""{template}

---

## User's Description

{description}

---

Generate the entrypoint prompt now. Remember: natural, conversational, 1-2 sentences maximum."""

    response = manager.generate_content(prompt)

    # Clean up response
    entrypoint = response.text

    # Remove any markdown code blocks
    if entrypoint.startswith("```"):
        lines = entrypoint.split("\n")
        entrypoint = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        entrypoint = entrypoint.strip()

    return entrypoint


def generate_workflow_prompt(description: str) -> str:
    """
    Generate comprehensive workflow prompt from description.

    Args:
        description: User's description of what the agent should do

    Returns:
        str: Detailed system prompt for conversation mode agent
    """
    manager = get_provider_manager()

    # Load workflow generator prompt template
    template = _load_prompt_template(WORKFLOW_GENERATOR_PROMPT)

    # Combine template with user's description
    prompt = f"""{template}

---

## User's Description

{description}

---

Generate the workflow prompt now. Include role, execution steps, and data presentation guidelines."""

    response = manager.generate_content(prompt)

    # Clean up response
    workflow = response.text

    # Remove outer markdown code blocks if present
    if workflow.startswith("```markdown"):
        lines = workflow.split("\n")
        workflow = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        workflow = workflow.strip()
    elif workflow.startswith("```"):
        lines = workflow.split("\n")
        workflow = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
        workflow = workflow.strip()

    return workflow


def generate_agent_config(description: str) -> dict:
    """
    Generate complete agent configuration from description.

    This function generates:
    1. Agent name (concise, descriptive)
    2. Entrypoint prompt (human-like trigger message)
    3. Workflow prompt (system prompt for conversation mode)

    Args:
        description: User's description of what the agent should do

    Returns:
        dict with keys:
            - name: Agent name (str)
            - entrypoint_prompt: Natural trigger message (str)
            - workflow_prompt: Detailed system prompt (str)
    """
    try:
        # Generate all components
        name = generate_agent_name(description)
        entrypoint = generate_entrypoint_prompt(description)
        workflow = generate_workflow_prompt(description)

        return {
            "name": name,
            "entrypoint_prompt": entrypoint,
            "workflow_prompt": workflow,
        }
    except Exception as e:
        # Fallback to simple config if generation fails
        return {
            "name": f"Agent: {description[:40]}...",
            "entrypoint_prompt": description,
            "workflow_prompt": f"You are an assistant that helps with: {description}",
        }
