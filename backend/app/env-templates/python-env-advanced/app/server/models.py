from pydantic import BaseModel
from datetime import datetime


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


class MessageRequest(BaseModel):
    """Chat message request"""
    session_id: str
    message: str
    history: list[dict] = []
    context: dict = {}


class MessageResponse(BaseModel):
    """Chat message response"""
    response: str
    metadata: dict | None = None
