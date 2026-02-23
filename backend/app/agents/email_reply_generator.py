"""
Email reply generator - crafts professional email replies from agent session results.

Uses the provider manager for cascade provider selection.
"""
import json
from pathlib import Path

from .provider_manager import get_provider_manager


# Path to prompt template
PROMPTS_DIR = Path(__file__).parent / "prompts"
EMAIL_REPLY_PROMPT = PROMPTS_DIR / "email_reply_generator_prompt.md"


def _load_prompt_template() -> str:
    """Load the email reply generator prompt template."""
    try:
        return EMAIL_REPLY_PROMPT.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to load email reply generator prompt: {e}")


def generate_email_reply(
    original_subject: str,
    original_body: str,
    original_sender: str,
    session_result: str,
    task_description: str,
) -> dict:
    """
    Generate a professional email reply based on original email and session results.

    Args:
        original_subject: Subject of the original email
        original_body: Body of the original email
        original_sender: Email address of the original sender
        session_result: The agent's session result/output
        task_description: The task description that was executed

    Returns:
        dict with keys:
            - success: bool
            - reply_body: The generated reply body (if success)
            - reply_subject: The generated reply subject (if success)
            - error: Error message (if not success)
    """
    manager = get_provider_manager()

    # Load system prompt
    system_prompt = _load_prompt_template()

    # Build the full prompt
    prompt = f"""{system_prompt}

## Original Email
**From:** {original_sender}
**Subject:** {original_subject}

{original_body}

## Task Description (what was asked)
{task_description}

## Agent Session Results
{session_result}

## Your Task
Generate a professional email reply that addresses the original email using the agent's results. Return a valid JSON response with the structure shown in the Response Format section above.

IMPORTANT: Return ONLY the JSON object, no markdown code blocks or other formatting.
"""

    try:
        response = manager.generate_content(prompt)
        response_text = response.text.strip()

        # Clean up response if wrapped in code blocks
        if response_text.startswith("```"):
            lines = response_text.split("\n")
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines[-1].strip() == "```":
                lines = lines[:-1]
            response_text = "\n".join(lines)

        # Parse JSON response
        result = json.loads(response_text)

        if not isinstance(result, dict):
            return {
                "success": False,
                "error": "Invalid response format - expected JSON object",
            }

        reply_body = result.get("reply_body", "")
        reply_subject = result.get("reply_subject", f"Re: {original_subject}")

        if not reply_body:
            return {
                "success": False,
                "error": "No reply body in response",
            }

        return {
            "success": True,
            "reply_body": reply_body,
            "reply_subject": reply_subject,
        }

    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Failed to parse AI response as JSON: {str(e)}",
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to generate email reply: {str(e)}",
        }
