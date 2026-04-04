"""
HMAC signing for session context data.

The backend signs session_context with AGENT_AUTH_TOKEN before sending to agent-env.
Agent-env verifies the signature to ensure context authenticity (not forged by LLM/scripts).

Uses HMAC-SHA256 with canonical JSON (sorted keys, no whitespace) for deterministic signing.
"""
import hashlib
import hmac
import json


def _canonical_json(data: dict) -> bytes:
    """Produce deterministic JSON bytes for HMAC signing."""
    return json.dumps(data, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign_session_context(context: dict, signing_key: str) -> str:
    """Sign session context dict with HMAC-SHA256.

    Args:
        context: Session context dict to sign
        signing_key: AGENT_AUTH_TOKEN used as HMAC key

    Returns:
        Hex-encoded HMAC-SHA256 signature
    """
    return hmac.new(
        signing_key.encode("utf-8"),
        _canonical_json(context),
        hashlib.sha256,
    ).hexdigest()


def verify_session_context(context: dict, signature: str, signing_key: str) -> bool:
    """Verify HMAC-SHA256 signature of session context.

    Args:
        context: Session context dict to verify
        signature: Hex-encoded HMAC-SHA256 signature to check
        signing_key: AGENT_AUTH_TOKEN used as HMAC key

    Returns:
        True if signature is valid
    """
    expected = sign_session_context(context, signing_key)
    return hmac.compare_digest(expected, signature)
