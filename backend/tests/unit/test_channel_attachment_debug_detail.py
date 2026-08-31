"""Unit tests for ``channel_inbound_service._attachment_detail`` — the wire
format between the inbound pipeline and the admin debug panel for a
message's attachment outcome.

Why this needs its own test: ``ChannelDebugEvent.detail`` is typed
``dict[str, str]`` (plan §6.3 / the function's own docstring — widening it
would ripple into the generated OpenAPI client and cost the feature its "no
client regeneration" property), so the skip list is flattened into one
``"name (code); name (code)"`` line and capped at
``_MAX_SKIP_DETAIL_CHARS`` characters with a trailing ``"…"``. The frontend
panel parses that string back apart. There is no frontend test harness in
this repo to pin its half of the contract, so this file pins the backend
half: the separator, the per-entry shape, the cap, and — the part that is
easy to get wrong — that the cap is **not entry-aware**. A cut can land
mid-entry, leaving a trailing fragment with no closing ``(code)``, and that
was a real defect found only because two phases of this feature met at that
exact string boundary. ``attachments_skipped`` must stay the TRUE total
throughout, since it is the only authoritative count once the list itself
has been cut short.

Pure, I/O-free, no DB — belongs in ``tests/unit/`` (see "Cross-reference
convention" in ``tests/README.md``); the end-to-end shape of a debug record
for an accepted/skipped attachment is exercised through the real pipeline in
``tests/api/server_channels/server_channels_attachments_test.py``.
"""
from app.services.server_channels.channel_attachment_service import SkippedAttachment
from app.services.server_channels.channel_inbound_service import (
    _MAX_SKIP_DETAIL_CHARS,
    _attachment_detail,
)


def test_no_skips_omits_the_attachment_skips_key_entirely() -> None:
    """A message that carried files successfully must not show a bare
    ``attachment_skips=`` line in the panel."""
    detail = _attachment_detail(accepted=3, skipped=[])
    assert detail == {"attachments_accepted": "3", "attachments_skipped": "0"}
    assert "attachment_skips" not in detail


def test_skips_render_as_semicolon_separated_name_paren_code_entries() -> None:
    skipped = [
        SkippedAttachment("report.pdf", "too_large"),
        SkippedAttachment("logo.png", "type_not_allowed"),
    ]
    detail = _attachment_detail(accepted=1, skipped=skipped)
    assert detail["attachments_accepted"] == "1"
    assert detail["attachments_skipped"] == "2"
    assert detail["attachment_skips"] == "report.pdf (too_large); logo.png (type_not_allowed)"


def test_attachment_skips_truncate_at_the_cap_with_a_trailing_ellipsis_and_the_count_stays_authoritative() -> None:
    """
    Forces truncation with enough entries, and pins the exact contract:

      - the rendered line is cut at ``_MAX_SKIP_DETAIL_CHARS`` characters
        total, the last of which is the ``"…"`` marker;
      - ``attachments_skipped`` keeps naming the TRUE count, unaffected by
        the cut;
      - the cut is NOT entry-aware — it can (and here, does) land inside an
        entry rather than on an ``"; "`` boundary, leaving a trailing
        fragment with no closing ``)``. This is the exact shape the frontend
        must tolerate; a "fix" that made this entry-aware would be a
        backend/frontend contract change, not a bug fix, and this test is
        what would need to change first.
    """
    skipped = [SkippedAttachment(str(i), "too_large") for i in range(60)]
    raw = "; ".join(f"{item.filename} ({item.reason})" for item in skipped)
    assert len(raw) > _MAX_SKIP_DETAIL_CHARS, (
        "precondition: the fixture must actually force truncation"
    )

    detail = _attachment_detail(accepted=0, skipped=skipped)

    assert detail["attachments_skipped"] == str(len(skipped)), (
        "the count must stay the TRUE total even when the rendered list "
        "below it has been cut short"
    )
    rendered = detail["attachment_skips"]
    assert len(rendered) == _MAX_SKIP_DETAIL_CHARS
    assert rendered.endswith("…")
    assert rendered == raw[: _MAX_SKIP_DETAIL_CHARS - 1] + "…"

    body_before_ellipsis = rendered[:-1]
    assert not body_before_ellipsis.endswith(")"), (
        "fixture drifted: this construction is meant to cut mid-entry so the "
        "not-entry-aware property is actually exercised, but the truncation "
        "landed exactly on an entry boundary instead — adjust the fixture"
    )
