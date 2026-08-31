"""SecretScrubber — mask an install's credential values out of snapshot text.

Agents do occasionally echo a token into chat. Without this pass a consumer's
own API key could ride into a bundle publisher's improvement archive, which is
exactly the leak this feature must not create.

Design notes:

* ``scrub`` is a **pure function** — ``(payload, secrets) -> (payload, hits)``.
  No DB access, no logging of values. The DB read that produces the secret set
  lives in ``ImprovementRequestService``; the collection helpers here operate on
  already-decrypted dicts, so the whole module is exhaustively unit-testable.
  This follows the ``assert_url_allowed`` / ``assert_api_proxy_allowed``
  chokepoint precedent.
* The sensitive-field map is **reused** from
  :attr:`CredentialsService.SENSITIVE_FIELDS`, never duplicated — a new
  credential type gets scrubbed here the moment it is redacted there.

See ``docs/plans/agent_improvement_requests_plan.md`` §4.2.
"""
from typing import Any

from app.services.credentials.credentials_service import CredentialsService

# Replacement written in place of every matched secret.
REDACTION_PLACEHOLDER = "***REDACTED***"

# Values shorter than this are discarded from the secret set. A 4-character
# "password" would shred ordinary prose; a real secret is never this short.
MIN_SECRET_LENGTH = 8

# The snapshot string fields that carry free text an agent could have echoed a
# secret into. Structural fields (ids, names, timestamps, mime types) are not
# rewritten — a false positive there would corrupt the archive's metadata.
# ``title`` is included beyond the three fields the plan names: a session title
# is auto-generated from the first user message, so it is derived free text on
# the same footing as ``content``, and it is display-only (never an identifier).
# ``text`` is the free-text field of the ``context.prompts`` and
# ``context.memory`` entries. It is the reason the scrubber now runs over the
# *context* block too and not just the transcript: a prompt document routinely
# carries endpoints and occasionally a pasted key, and a memory note is
# whatever the user asked the agent to remember.
SCRUBBED_KEYS: frozenset[str] = frozenset(
    {"content", "brief", "result_summary", "title", "text"}
)


def collect_credential_secrets(credentials: list[dict]) -> set[str]:
    """Extract secret values from decrypted credential dicts.

    Args:
        credentials: rows as produced by
            ``CredentialsService.get_agent_credentials_with_data`` —
            ``{"type": str, "credential_data": dict, ...}``.

    Returns:
        The set of candidate secret strings (unfiltered by length; ``scrub``
        applies the length floor).
    """
    secrets: set[str] = set()
    for cred in credentials:
        sensitive_fields = CredentialsService.SENSITIVE_FIELDS.get(
            cred.get("type") or "", []
        )
        data = cred.get("credential_data") or {}
        for field in sensitive_fields:
            value = data.get(field)
            if isinstance(value, str) and value.strip():
                secrets.add(value.strip())
    return secrets


def scrub(payload: dict, secrets: set[str]) -> tuple[dict, int]:
    """Replace every occurrence of a secret in the payload's text fields.

    Walks ``payload`` recursively and rewrites any string living under a key in
    :data:`SCRUBBED_KEYS`. Secrets are applied longest-first so a longer token
    is masked before a shorter value that happens to be its prefix.

    Args:
        payload: the snapshot dict. Left untouched — a rewritten copy is
            returned.
        secrets: candidate secret values. Entries shorter than
            :data:`MIN_SECRET_LENGTH` are ignored.

    Returns:
        ``(payload, hits)`` where ``hits`` is the total number of replaced
        occurrences — recorded as ``context.platform.scrubbed_hits`` for
        observability. The values themselves are never logged or returned.
    """
    ordered = sorted(
        (s for s in secrets if len(s) >= MIN_SECRET_LENGTH),
        key=len,
        reverse=True,
    )
    if not ordered:
        return payload, 0

    hits = 0

    def _scrub_text(text: str) -> str:
        nonlocal hits
        for secret in ordered:
            occurrences = text.count(secret)
            if occurrences:
                hits += occurrences
                text = text.replace(secret, REDACTION_PLACEHOLDER)
        return text

    def _walk(node: Any, key: str | None = None) -> Any:
        if isinstance(node, dict):
            return {k: _walk(v, k) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(item, key) for item in node]
        if isinstance(node, str) and key in SCRUBBED_KEYS:
            return _scrub_text(node)
        return node

    scrubbed = _walk(payload)
    return scrubbed, hits
