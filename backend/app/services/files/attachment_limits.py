"""Shared size / MIME / quota policy for attachment bytes.

Two directions materialise bytes into ``FileUpload`` rows and both have to
answer the same four questions before they do: is this type allowed, is the
file small enough, does the message as a whole stay under its aggregate cap,
and does the owner still have storage left.

* **Agent → platform** — ``AttachmentMaterializationService`` pulls a file the
  agent declared with ``<cinna_attach>`` out of its workspace.
* **Channel → platform** — ``ChannelAttachmentService`` materialises a file a
  person sent through a Server Channel (Google Chat, email).

The policy lives here rather than in either of them because two copies of a
size-and-MIME rule *drift*, and the drift is invisible until someone's file is
accepted on one path and refused on the other. The two callers keep their own
*constants* — a channel attachment is capped well below a web upload (§4.3 of
the channel-attachments plan) and the agent path keeps its own per-message
aggregate — but they share the one function that applies them.

**Why a reason code rather than a raised exception.** A rejected attachment is
a per-file skip, not a failed message: the rest of the message still has to
reach the agent. Returning ``None`` for "accepted" and a short code otherwise
is what makes that natural at both call sites, and it was the existing
convention before this module existed.

**Why a code rather than a sentence.** The two callers render the same
rejection to different audiences: the agent path composes an
``attachment_error`` notice for the platform user who owns the session, while
the channel path names the reason in an *external sender's* transcript and in
the admin debug feed, which distinguishes refused-by-validation from
failed-to-fetch by exact token. A code is the value both can render; a
sentence would force one of them to parse the other's prose.
"""
from app.core.config import settings
from app.services.files.file_service import _is_mime_type_allowed

# Rejection codes. Stable tokens — they are matched on (the debug feed groups
# validation failures apart from fetch failures) and rendered into
# sender-visible text, so they are part of this module's contract.
REASON_TYPE_NOT_ALLOWED = "type_not_allowed"
REASON_TOO_LARGE = "too_large"
REASON_AGGREGATE_LIMIT = "aggregate_limit"
REASON_QUOTA_EXCEEDED = "quota_exceeded"


def validate_attachment_bytes(
    *,
    size: int,
    mime_type: str,
    max_file_bytes: int,
    aggregate_so_far: int,
    max_aggregate_bytes: int,
    running_usage: int,
    max_user_storage_bytes: int,
) -> str | None:
    """Apply the four attachment limits, in order.

    Args:
        size: Actual byte length of the content. Never a declared size.
        mime_type: The **resolved** MIME type — what the caller decided the
            file is, not what the sender or the agent claimed it was.
        max_file_bytes: Per-file cap.
        aggregate_so_far: Bytes already accepted for *this* message.
        max_aggregate_bytes: Per-message aggregate cap.
        running_usage: The owner's storage usage, including everything already
            accepted for this message — so ten files cannot each individually
            fit under a quota they jointly blow.
        max_user_storage_bytes: The owner's storage quota.

    Returns:
        A rejection code (one of the ``REASON_*`` constants), or ``None`` when
        the attachment is accepted.

    The MIME allowlist is **not** a parameter: ``settings.allowed_mime_types``
    is deployment-wide and identical on both paths (§4.3), so passing it would
    only create a way for the two callers to disagree about it.

    The order is deliberate and is the order the agent path has always used:
    type before size, size before the message aggregate, aggregate before the
    owner's quota. It goes cheapest-and-most-specific first, so the reason the
    caller reports is the one that is most useful to act on.
    """
    if not _is_mime_type_allowed(mime_type, settings.allowed_mime_types):
        return REASON_TYPE_NOT_ALLOWED

    if size > max_file_bytes:
        return REASON_TOO_LARGE

    if aggregate_so_far + size > max_aggregate_bytes:
        return REASON_AGGREGATE_LIMIT

    if running_usage + size > max_user_storage_bytes:
        return REASON_QUOTA_EXCEEDED

    return None
