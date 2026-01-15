"""
Handover prompt generator - creates handover prompts between agents.

This module generates handover prompts that define when and how
one agent should trigger another agent with specific context.
Uses the provider manager for cascade provider selection.
"""
from pathlib import Path

from .provider_manager import get_provider_manager


# Paths to prompt template files
PROMPTS_DIR = Path(__file__).parent / "prompts"
HANDOVER_PROMPT_TEMPLATE = PROMPTS_DIR / "handover_generator_prompt.md"


def _load_prompt_template(file_path: Path) -> str:
    """Load prompt template from file."""
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to load prompt template from {file_path}: {e}")


def generate_handover_prompt(
    source_agent_name: str,
    source_entrypoint: str | None,
    source_workflow: str | None,
    target_agent_name: str,
    target_entrypoint: str | None,
    target_workflow: str | None,
) -> dict:
    """
    Generate handover prompt from source to target agent.

    Args:
        source_agent_name: Name of the agent doing the handover
        source_entrypoint: Source agent's entrypoint prompt
        source_workflow: Source agent's workflow prompt
        target_agent_name: Name of the agent receiving the handover
        target_entrypoint: Target agent's entrypoint prompt
        target_workflow: Target agent's workflow prompt

    Returns:
        dict with keys:
            - success: bool
            - handover_prompt: Generated prompt (if success)
            - error: Error message (if not success)
    """
    try:
        manager = get_provider_manager()

        # Load template
        template = _load_prompt_template(HANDOVER_PROMPT_TEMPLATE)

        # Build agent context
        source_context = f"""
**Source Agent: {source_agent_name}**
- Entrypoint: {source_entrypoint or "Not defined"}
- Workflow: {source_workflow or "Not defined"}
""".strip()

        target_context = f"""
**Target Agent: {target_agent_name}**
- Entrypoint: {target_entrypoint or "Not defined"}
- Workflow: {target_workflow or "Not defined"}
""".strip()

        # Construct full prompt
        prompt = f"""{template}

---

## Agent Information

{source_context}

{target_context}

---

Generate the handover prompt now. Remember: 2-3 sentences maximum, include condition, context, and example.
"""

        # Call LLM using provider manager (cascade fallback)
        response = manager.generate_content(prompt)

        # Clean up response
        handover_prompt = response.text

        # Remove any markdown code blocks
        if handover_prompt.startswith("```"):
            lines = handover_prompt.split("\n")
            handover_prompt = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            handover_prompt = handover_prompt.strip()

        # Remove quotes if present
        handover_prompt = handover_prompt.strip('"').strip("'")

        return {
            "success": True,
            "handover_prompt": handover_prompt
        }

    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to generate handover prompt: {str(e)}"
        }
