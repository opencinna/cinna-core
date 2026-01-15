"""
Title generator - creates concise conversation titles from first message.

Uses the provider manager for cascade provider selection.
"""
from .provider_manager import get_provider_manager


def generate_conversation_title(message_content: str) -> str:
    """
    Generate a concise title for a conversation based on the first message.

    Args:
        message_content: First message from the user

    Returns:
        str: Concise title for the conversation (max 100 chars)
    """
    # Create prompt
    prompt = f"""You are an AI assistant that creates concise conversation titles.

Given the first message from a user, generate a short, descriptive title for the conversation (maximum 100 characters).
The title should capture the main topic or intent of the message.

User's message: {message_content}

Return ONLY the title text, without any quotes, markdown, or formatting.
"""

    # Generate content using provider manager (cascade fallback)
    manager = get_provider_manager()
    response = manager.generate_content(prompt)

    # Extract title
    title = response.text

    # Remove quotes if present
    if (title.startswith('"') and title.endswith('"')) or (
        title.startswith("'") and title.endswith("'")
    ):
        title = title[1:-1]

    # Truncate to 100 chars if needed
    if len(title) > 100:
        title = title[:97] + "..."

    # Fallback to truncated message if result is empty
    if not title:
        title = message_content[:100]
        if len(message_content) > 100:
            title += "..."

    return title
