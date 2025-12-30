"""
Schedule generator - converts natural language to CRON expressions.

This module generates CRON schedules from natural language input using AI.
It handles timezone conversion and validates minimum execution frequency.
"""
import json
from pathlib import Path
from datetime import datetime
import pytz
from google.genai import Client


PROMPTS_DIR = Path(__file__).parent / "prompts"
SCHEDULE_PROMPT = PROMPTS_DIR / "schedule_generator_prompt.md"


def _load_prompt_template(file_path: Path) -> str:
    """Load prompt template from file."""
    try:
        return file_path.read_text(encoding="utf-8")
    except Exception as e:
        raise RuntimeError(f"Failed to load prompt template from {file_path}: {e}")


def generate_agent_schedule(
    natural_language: str,
    timezone: str,
    api_key: str
) -> dict:
    """
    Convert natural language schedule to CRON string.

    Args:
        natural_language: User's input (e.g., "every workday at 7 AM")
        timezone: IANA timezone (e.g., "Europe/Berlin")
        api_key: Google API key

    Returns:
        Dict with success, description, cron_string, or error
    """
    try:
        client = Client(api_key=api_key)

        # Load prompt template
        template = _load_prompt_template(SCHEDULE_PROMPT)

        # Get current time in user's timezone
        user_tz = pytz.timezone(timezone)
        current_time = datetime.now(user_tz)

        # Construct prompt with user input
        prompt = f"""{template}

---

## User Input

Natural language: {natural_language}
User timezone: {timezone}
Current time: {current_time.isoformat()}

Generate the schedule configuration in JSON format.
"""

        # Call LLM
        response = client.models.generate_content(
            model="gemini-2.5-flash-lite",
            contents=prompt,
        )

        # Parse response (expected to be JSON)
        response_text = response.text.strip()

        # Remove markdown code blocks if present
        if response_text.startswith("```json"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            response_text = response_text.strip()
        elif response_text.startswith("```"):
            lines = response_text.split("\n")
            response_text = "\n".join(lines[1:-1] if lines[-1] == "```" else lines[1:])
            response_text = response_text.strip()

        result = json.loads(response_text)

        return result

    except json.JSONDecodeError as e:
        return {
            "success": False,
            "error": f"Failed to parse AI response: {str(e)}"
        }
    except Exception as e:
        return {
            "success": False,
            "error": f"Failed to generate schedule: {str(e)}"
        }
