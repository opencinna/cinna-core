"""
A2A communication utilities.

This module contains utilities for A2A (Agent-to-Agent) protocol communication,
including connection management, message handling, and payload logging.
"""

import json
import os
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator
from uuid import uuid4

import httpx
from dotenv import load_dotenv

from a2a.client.client import Client, ClientConfig
from a2a.client.client_factory import ClientFactory
from a2a.types import (
    AgentCard,
    Message,
    Role,
    Task,
    TextPart,
)


@dataclass
class CachedAgent:
    """Represents a cached agent configuration."""

    name: str
    url: str
    token: str
    description: str = ""
    added_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "name": self.name,
            "url": self.url,
            "token": self.token,
            "description": self.description,
            "added_at": self.added_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "CachedAgent":
        """Create from dictionary."""
        return cls(
            name=data.get("name", "Unknown Agent"),
            url=data.get("url", ""),
            token=data.get("token", ""),
            description=data.get("description", ""),
            added_at=data.get("added_at", datetime.now().isoformat()),
        )


class AgentCache:
    """Manages the local cache of agents in agents.json."""

    def __init__(self, cache_file: Path | None = None):
        """Initialize agent cache.

        Args:
            cache_file: Path to agents.json. Defaults to agents.json in same directory.
        """
        if cache_file is None:
            cache_file = Path(__file__).parent / "agents.json"
        self.cache_file = cache_file

    def load_agents(self) -> list[CachedAgent]:
        """Load agents from cache file.

        Returns:
            List of cached agents
        """
        if not self.cache_file.exists():
            return []

        try:
            with open(self.cache_file) as f:
                data = json.load(f)
                return [CachedAgent.from_dict(agent) for agent in data]
        except (json.JSONDecodeError, OSError) as e:
            print(f"Warning: Could not load agents cache: {e}")
            return []

    def save_agents(self, agents: list[CachedAgent]) -> None:
        """Save agents to cache file.

        Args:
            agents: List of agents to save
        """
        try:
            with open(self.cache_file, "w") as f:
                json.dump([agent.to_dict() for agent in agents], f, indent=2)
        except OSError as e:
            print(f"Warning: Could not save agents cache: {e}")

    def add_agent(self, agent: CachedAgent) -> None:
        """Add or update an agent in the cache.

        Args:
            agent: Agent to add or update
        """
        agents = self.load_agents()
        # Remove existing agent with same URL if exists
        agents = [a for a in agents if a.url != agent.url]
        agents.append(agent)
        self.save_agents(agents)

    def remove_agent(self, url: str) -> bool:
        """Remove an agent from cache by URL.

        Args:
            url: URL of agent to remove

        Returns:
            True if agent was removed, False if not found
        """
        agents = self.load_agents()
        original_count = len(agents)
        agents = [a for a in agents if a.url != url]
        if len(agents) < original_count:
            self.save_agents(agents)
            return True
        return False


async def fetch_public_agent_card(url: str) -> tuple[AgentCard | None, bool, str | None]:
    """Fetch public agent card from URL.

    Args:
        url: Agent URL

    Returns:
        Tuple of (agent_card, requires_auth, error_message)
        - agent_card: The public agent card if successful
        - requires_auth: True if the agent requires authentication
        - error_message: Error message if failed, None if successful
    """
    agent_url = url.rstrip("/") + "/"

    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(agent_url)
            response.raise_for_status()

            card_data = response.json()
            card = AgentCard(**card_data)

            # Check if agent requires authentication for extended card
            requires_auth = getattr(card, "supportsAuthenticatedExtendedCard", False)

            return card, requires_auth, None

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return None, True, "Agent requires authentication"
        return None, False, f"HTTP Error: {e.response.status_code}"
    except Exception as e:
        return None, False, f"Failed to fetch agent card: {e}"


async def verify_agent_token(url: str, token: str) -> tuple[AgentCard | None, str | None]:
    """Verify token by fetching extended agent card.

    Args:
        url: Agent URL
        token: Access token to verify

    Returns:
        Tuple of (agent_card, error_message)
        - agent_card: The extended agent card if successful
        - error_message: Error message if failed, None if successful
    """
    agent_url = url.rstrip("/") + "/"

    try:
        async with httpx.AsyncClient(
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(30.0, connect=10.0),
            follow_redirects=True,
        ) as client:
            response = await client.get(agent_url)
            response.raise_for_status()

            card_data = response.json()
            card = AgentCard(**card_data)

            return card, None

    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            return None, "Invalid or expired token"
        if e.response.status_code == 403:
            return None, "Access forbidden - check token permissions"
        return None, f"HTTP Error: {e.response.status_code}"
    except Exception as e:
        return None, f"Failed to verify token: {e}"


