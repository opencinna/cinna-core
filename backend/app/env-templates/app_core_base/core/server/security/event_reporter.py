"""
Security Event Reporter

Reports security events to the backend via the environment server proxy endpoint.

Two reporting modes:
- Synchronous (blockable): Waits for the backend response and returns the action
  decision ("allow" or "block"). Used in SDK hooks where the tool call must be
  held until the backend responds.
- Asynchronous (fire-and-forget): Returns immediately without waiting for the
  backend. Used for informational events like OUTPUT_REDACTED.

The environment server proxy is at POST /security/report (routes.py).
It forwards to the backend at POST /api/v1/security-events/report.

Fail-open design: if the backend is unreachable or times out, the tool call is
always allowed and a local warning is logged. Security > Availability is enforced
at the policy level, not the availability level.
"""
import json
import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Environment variables — set by docker-compose from backend config
SERVER_PORT = os.getenv("SERVER_PORT", "8000")
ENV_ID = os.getenv("ENV_ID")
AGENT_ID = os.getenv("AGENT_ID")


def _proxy_url() -> str:
    """Build the local proxy URL for the environment server."""
    return f"http://localhost:{SERVER_PORT}/security/report"


class SecurityEventReporter:
    """
    Reports security events to the backend via the environment server proxy.

    The reporter targets the /security/report proxy endpoint (localhost) which
    forwards to the backend with the AGENT_AUTH_TOKEN already attached.

    Usage (synchronous / blockable):
        reporter = SecurityEventReporter()
        action = reporter.report(
            event_type="CREDENTIAL_READ_ATTEMPT",
            tool_name="Read",
            tool_input="/app/workspace/credentials/credentials.json",
            session_id="...",
        )
        if action == "block":
            # deny the tool call

    Usage (async / fire-and-forget):
        asyncio.create_task(reporter.report_async(event_type="OUTPUT_REDACTED", ...))
    """

    def report(
        self,
        event_type: str,
        tool_name: str | None = None,
        tool_input: str | None = None,
        session_id: str | None = None,
        severity: str = "high",
        details: dict | None = None,
    ) -> str:
        """
        Synchronously report a security event and return the action decision.

        Blocks until the backend responds (up to 3 seconds).
        Returns "allow" on any error (fail-open).

        Args:
            event_type: Security event type constant
            tool_name: SDK tool name ("Read", "Bash", "Write", "Edit")
            tool_input: File path or command string
            session_id: Current backend session ID (optional)
            severity: "low", "medium", "high", "critical"
            details: Additional free-form data

        Returns:
            "allow" or "block"
        """
        try:
            import httpx  # type: ignore[import]
            payload = _build_payload(
                event_type=event_type,
                tool_name=tool_name,
                tool_input=tool_input,
                session_id=session_id,
                severity=severity,
                details=details,
            )
            response = httpx.post(
                _proxy_url(),
                json=payload,
                timeout=3.0,
            )
            response.raise_for_status()
            data = response.json()
            return data.get("action", "allow")
        except Exception as exc:
            logger.warning(
                "Security event report failed (fail-open): %s event_type=%s",
                exc,
                event_type,
            )
            return "allow"

    async def report_async(
        self,
        event_type: str,
        session_id: str | None = None,
        severity: str = "medium",
        details: dict | None = None,
    ) -> None:
        """
        Asynchronously report a security event (fire-and-forget).

        Does not block the caller. Swallows all errors with a warning log.

        Args:
            event_type: Security event type constant (e.g. "OUTPUT_REDACTED")
            session_id: Current backend session ID (optional)
            severity: "low", "medium", "high", "critical"
            details: Additional free-form data
        """
        try:
            import httpx  # type: ignore[import]
            payload = _build_payload(
                event_type=event_type,
                tool_name=None,
                tool_input=None,
                session_id=session_id,
                severity=severity,
                details=details,
            )
            async with httpx.AsyncClient(timeout=5.0) as client:
                await client.post(_proxy_url(), json=payload)
        except Exception as exc:
            logger.warning(
                "Async security event report failed: %s event_type=%s",
                exc,
                event_type,
            )


def _build_payload(
    event_type: str,
    tool_name: str | None,
    tool_input: str | None,
    session_id: str | None,
    severity: str,
    details: dict | None,
) -> dict[str, Any]:
    """Build the JSON payload for the /security/report proxy endpoint."""
    return {
        "event_type": event_type,
        "tool_name": tool_name,
        "tool_input": tool_input,
        "session_id": session_id,
        "environment_id": ENV_ID,
        "agent_id": AGENT_ID,
        "severity": severity,
        "details": details or {},
    }
