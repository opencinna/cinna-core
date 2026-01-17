#!/usr/bin/env python3
"""
Interactive A2A Client for testing agent communication.

This script connects to an A2A-compatible agent. Agents can be selected from
a local cache (agents.json) or added by providing a URL.

Usage:
    python run_a2a_agent.py

The script maintains a local cache of agents in agents.json for easy reuse.
"""

import asyncio
from pathlib import Path

from utils import (
    A2AConnection,
    AgentCache,
    CachedAgent,
    SessionLogger,
    extract_text_from_message,
    fetch_public_agent_card,
    verify_agent_token,
)


class InteractiveChatClient:
    """Interactive console client for chat communication with A2A agent."""

    def __init__(self, connection: A2AConnection):
        """Initialize chat client.

        Args:
            connection: A2AConnection instance for agent communication
        """
        self.connection = connection

    async def chat(self, message: str) -> str | None:
        """Send a chat message and print the response.

        Args:
            message: Message text to send

        Returns:
            Full response text or None on error
        """
        response_parts: list[str] = []

        async for event_type, content in self.connection.send_message(message):
            if event_type == "text":
                print(f"\nAgent: {content}", flush=True)
                response_parts.append(content)
            elif event_type == "error":
                print(f"\nError: {content}")
                return None
        return "".join(response_parts)

    def handle_command(self, command: str) -> bool | str:
        """Handle a chat command.

        Args:
            command: Command string (e.g., '/quit', '/new')

        Returns:
            True if should continue chat loop (command handled),
            False if should exit,
            'async' if command needs async handling
        """
        parts = command.split(maxsplit=1)
        cmd_name = parts[0].lower()

        if cmd_name in ["/quit", "/exit"]:
            print("Goodbye!")
            return False

        if cmd_name == "/new":
            self.connection.reset_conversation()
            print("Started new conversation")
            return True

        if cmd_name == "/status":
            print(f"  Task ID: {self.connection.task_id or '(none)'}")
            print(f"  Context ID: {self.connection.context_id or '(none)'}")
            if self.connection.logger:
                print(f"  Log file: {self.connection.logger.log_file}")
            return True

        if cmd_name in ["/session", "/task"]:
            # Need async handling
            return "async"

        if cmd_name == "/tasks":
            # Need async handling
            return "async"

        # Unknown command, treat as regular message
        return True

    async def handle_async_command(self, command: str) -> None:
        """Handle commands that require async operations.

        Args:
            command: Command string
        """
        parts = command.split(maxsplit=1)
        cmd_name = parts[0].lower()

        if cmd_name in ["/session", "/task"]:
            if len(parts) < 2:
                print("Usage: /session <task_id>")
                return

            session_id = parts[1].strip()
            print(f"\nResuming session: {session_id}")

            # Fetch task to verify it exists
            task = await self.connection.get_task(session_id)
            if task:
                self.connection.set_session(session_id)
                print("  Session resumed successfully")
                state = task.status.state if task.status else "unknown"
                # Handle TaskState enum
                state_str = state.value if hasattr(state, "value") else str(state)
                print(f"  State: {state_str}")
                if task.status and task.status.timestamp:
                    print(f"  Last updated: {task.status.timestamp}")

                # Show message history
                if task.history:
                    print(f"\n  --- Conversation History ({len(task.history)} messages) ---\n")
                    for msg in task.history:
                        role = msg.role
                        role_str = role.value if hasattr(role, "value") else str(role)
                        text = extract_text_from_message(msg)
                        if role_str == "user":
                            print(f"You: {text}\n")
                        else:
                            print(f"Agent: {text}\n")
                    print("  --- End of History ---")
            else:
                print("  Error: Task not found or access denied")

        elif cmd_name == "/tasks":
            print("\nFetching tasks...")
            tasks = await self.connection.list_tasks(limit=20)

            if not tasks:
                print("  No tasks found")
                return

            print(f"\n  {'ID':<36}  {'State':<15}  {'Updated'}")
            print("  " + "-" * 70)
            for task in tasks:
                state = task.status.state if task.status else "unknown"
                timestamp = task.status.timestamp[:19] if task.status and task.status.timestamp else "N/A"
                task_id = task.id or "N/A"
                print(f"  {task_id:<36}  {state:<15}  {timestamp}")

            print(f"\n  Total: {len(tasks)} task(s)")
            print("  Use '/session <id>' to resume a session")

    async def run(self) -> None:
        """Run the interactive chat loop."""
        if not await self.connection.connect():
            return

        self._print_commands()
        print("\nType your message and press Enter to send.")
        print("=" * 60)

        try:
            while True:
                try:
                    user_input = input("\nYou: ").strip()
                except (EOFError, KeyboardInterrupt):
                    print("\nGoodbye!")
                    break

                if not user_input:
                    continue

                # Handle commands
                if user_input.startswith("/"):
                    result = self.handle_command(user_input)
                    if result is False:
                        break
                    if result == "async":
                        await self.handle_async_command(user_input)
                        continue
                    if result is True:
                        # Command handled synchronously
                        cmd_name = user_input.split()[0].lower()
                        if cmd_name in ["/new", "/status"]:
                            continue

                # Send message to agent
                await self.chat(user_input)

        finally:
            await self.connection.close()

    def _print_commands(self) -> None:
        """Print available commands."""
        print("\nCommands:")
        print("  /quit or /exit   - Exit the client")
        print("  /new             - Start a new conversation")
        print("  /status          - Show current session info")
        print("  /tasks           - List all tasks (sessions)")
        print("  /session <id>    - Resume a session by task ID")
        print("  /task <id>       - Alias for /session")