class SessionLogger:
    """Logs A2A payloads (sent and received) to a session file."""

    def __init__(self, logs_dir: Path):
        self.logs_dir = logs_dir
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        session_id = uuid4().hex[:8]
        self.log_file = self.logs_dir / f"session_{timestamp}_{session_id}.json"
        self.events: list[dict[str, Any]] = []
        print(f"Session log: {self.log_file}")

    def log_sent(self, payload: dict[str, Any]) -> None:
        """Log a sent payload."""
        self.events.append({
            "direction": "sent",
            "timestamp": datetime.now().isoformat(),
            "payload": payload,
        })
        self._save()

    def log_received(self, payload: dict[str, Any]) -> None:
        """Log a received payload."""
        self.events.append({
            "direction": "received",
            "timestamp": datetime.now().isoformat(),
            "payload": payload,
        })
        self._save()

    def _save(self) -> None:
        """Save events to file."""
        with open(self.log_file, "w") as f:
            json.dump(self.events, f, indent=2, default=str)


def load_config(env_file: Path | None = None) -> tuple[str, str]:
    """Load A2A configuration from .env file.

    Args:
        env_file: Path to .env file. If None, uses .env in the same directory as this module.

    Returns:
        Tuple of (agent_url, access_token)
    """
    if env_file is None:
        env_file = Path(__file__).parent / ".env"

    if not env_file.exists():
        print(f"Error: .env file not found at {env_file}")
        print("\nCreate a .env file with:")
        print("  A2A_AGENT_URL=https://your-api-url/api/v1/a2a/{agent_id}/")
        print("  A2A_ACCESS_TOKEN=your-jwt-token")
        sys.exit(1)

    load_dotenv(env_file, override=True)

    agent_url = os.getenv("A2A_AGENT_URL")
    access_token = os.getenv("A2A_ACCESS_TOKEN")

    if not agent_url:
        print("Error: A2A_AGENT_URL not set in .env file")
        sys.exit(1)

    if not access_token:
        print("Error: A2A_ACCESS_TOKEN not set in .env file")
        sys.exit(1)

    print(f"Loaded config from: {env_file}")
    print(f"Agent URL: {agent_url}")

    return agent_url, access_token


def get_text_from_part(part: Any) -> str | None:
    """Extract text from a part, handling various forms.

    Args:
        part: A message part (dict, TextPart, or Part wrapper)

    Returns:
        Extracted text or None if not a text part
    """
    # Handle dict form
    if isinstance(part, dict):
        if part.get("kind") == "text" and "text" in part:
            return part["text"]
        return None

    # Handle Part wrapper (part.root contains the actual TextPart)
    if hasattr(part, "root"):
        inner = part.root
        if getattr(inner, "kind", None) == "text" and hasattr(inner, "text"):
            return inner.text
        return None

    # Handle direct TextPart object
    kind = getattr(part, "kind", None)
    if kind == "text" and hasattr(part, "text"):
        return part.text
    return None


def extract_text_from_message(message: Message) -> str:
    """Extract text content from a Message object.

    Args:
        message: A2A Message object

    Returns:
        Concatenated text from all text parts
    """
    text_parts: list[str] = []
    for part in message.parts:
        text = get_text_from_part(part)
        if text:
            text_parts.append(text)
    return "".join(text_parts)


def extract_text_from_task(task: Task) -> str:
    """Extract text content from a Task object.

    Args:
        task: A2A Task object

    Returns:
        Concatenated text from status message and artifacts
    """
    text_parts: list[str] = []

    # Check status.message for text
    if task.status and task.status.message:
        for part in task.status.message.parts:
            text = get_text_from_part(part)
            if text:
                text_parts.append(text)

    # Check artifacts for text
    if task.artifacts:
        for artifact in task.artifacts:
            if artifact.parts:
                for part in artifact.parts:
                    text = get_text_from_part(part)
                    if text:
                        text_parts.append(text)

    return "".join(text_parts)


