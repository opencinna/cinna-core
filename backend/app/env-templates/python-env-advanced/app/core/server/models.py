from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class HealthCheckResponse(BaseModel):
    """Health check response model"""
    status: str  # "healthy" | "degraded" | "unhealthy"
    timestamp: datetime
    uptime: int  # Seconds since startup
    message: str | None = None


class AgentPromptsResponse(BaseModel):
    """Agent prompts from docs files"""
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None


class AgentPromptsUpdate(BaseModel):
    """Update agent prompts in docs files"""
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None


class ChatRequest(BaseModel):
    """Chat message request"""
    message: str
    session_id: Optional[str] = None  # External SDK session ID
    mode: str = "conversation"  # "building" | "conversation"
    agent_sdk: str = "claude"  # SDK to use: "claude" (more options can be added later)
    system_prompt: Optional[str] = None


class ChatResponse(BaseModel):
    """Chat message response"""
    response: str
    session_id: Optional[str] = None
    metadata: dict = {}


class CredentialsUpdate(BaseModel):
    """Update credentials in workspace"""
    credentials_json: list[dict]  # Full credentials data
    credentials_readme: str  # Redacted README for agent prompt
