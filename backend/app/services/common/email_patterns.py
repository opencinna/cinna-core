"""Shared email-pattern matching for sender allowlists.

Written when two features gated inbound senders on an admin-authored pattern
list — the per-agent email integration's ``auto_approve_email_pattern`` and
the server channels' ``email_whitelist``. The email integration is deleted
(``auto_approve_email_pattern`` survives only in alembic history), so today
there is exactly one consumer: ``ServerChannel.email_whitelist``, matched by
``ChannelInboundService``. The module stays separate because the semantics
below are a **fail-closed security contract** worth stating in one place
rather than inlining into the inbound pipeline — and because the next
transport that needs a sender allowlist must get the identical answer for
``*@example.com, devops.*@support.com``.

Semantics:
- Comma-separated list of ``fnmatch`` globs (``*``, ``?``, ``[seq]``).
- Case-insensitive on both sides.
- Blank entries are ignored (trailing commas are harmless).
- **Fails closed**: an empty / whitespace-only / ``None`` pattern string
  matches nothing. Callers that want "allow everyone" must say so with ``*``.
"""
import fnmatch


def match_email_pattern(email: str | None, pattern_string: str | None) -> bool:
    """Return True when ``email`` matches any glob in ``pattern_string``.

    Args:
        email: The sender address to test.
        pattern_string: Comma-separated fnmatch globs.

    Returns:
        False when either argument is empty — the allowlist fails closed.
    """
    if not email or not pattern_string:
        return False

    # The address is lowercased but NOT stripped — a pattern list is an
    # allowlist, so widening what counts as a match is never the safe default.
    candidate = email.lower()
    patterns = [p.strip().lower() for p in pattern_string.split(",") if p.strip()]
    return any(fnmatch.fnmatch(candidate, pattern) for pattern in patterns)