async def add_new_agent(cache: AgentCache) -> CachedAgent | None:
    """Add a new agent by URL.

    Fetches the agent card, verifies token if required, and caches the agent.

    Args:
        cache: Agent cache instance

    Returns:
        CachedAgent if successful, None if cancelled or failed
    """
    print("\n--- Add New Agent ---")
    try:
        url = input("Enter agent URL: ").strip()
    except (EOFError, KeyboardInterrupt):
        return None

    if not url:
        print("No URL provided.")
        return None

    # Normalize URL
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    print(f"\nFetching agent card from: {url}")

    # Try to fetch public card
    public_card, requires_auth, error = await fetch_public_agent_card(url)

    if error and not requires_auth:
        print(f"Error: {error}")
        return None

    if public_card:
        print(f"  Agent: {public_card.name}")
        if public_card.description:
            print(f"  Description: {public_card.description}")

    # Check if authentication is required
    token = ""
    if requires_auth or (public_card and getattr(public_card, "supportsAuthenticatedExtendedCard", False)):
        print("\nThis agent requires authentication.")
        try:
            token = input("Enter access token: ").strip()
        except (EOFError, KeyboardInterrupt):
            return None

        if not token:
            print("No token provided.")
            return None

        # Verify token
        print("\nVerifying token...")
        extended_card, verify_error = await verify_agent_token(url, token)

        if verify_error:
            print(f"Error: {verify_error}")
            return None

        print("  Token verified successfully!")
        if extended_card:
            public_card = extended_card
            print(f"  Agent: {extended_card.name}")
            if extended_card.description:
                print(f"  Description: {extended_card.description}")
            if extended_card.skills:
                print(f"  Skills: {[s.name for s in extended_card.skills]}")

    if not public_card:
        print("Error: Could not fetch agent card.")
        return None

    # Create cached agent
    agent = CachedAgent(
        name=public_card.name,
        url=url,
        token=token,
        description=public_card.description or "",
    )

    # Save to cache
    cache.add_agent(agent)
    print(f"\nAgent '{agent.name}' added to cache.")

    return agent


def select_agent(cache: AgentCache) -> CachedAgent | None:
    """Display agent selection menu and return selected agent.

    Args:
        cache: Agent cache instance

    Returns:
        Selected CachedAgent or None if adding new agent
    """
    agents = cache.load_agents()

    print("\n" + "=" * 60)
    print("Select an Agent")
    print("=" * 60)

    if agents:
        for i, agent in enumerate(agents, 1):
            print(f"  {i}. {agent.name}")
            if agent.description:
                print(f"     {agent.description[:50]}{'...' if len(agent.description) > 50 else ''}")
        print()

    print(f"  N. Add new agent by URL")
    print(f"  Q. Quit")
    print("=" * 60)

    try:
        choice = input("\nYour choice: ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return None

    if choice == "q":
        return None

    if choice == "n":
        # Return special marker to indicate new agent flow
        return CachedAgent(name="__NEW__", url="", token="")

    # Try to parse as number
    try:
        idx = int(choice) - 1
        if 0 <= idx < len(agents):
            return agents[idx]
        print("Invalid selection.")
        return select_agent(cache)  # Retry
    except ValueError:
        print("Invalid input.")
        return select_agent(cache)  # Retry


async def main() -> None:
    """Main entry point."""
    logs_dir = Path(__file__).parent / "logs"
    cache = AgentCache()

    print("\n" + "=" * 60)
    print("Interactive A2A Client")
    print("=" * 60)

    while True:
        # Select or add agent
        selected = select_agent(cache)

        if selected is None:
            print("\nGoodbye!")
            return

        # Check if user wants to add new agent
        if selected.name == "__NEW__":
            selected = await add_new_agent(cache)
            if selected is None:
                continue  # Go back to selection menu

        print(f"\nConnecting to: {selected.name}")
        print(f"URL: {selected.url}")

        # Create connection and run client
        logger = SessionLogger(logs_dir)
        connection = A2AConnection(selected.url, selected.token, logger)
        client = InteractiveChatClient(connection)

        await client.run()

        # After client exits, ask if user wants to connect to another agent
        try:
            again = input("\nConnect to another agent? (y/N): ").strip().lower()
            if again != "y":
                print("\nGoodbye!")
                return
        except (EOFError, KeyboardInterrupt):
            print("\nGoodbye!")
            return


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nGoodbye!")