class A2AConnection:
    """Manages A2A connection and message exchange."""

    def __init__(self, agent_url: str, access_token: str, logger: SessionLogger | None = None):
        """Initialize A2A connection.

        Args:
            agent_url: Base URL of the A2A agent
            access_token: JWT access token for authentication
            logger: Optional SessionLogger for payload logging
        """
        self.agent_url = agent_url.rstrip("/")
        self.access_token = access_token
        self.logger = logger
        self.task_id: str | None = None
        self.context_id: str | None = None
        self.httpx_client: httpx.AsyncClient | None = None
        self.a2a_client: Client | None = None
        self.agent_card: AgentCard | None = None

    async def connect(self) -> bool:
        """Connect to the agent and fetch the agent card.

        First fetches the public card (without authentication) to discover the agent,
        then fetches the extended card (with authentication) for full details.

        Returns:
            True if connection successful, False otherwise
        """
        print(f"\nConnecting to: {self.agent_url}")

        agent_card_url = self.agent_url
        if not agent_card_url.endswith("/"):
            agent_card_url += "/"

        # Step 1: Fetch public card (without authentication)
        print(f"\nFetching public agent card from: {agent_card_url}")
        try:
            async with httpx.AsyncClient(
                timeout=httpx.Timeout(60.0, connect=10.0),
                follow_redirects=True,
            ) as client:
                response = await client.get(agent_card_url)
                response.raise_for_status()

                public_card_data = response.json()
                if self.logger:
                    self.logger.log_received({"type": "public_agent_card", "data": public_card_data})

                public_card = AgentCard(**public_card_data)
                print(f"\n--- Public Agent Card ---")
                print(f"Name: {public_card.name}")
                if public_card.description:
                    print(f"Description: {public_card.description}")
                supports_extended = getattr(public_card, "supportsAuthenticatedExtendedCard", None)
                if supports_extended is not None:
                    print(f"Supports Extended Card: {supports_extended}")
                if public_card.skills:
                    print(f"Skills: {[s.name for s in public_card.skills]}")
                else:
                    print("Skills: (not available in public card)")

        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                print(f"\nPublic card not available (401) - A2A may not be enabled for this agent")
            else:
                print(f"\nHTTP Error fetching public card: {e.response.status_code}")
                print(f"Response: {e.response.text}")
            # Continue to try authenticated request
        except Exception as e:
            print(f"\nFailed to fetch public card: {e}")
            # Continue to try authenticated request

        # Step 2: Fetch extended card (with authentication)
        print(f"\nFetching extended agent card (authenticated)...")

        # Create httpx client with auth header
        self.httpx_client = httpx.AsyncClient(
            headers={"Authorization": f"Bearer {self.access_token}"},
            timeout=httpx.Timeout(60.0, connect=10.0),
            follow_redirects=True,
        )

        try:
            response = await self.httpx_client.get(agent_card_url)
            response.raise_for_status()

            card_data = response.json()
            if self.logger:
                self.logger.log_received({"type": "extended_agent_card", "data": card_data})

            self.agent_card = AgentCard(**card_data)
            print(f"\n--- Extended Agent Card ---")
            print(f"Name: {self.agent_card.name}")
            if self.agent_card.description:
                print(f"Description: {self.agent_card.description}")
            if self.agent_card.skills:
                print(f"Skills: {[s.name for s in self.agent_card.skills]}")
            else:
                print("Skills: (none)")

            # Check if agent supports streaming
            supports_streaming = False
            if self.agent_card.capabilities and self.agent_card.capabilities.streaming:
                supports_streaming = True
            print(f"Streaming: {supports_streaming}")

            # Use the URL from the agent card (may differ from original due to redirects)
            # This ensures we use the correct URL (e.g., HTTPS) for subsequent requests
            card_url = self.agent_card.url.rstrip("/")
            if card_url != self.agent_url:
                print(f"Using agent card URL: {card_url}")
                self.agent_url = card_url
                # Recreate httpx client with correct base URL to avoid redirect issues
                # (301/302 redirects convert POST to GET, breaking SSE streaming)
                await self.httpx_client.aclose()
                self.httpx_client = httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self.access_token}"},
                    timeout=httpx.Timeout(60.0, connect=10.0),
                    follow_redirects=True,
                )

            # Initialize A2A client using ClientFactory
            config = ClientConfig(
                httpx_client=self.httpx_client,
                streaming=supports_streaming,
            )
            factory = ClientFactory(config=config)
            self.a2a_client = factory.create(self.agent_card)
            return True

        except httpx.HTTPStatusError as e:
            print(f"\nHTTP Error: {e.response.status_code}")
            print(f"Response: {e.response.text}")
            return False
        except Exception as e:
            print(f"\nFailed to connect: {e}")
            return False

    async def send_message(self, text: str) -> AsyncIterator[tuple[str, Any]]:
        """Send a message and yield response events.

        Args:
            text: Message text to send

        Yields:
            Tuples of (event_type, content) where event_type is 'text', 'task_update', or 'error'
        """
        if not self.a2a_client:
            yield ("error", "Not connected to agent")
            return

        # Build Message object
        message = Message(
            messageId=uuid4().hex,
            role=Role.user,
            parts=[TextPart(text=text)],
            taskId=self.task_id,
            contextId=self.context_id,
        )

        # Log sent payload
        sent_payload = message.model_dump(mode="json", exclude_none=True)
        if self.logger:
            self.logger.log_sent(sent_payload)

        try:
            async for event in self.a2a_client.send_message(message):
                # Handle different event types
                if isinstance(event, Message):
                    # Direct message response (non-streaming)
                    event_data = event.model_dump(mode="json", exclude_none=True)
                    if self.logger:
                        self.logger.log_received(event_data)
                    text_content = extract_text_from_message(event)
                    if text_content:
                        yield ("text", text_content)
                elif isinstance(event, tuple) and len(event) >= 2:
                    # ClientEvent: (Task, update_event)
                    task, update = event[0], event[1]
                    # Update task_id and context_id
                    task_id = getattr(task, "id", None)
                    context_id = getattr(task, "context_id", None)
                    if task_id:
                        self.task_id = task_id
                    if context_id:
                        self.context_id = context_id

                    # Log the event
                    if self.logger:
                        task_dump = task.model_dump(mode="json", exclude_none=True) if hasattr(task, "model_dump") else str(task)
                        update_dump = update.model_dump(mode="json", exclude_none=True) if update and hasattr(update, "model_dump") else str(update) if update else None
                        event_data = {"task": task_dump, "update": update_dump}
                        self.logger.log_received(event_data)

                    # Extract text from task
                    text_content = extract_text_from_task(task)
                    if text_content:
                        yield ("text", text_content)

                    yield ("task_update", task)

        except httpx.HTTPStatusError as e:
            error_response = {"error": str(e), "status_code": e.response.status_code}
            try:
                error_response["body"] = e.response.json()
            except Exception:
                error_response["body"] = e.response.text
            if self.logger:
                self.logger.log_received(error_response)
            yield ("error", f"HTTP Error {e.response.status_code}: {e.response.text}")
        except Exception as e:
            if self.logger:
                self.logger.log_received({"error": str(e)})
            yield ("error", str(e))

    def reset_conversation(self) -> None:
        """Reset the conversation state (task_id and context_id)."""
        self.task_id = None
        self.context_id = None

    def set_session(self, session_id: str) -> None:
        """Set the current session (task) ID.

        Args:
            session_id: The session/task ID to resume
        """
        self.task_id = session_id
        self.context_id = session_id

    async def get_task(self, task_id: str) -> Task | None:
        """Get a task by ID using A2A protocol.

        Args:
            task_id: The task ID to fetch

        Returns:
            Task object or None if not found
        """
        if not self.httpx_client:
            return None

        payload = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": "tasks/get",
            "params": {"id": task_id, "historyLength": 5},
        }

        if self.logger:
            self.logger.log_sent(payload)

        try:
            response = await self.httpx_client.post(
                f"{self.agent_url}/",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

            if self.logger:
                self.logger.log_received(data)

            if "result" in data:
                return Task(**data["result"])
            return None
        except Exception as e:
            if self.logger:
                self.logger.log_received({"error": str(e)})
            return None

    async def list_tasks(self, limit: int = 20, offset: int = 0) -> list[Task]:
        """List tasks using A2A protocol (custom extension).

        Args:
            limit: Maximum number of tasks to return
            offset: Offset for pagination

        Returns:
            List of Task objects
        """
        if not self.httpx_client:
            return []

        payload = {
            "jsonrpc": "2.0",
            "id": uuid4().hex,
            "method": "tasks/list",
            "params": {"limit": limit, "offset": offset},
        }

        if self.logger:
            self.logger.log_sent(payload)

        try:
            response = await self.httpx_client.post(
                f"{self.agent_url}/",
                json=payload,
            )
            response.raise_for_status()
            data = response.json()

            if self.logger:
                self.logger.log_received(data)

            if "result" in data and isinstance(data["result"], list):
                return [Task(**t) for t in data["result"]]
            return []
        except Exception as e:
            if self.logger:
                self.logger.log_received({"error": str(e)})
            return []

    async def close(self) -> None:
        """Close the connection."""
        if self.a2a_client:
            await self.a2a_client.close()
        if self.httpx_client:
            await self.httpx_client.aclose()
