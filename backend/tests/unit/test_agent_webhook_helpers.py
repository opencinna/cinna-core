"""
Unit tests for ``AgentWebhookService`` pure helpers.

Header allowlist (``filter_headers``) and session-prompt assembly
(``_assemble_session_prompt``) operate on plain dicts / MagicMock stand-ins —
no DB, no HTTP. The full public-endpoint fire flow is covered in
``tests/api/agents/integrations/agents_webhooks_test.py``.
"""
from unittest.mock import MagicMock

from app.services.agents.agent_webhook_service import AgentWebhookService


# ── Header allowlist ──────────────────────────────────────────────────────────


def test_header_allowlist_strips_sensitive_headers() -> None:
    """
    filter_headers keeps only the allowlisted headers and strips
    authorization / cookie and any other non-allowlisted header.
    """
    incoming = {
        "Authorization": "Bearer super-secret-token",
        "Cookie": "session=abc123",
        "User-Agent": "GitHub-Hookshot/abc",
        "X-GitHub-Event": "push",
        "X-Hub-Signature-256": "sha256=abc",
        "X-Custom-Header": "should be stripped",
        "Content-Type": "application/json",
    }
    filtered = AgentWebhookService.filter_headers(incoming)

    # Sensitive headers must be absent
    assert "authorization" not in filtered
    assert "cookie" not in filtered
    # Non-allowlisted headers must be absent
    assert "x-custom-header" not in filtered
    assert "content-type" not in filtered
    # Allowlisted headers must be present (canonical lowercase)
    assert filtered.get("user-agent") == "GitHub-Hookshot/abc"
    assert filtered.get("x-github-event") == "push"
    assert filtered.get("x-hub-signature-256") == "sha256=abc"


def test_header_allowlist_preserves_all_allowed_headers() -> None:
    """All headers in FORWARDED_HEADERS are passed through when present."""
    incoming = {
        "user-agent": "test-agent",
        "x-forwarded-for": "1.2.3.4, 5.6.7.8",
        "x-real-ip": "1.2.3.4",
        "x-github-event": "push",
        "x-gitlab-event": "Push Hook",
        "x-hub-signature-256": "sha256=deadbeef",
        "x-event-key": "repo:push",
    }
    filtered = AgentWebhookService.filter_headers(incoming)
    for h in AgentWebhookService.FORWARDED_HEADERS:
        if h in incoming:
            assert h in filtered, f"Expected allowlisted header '{h}' to be present"


# ── Prompt assembly ───────────────────────────────────────────────────────────


def test_session_prompt_contains_payload_and_headers() -> None:
    """
    _assemble_session_prompt includes the payload body and allowlisted headers in
    the returned string, and uses the configured webhook prompt as the base.
    """
    webhook = MagicMock()
    webhook.name = "GitHub Push"
    webhook.prompt = "Analyze the push."
    webhook.payload_template = None

    agent = MagicMock()
    agent.entrypoint_prompt = None

    prompt = AgentWebhookService._assemble_session_prompt(
        webhook=webhook,
        agent=agent,
        payload_text='{"ref": "refs/heads/main"}',
        payload_content_type="application/json",
        headers_subset={"x-github-event": "push"},
    )

    assert "Analyze the push." in prompt
    assert '{"ref": "refs/heads/main"}' in prompt
    assert "x-github-event" in prompt
    assert "GitHub Push" in prompt


def test_session_prompt_truncated_when_too_large() -> None:
    """
    _assemble_session_prompt truncates the combined prompt at 20,000 chars and
    appends a [truncated] marker.
    """
    webhook = MagicMock()
    webhook.name = "Big Payload"
    webhook.prompt = "Base prompt."
    webhook.payload_template = None

    agent = MagicMock()
    agent.entrypoint_prompt = None

    # 30 KB payload — well above the 20,000 char cap
    large_payload = "x" * 30_000

    prompt = AgentWebhookService._assemble_session_prompt(
        webhook=webhook,
        agent=agent,
        payload_text=large_payload,
        payload_content_type="text/plain",
        headers_subset={},
    )

    assert len(prompt) <= 20_000
    assert prompt.endswith("[truncated]")


def test_session_prompt_uses_agent_entrypoint_prompt_as_fallback() -> None:
    """When webhook.prompt is None, falls back to agent.entrypoint_prompt."""
    webhook = MagicMock()
    webhook.name = "Fallback Test"
    webhook.prompt = None
    webhook.payload_template = None

    agent = MagicMock()
    agent.entrypoint_prompt = "You are a helpful code reviewer."

    prompt = AgentWebhookService._assemble_session_prompt(
        webhook=webhook,
        agent=agent,
        payload_text="some payload",
        payload_content_type="text/plain",
        headers_subset={},
    )

    assert "You are a helpful code reviewer." in prompt


def test_session_prompt_uses_default_when_both_prompts_none() -> None:
    """
    When both webhook.prompt and agent.entrypoint_prompt are None, the default
    string 'Start webhook-triggered execution.' is used.
    """
    webhook = MagicMock()
    webhook.name = "Default Prompt Test"
    webhook.prompt = None
    webhook.payload_template = None

    agent = MagicMock()
    agent.entrypoint_prompt = None

    prompt = AgentWebhookService._assemble_session_prompt(
        webhook=webhook,
        agent=agent,
        payload_text=None,
        payload_content_type=None,
        headers_subset={},
    )

    assert "Start webhook-triggered execution." in prompt
