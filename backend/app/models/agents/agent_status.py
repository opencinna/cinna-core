"""Response models for agent status endpoints."""
import uuid
from datetime import datetime
from sqlmodel import SQLModel


class AgentStatusPublic(SQLModel):
    """Public representation of an agent's self-reported status snapshot."""
    agent_id: uuid.UUID
    environment_id: uuid.UUID | None = None
    severity: str | None = None                  # ok | warning | error | info | unknown
    summary: str | None = None                   # short status description (≤ 512 chars)
    reported_at: datetime | None = None          # when the agent reported this status
    reported_at_source: str | None = None        # "frontmatter" | "file_mtime" | None
    fetched_at: datetime | None = None           # when the platform last read STATUS.md
    raw: str | None = None                       # full STATUS.md body (may be truncated at 64 KB)
    body: str | None = None                      # raw minus the leading YAML frontmatter block
    has_structured_metadata: bool = False        # True when YAML frontmatter was successfully parsed
    prev_severity: str | None = None             # severity before the most recent transition
    severity_changed_at: datetime | None = None  # timestamp of the last severity transition


class AgentStatusListPublic(SQLModel):
    """Paginated list of agent status snapshots."""
    items: list[AgentStatusPublic]
