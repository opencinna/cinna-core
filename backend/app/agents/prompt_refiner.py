"""
Prompt refiner - helps users write better prompts for their AI agents.
"""
from google.genai import Client


def refine_prompt(
    user_input: str,
    has_files_attached: bool,
    agent_name: str | None,
    entrypoint_prompt: str | None,
    workflow_prompt: str | None,
    mode: str,
    is_new_agent: bool,
    api_key: str,
) -> dict:
    """
    Refine a user's prompt to make it more effective for the AI agent.

    Args:
        user_input: The user's current input text
        has_files_attached: Whether files are attached to the message
        agent_name: Name of the agent (if any)
        entrypoint_prompt: Agent's entrypoint prompt (if any)
        workflow_prompt: Agent's workflow prompt (if any)
        mode: Session mode - "building" or "conversation"
        is_new_agent: Whether this is a new agent being created
        api_key: Google API key for Gemini

    Returns:
        dict with keys:
            - success: bool
            - refined_prompt: The improved prompt text (if success)
            - error: Error message (if not success)
    """
    client = Client(api_key=api_key)

    # Build context about the agent
    agent_context = ""
    if agent_name:
        agent_context = f"\n## Agent Context\n- **Agent Name**: {agent_name}"
        if entrypoint_prompt:
            agent_context += f"\n- **Agent's Purpose (Entrypoint)**: {entrypoint_prompt}"
        if workflow_prompt:
            agent_context += f"\n- **Agent's Workflow Instructions**: {workflow_prompt}"

    # Mode explanation
    mode_explanation = ""
    if mode == "building":
        mode_explanation = """
## Current Mode: Building Mode
In Building Mode, the user is setting up the agent's capabilities, writing scripts, configuring integrations, or doing initial setup.
The agent has access to development tools and can modify files, create scripts, and configure the workspace.
A good building prompt should be clear about what capability or setup is needed."""
    else:
        mode_explanation = """
## Current Mode: Conversation Mode
In Conversation Mode, the user is executing tasks using the agent's pre-built tools and workflows.
The agent focuses on completing tasks efficiently using available tools.
A good conversation prompt should be clear, specific, and actionable."""

    # New agent context
    new_agent_context = ""
    if is_new_agent:
        new_agent_context = """
## Special Context: New Agent Creation
The user is creating a brand new agent. A good prompt for building a new agent should describe:
- What the agent should help with (its purpose)
- What kind of tasks it will perform
- Any specific tools or integrations it needs"""

    # Files context
    files_context = ""
    if has_files_attached:
        files_context = "\n\n**Note**: The user has attached files to this message. The refined prompt should acknowledge and reference these files appropriately."

    prompt = f"""You are a prompt refinement assistant. Your job is to help users write better prompts for their AI agents.

Given the user's input and context, improve their prompt to be more effective, clear, and actionable.

{mode_explanation}
{agent_context}
{new_agent_context}

## User's Current Input
{user_input}
{files_context}

## Your Task
Rewrite the user's prompt to make it more effective. Guidelines:
1. Keep the same intent and meaning
2. Make it clearer and more specific
3. Add relevant context if helpful
4. Keep it concise - maximum 3 short paragraphs
5. Don't add unnecessary fluff or formal language
6. Preserve any technical details or specific requirements
7. If the input is already good, make minimal changes

Return ONLY the refined prompt text, without any markdown formatting, quotes, or explanations.
Do not include phrases like "Here's the refined prompt:" - just output the prompt itself."""

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )

        refined = response.text.strip()

        # Remove quotes if present
        if (refined.startswith('"') and refined.endswith('"')) or (
            refined.startswith("'") and refined.endswith("'")
        ):
            refined = refined[1:-1]

        # Remove markdown quotes if present
        if refined.startswith("```") and refined.endswith("```"):
            refined = refined[3:-3].strip()

        if not refined:
            return {
                "success": False,
                "error": "Failed to generate refined prompt - empty response",
            }

        return {
            "success": True,
            "refined_prompt": refined,
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to refine prompt: {str(e)}",
        }
