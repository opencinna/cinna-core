from pydantic import BaseModel
from datetime import datetime
from typing import Optional


class HealthCheckResponse(BaseModel):
    """Health check response model"""
    status: str  # "healthy" | "degraded" | "unhealthy"
    timestamp: datetime
    uptime: int  # Seconds since startup
    message: str | None = None


class PromptsConfig(BaseModel):
    """Agent prompts configuration"""
    workflow_prompt: str | None = None
    entrypoint_prompt: str | None = None


class ChatRequest(BaseModel):
    """Chat message request"""
    message: str
    session_id: Optional[str] = None  # External SDK session ID
    mode: str = "conversation"  # "building" | "conversation"
    system_prompt: Optional[str] = None


class ChatResponse(BaseModel):
    """Chat message response"""
    response: str
    session_id: Optional[str] = None
    metadata: dict = {}
