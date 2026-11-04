"""
Per-agent CLI — building-context response schema.

Returned by ``GET /api/v1/cli/agents/{agent_id}/building-context``: the
building prompt env-core assembles, its raw parts, the companion prompt files,
environment settings, and the platform skills the environment carries.
"""
from typing import Any

from pydantic import ConfigDict
from sqlmodel import Field, SQLModel


class CLIPlatformSkill(SQLModel):
    """A platform skill projected into the environment (outside ``/app/workspace``)."""
    name: str
    # The skill's path inside the Local Agent Kit, e.g. ``skills/cinna-dashboards``.
    kit_path: str


class CLIBuildingContext(SQLModel):
    """Building context for a CLI workspace.

    Extra keys a newer env-core adds pass through unchanged.
    """
    model_config = ConfigDict(extra="allow")

    building_prompt: str = ""
    building_prompt_parts: dict[str, Any] = Field(default_factory=dict)
    prompt_files: dict[str, str] = Field(default_factory=dict)
    settings: dict[str, Any] = Field(default_factory=dict)
    platform_skills: list[CLIPlatformSkill] = Field(default_factory=list)
