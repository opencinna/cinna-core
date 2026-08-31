"""Turn inbound channel attachment refs into platform ``FileUpload`` rows.

A person talking to an agent through a Server Channel (Google Chat, polled
email) can attach files. The adapter reports what arrived as
``ChannelAttachmentRef``s — bytes in hand for email, an opaque media handle for
Chat — and this service is the single place those become durable uploads owned
by the **sender**, ready to be handed to the session pipeline as ordinary
``file_ids``. From that point on a channel attachment is indistinguishable from
a web upload: same table, same lifecycle, same garbage collection, same
rendering.

**Position in the pipeline is load-bearing.** ``materialize`` is step 6.5 —
after the rate limit, the signature/poll verification, the redelivery dedup,
the whitelist, user resolution and the channel policy gate. Nothing here
re-authenticates and nothing here widens what an earlier step decided: by the
time it runs, the sender is admitted, a ``FileUpload.user_id`` exists to write,
and there is a quota to charge. Moving this call earlier would let an
unadmitted sender spend the deployment's disk.

**Nothing raises out of this method.** A fetch error, a validation rejection,
a disk error — each is one skipped file, never a failed message. A message
whose *text* was fine must still reach the agent; the sender is told what was
dropped, in the transcript, by the caller.

Deliberately a sibling of ``AttachmentMaterializationService`` rather than a
reuse of it: that one is ``<cinna_attach>``-specific (it reads an agent
workspace through an env adapter and writes ``source="agent_attachment"``
junction rows) and folding two directions into one function would make both
harder to read. The part that genuinely must not drift — the size / MIME /
quota policy — *is* shared, via ``services/files/attachment_limits.py``.
"""
from __future__ import annotations

import functools
import logging
import mimetypes
import re
import uuid
from dataclasses import dataclass, field

import anyio
import anyio.to_thread
from sqlmodel import Session as DBSession
from sqlmodel import col, select

from app.core.config import settings
from app.models import ServerChannel
from app.models.files.file_upload import FileUpload
from app.services.files.attachment_limits import (
    REASON_AGGREGATE_LIMIT,
    REASON_TYPE_NOT_ALLOWED,
    validate_attachment_bytes,
)
from app.services.files.file_service import FileService
from app.services.files.file_storage_service import FileStorageService
from app.services.server_channels.adapters.base import (
    ChannelAdapter,
    ChannelAttachmentRef,
    ChannelAttachmentUnavailable,
    ChannelInboundMessage,
)

logger = logging.getLogger(__name__)

# Skip reasons owned by this module. The validation reasons
# (``type_not_allowed``, ``too_large``, ``aggregate_limit``,
# ``quota_exceeded``) come back from ``validate_attachment_bytes``; the fetch
# reasons (``not_found``, ``forbidden``, ``timeout``, ``drive_file``) come from
# the adapter. Together they are the vocabulary the transcript note and the
# admin debug feed render, and the feed groups them into the two families that
# imply different next actions: refused by validation is the sender's to fix,
# failed to fetch is the operator's.
REASON_TOO_MANY_ATTACHMENTS = "too_many_attachments"
REASON_NO_CONTENT = "no_content"
REASON_TIMEOUT = "timeout"
REASON_UPSTREAM_ERROR = "upstream_error"
REASON_STORAGE_ERROR = "storage_error"
# The whole-message fetch budget ran out while this attachment was still
# *queued* behind the concurrency cap — no request was ever issued for it.
# Deliberately distinct from ``timeout``: the two look identical in a skip list
# and send an operator to completely different places. ``timeout`` says the
# upstream was slow; this one says the message brought more files than the
# budget could work through, and the fix is the concurrency cap or the budget,
# not Google's latency.
REASON_FETCH_BUDGET_EXHAUSTED = "fetch_budget_exhausted"

# Last-resort display name for a ref whose filename is empty or sanitises away
# to nothing. Never a path, never sender-controlled.
_FALLBACK_FILENAME = "attachment"

# Provenance keys this module both writes (``_provenance``) and reads back
# (``_existing_materializations``). Named rather than spelled twice: the write
# and the read are the two halves of the idempotency key, and a typo in either
# half degrades silently into "materialise it again".
_PROV_SOURCE = "source"
_PROV_SOURCE_VALUE = "server_channel"
_PROV_CHANNEL_ID = "server_channel_id"
_PROV_THREAD_KEY = "thread_key"
_PROV_EXTERNAL_MESSAGE_ID = "external_message_id"
#: Position of the attachment within its message. The fourth term of the key,
#: and the one that makes reuse *per attachment* rather than all-or-nothing —
#: so a retry after a partial store finishes the job instead of stranding the
#: rest. Written as a string because the whole block is stringly typed.
_PROV_INDEX = "attachment_index"

# ``file_uploads.mime_type`` is ``VARCHAR(127)``. A declared type is
# sender-controlled and SQLModel does not validate ``table=True`` instances on
# construction, so an over-long value would survive the allowlist (``text/*``
# matches on prefix), reach ``db.commit()`` as a StringDataRightTruncation, and
# take down *every* accepted attachment in the message through the
# commit-failure branch. Bounded and shape-checked here instead.
_MIME_MAX_LENGTH = 127
_MIME_SHAPE = re.compile(r"^[a-z0-9][a-z0-9!#$&^_.+-]*/[a-z0-9][a-z0-9!#$&^_.+-]*$")


@dataclass(frozen=True)
class SkippedAttachment:
    """One attachment that did not become a file, and why.

    ``filename`` is always the **sanitised** name: this value is rendered into
    the session transcript and into the admin debug feed, and it is
    sender-supplied text.
    """

    filename: str
    reason: str


@dataclass(frozen=True)
class ChannelAttachmentResult:
    """What one message's attachments became.

    ``file_ids`` is what rides beside ``text`` down the rest of the pipeline;
    ``skipped`` is what the sender has to be told about. An empty ``file_ids``
    with a non-empty ``skipped`` is the attachment-only rejection case the
    caller answers explicitly — see §5.4 of the plan.

    ``accepted_filenames`` is parallel to ``file_ids`` (same order, same
    length) and carries the **sanitised** display names. It exists for exactly
    one consumer: the pipeline builds an attachment-only message's classifier
    input from it (``"(sent 3 files: a.pdf, b.png, c.csv)"``, §5.4). Returning
    the names this method already computed is what lets that caller avoid
    re-reading the rows it just wrote — a ``SELECT`` on the synchronous webhook
    path, purely to recover a value that was in hand a moment earlier.

    It is display text, never a path and never a key: the ids are the
    identity. A consumer that finds itself matching on a name wants
    ``file_ids`` instead.

    ``reused_file_ids`` is the subset of ``file_ids`` this call did **not**
    create — rows an earlier delivery of this same ``(binding,
    external_message_id, position)`` already materialised, found by the
    idempotency lookup and handed back rather than fetched and stored again.

    It exists because reuse collides with a rule one layer down:
    ``MessageService.prepare_user_message_with_files`` refuses any file whose
    ``status`` is not ``"temporary"``, and the first delivery flipped these to
    ``"attached"``. Both rules are right — reuse must not re-charge the
    sender's quota or re-fetch from Google, and a file must not drift across
    unrelated messages — so the collision is resolved by naming the exact ids
    that are a redelivery of a message that already owns them, and exempting
    only those. A caller that ignores this field gets exactly the old
    behaviour; a caller that widens it into "be lenient about statuses" has
    misread it.
    """

    file_ids: list[uuid.UUID] = field(default_factory=list)
    skipped: list[SkippedAttachment] = field(default_factory=list)
    accepted_filenames: list[str] = field(default_factory=list)
    reused_file_ids: list[uuid.UUID] = field(default_factory=list)


class ChannelAttachmentService:
    """Materialise inbound channel attachments. Stateless; one public method."""

    @staticmethod
    async def materialize(
        *,
        db: DBSession,
        channel: ServerChannel,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        owner_id: uuid.UUID,
    ) -> ChannelAttachmentResult:
        """Turn ``inbound.attachments`` into ``FileUpload`` rows owned by ``owner_id``.

        Args:
            db: Database session. Committed once, at the end, for all accepted
                rows.
            channel: The channel the message arrived on. Used for the
                provenance block and handed to the adapter's fetch.
            adapter: The transport's adapter. Its ``capabilities`` decide
                whether attachments are considered at all, and its
                ``fetch_attachment`` resolves handles into bytes.
            inbound: The normalised inbound message. **Never mutated.**
            owner_id: The resolved *sender's* platform user id — not the
                session owner. On an identity-routed thread those differ, and
                owning the file as the sender is what keeps provenance honest
                and charges the right person's quota (§3.4).

        Returns:
            A ``ChannelAttachmentResult``. Never raises.
        """
        # Owned here rather than inside ``_materialize`` so the backstop below
        # can still unlink what was written when the failure happened *after*
        # some bytes hit disk. Nothing else reclaims them: the GC sweeps
        # ``FileUpload`` rows, and a rolled-back row names nothing.
        stored_paths: list[str] = []

        # The whole body, not just the delegation: "never raises" has to be
        # true by construction, not by the reader's confidence that the two
        # attribute reads above cannot throw.
        try:
            if not inbound.attachments:
                return ChannelAttachmentResult()

            if not adapter.capabilities.supports_inbound_attachments:
                # A declaration and a behaviour that disagree is a bug, and the
                # declaration is the safe reading — so the refs are dropped
                # rather than trusted. Named loudly; nothing else will notice.
                logger.warning(
                    "Adapter %s returned %d attachment ref(s) but declares "
                    "supports_inbound_attachments=False; ignoring them "
                    "(channel=%s)",
                    type(adapter).__name__,
                    len(inbound.attachments),
                    channel.id,
                )
                return ChannelAttachmentResult()

            return await ChannelAttachmentService._materialize(
                db=db,
                channel=channel,
                adapter=adapter,
                inbound=inbound,
                owner_id=owner_id,
                stored_paths=stored_paths,
            )
        except Exception:  # pragma: no cover - total backstop, see module docs
            logger.error(
                "Channel attachment materialisation failed outright "
                "(channel=%s owner=%s); delivering the message without files",
                getattr(channel, "id", None),
                owner_id,
                exc_info=True,
            )
            try:
                db.rollback()
            except Exception:
                logger.debug("Rollback after materialisation failure failed")
            _unlink_all(stored_paths)
            try:
                return ChannelAttachmentResult(
                    skipped=[
                        SkippedAttachment(
                            _safe_display_name(ref.filename), REASON_STORAGE_ERROR
                        )
                        for ref in inbound.attachments
                    ]
                )
            except Exception:
                return ChannelAttachmentResult()

    # ------------------------------------------------------------------
    # Implementation
    # ------------------------------------------------------------------

    @staticmethod
    async def _materialize(
        *,
        db: DBSession,
        channel: ServerChannel,
        adapter: ChannelAdapter,
        inbound: ChannelInboundMessage,
        owner_id: uuid.UUID,
        stored_paths: list[str],
    ) -> ChannelAttachmentResult:
        refs = list(inbound.attachments)
        max_per_message = settings.CHANNEL_ATTACHMENT_MAX_PER_MESSAGE

        # Count cap first, so the excess is never *fetched*. A cap enforced
        # after the download has already spent the bandwidth it was meant to
        # save.
        over_cap = refs[max_per_message:]
        refs = refs[:max_per_message]
        skipped: list[SkippedAttachment] = [
            SkippedAttachment(
                _safe_display_name(ref.filename), REASON_TOO_MANY_ATTACHMENTS
            )
            for ref in over_cap
        ]
        if over_cap:
            logger.info(
                "Channel message carried %d attachments; %d over the per-message "
                "cap of %d were skipped without being fetched (channel=%s)",
                len(over_cap) + len(refs),
                len(over_cap),
                max_per_message,
                channel.id,
            )

        # Force the channel's columns to load on THIS side of the concurrent
        # fetch group. The pipeline commits eagerly and ``expire_on_commit`` is
        # SQLAlchemy's default True, so ``channel`` may well be expired here —
        # and the first attribute an adapter touches inside the group would
        # then be a synchronous re-SELECT on the shared session, blocking the
        # event loop and defeating the deadline it is supposed to be running
        # under. One attribute read reloads every column attribute in a single
        # statement, which is exactly what makes ``fetch_attachment``'s "must
        # not need a database session" contract true rather than aspirational.
        _ = channel.channel_type
        # And the id off the same freshly-loaded instance, for the log lines
        # below the commit/rollback at the end of this method. Both expire the
        # instance, so ``channel.id`` there is a reload — and the one in the
        # commit-failure handler would replace the exception being reported
        # with an ``ObjectDeletedError`` from an argument expression the
        # logging call cannot guard.
        channel_id = channel.id

        # **Idempotency.** A webhook redelivery on an already-bound thread
        # reaches this method a second time: ``binding.last_external_message_id``
        # is stamped only after a *successful* ingest, deliberately, so that a
        # redelivery of a message the platform failed to process stays a
        # recovery opportunity. Without this lookup that recovery would also
        # re-store every file and charge the sender's quota twice.
        #
        # Keyed on ``(binding, external_message_id, position)`` — the binding
        # being ``(server_channel_id, thread_key)``, which is what the
        # provenance block already records. Rows found here are **reused**: no
        # fetch, no disk write, no second row, no second charge. Refs with no
        # row are materialised normally, so a retry after a partial store
        # finishes the job and a previously-*skipped* attachment is retried and
        # named again in the sender's notice.
        existing = _existing_materializations(
            db=db, channel_id=channel_id, inbound=inbound, owner_id=owner_id
        )

        resolved = await ChannelAttachmentService._resolve_bytes(
            channel=channel,
            adapter=adapter,
            refs=refs,
            # Nothing is fetched for a ref that already has a file. On Chat
            # that is the whole point: the retry costs no outbound request.
            skip_indices=set(existing),
        )

        running_usage = FileService.get_user_storage_usage(
            session=db, user_id=owner_id
        )
        aggregate_bytes = 0
        # ``(file_id, sanitised filename, is_new)`` in ref order. The flag
        # matters only in the commit-failure branch: a reused row is already
        # committed and survives the rollback, so it must not be reported to
        # the sender as a storage failure or dropped from ``file_ids``.
        accepted: list[tuple[uuid.UUID, str, bool]] = []

        for index, ref in enumerate(refs):
            reused = existing.get(index)
            if reused is not None:
                reused_id, reused_name, reused_size = reused
                accepted.append((reused_id, reused_name, False))
                # Charged against the per-message aggregate exactly as it was
                # the first time, so the cap behaves identically on a retry.
                # NOT added to ``running_usage``: those bytes are already in
                # the quota total this method read out of the database.
                aggregate_bytes += reused_size
                logger.info(
                    "Reusing already-materialised attachment %s ('%s') for a "
                    "redelivery of message %s on channel %s",
                    reused_id,
                    reused_name,
                    inbound.external_message_id,
                    channel_id,
                )
                continue

            # ``pop``, not ``[]``. Every accepted blob is otherwise held by
            # ``resolved`` for the whole of this loop, so peak memory stays at
            # its maximum right through the disk writes instead of tapering as
            # the bytes land. Dropping the reference here lets each one be
            # collected as soon as ``store_file`` has written it.
            #
            # ``inbound.attachments`` still holds email's own bytes — this
            # method must not mutate ``inbound`` (§5.3 rule 6) — so on that
            # transport this taper is bounded by what the poll tick is already
            # carrying. It is Chat's fetched bodies, which nothing else
            # references, that this actually releases.
            content, fetch_reason = resolved.pop(index, (None, None))
            filename = _safe_display_name(ref.filename)

            if content is None:
                skipped.append(
                    SkippedAttachment(filename, fetch_reason or REASON_NO_CONTENT)
                )
                continue

            mime_type = _resolve_mime_type(ref.mime_type, filename)
            if mime_type is None:
                # Nothing declared and nothing derivable from the extension.
                # Rejected rather than defaulted to octet-stream, which would
                # smuggle an unknown type past an allowlist that happens to
                # contain it.
                skipped.append(SkippedAttachment(filename, REASON_TYPE_NOT_ALLOWED))
                continue

            size = len(content)
            reject = validate_attachment_bytes(
                size=size,
                mime_type=mime_type,
                max_file_bytes=settings.channel_attachment_max_file_bytes,
                aggregate_so_far=aggregate_bytes,
                max_aggregate_bytes=settings.channel_attachment_max_aggregate_bytes,
                running_usage=running_usage,
                max_user_storage_bytes=settings.upload_max_user_storage_bytes,
            )
            if reject is not None:
                logger.info(
                    "Channel attachment rejected (%s): filename=%s mime=%s size=%d "
                    "channel=%s",
                    reject,
                    filename,
                    mime_type,
                    size,
                    channel.id,
                )
                skipped.append(SkippedAttachment(filename, reject))
                continue

            file_id = uuid.uuid4()
            try:
                # **On a worker thread, and not for this request's latency.**
                # ``store_file`` is a synchronous disk write of up to the
                # per-message aggregate (50MB by default, across ten files),
                # and this coroutine runs on the shared event loop of a
                # uvicorn worker. A blocking write here does not slow *this*
                # webhook down — it stops every other request that worker is
                # serving for as long as it takes, which is the cost that
                # matters and the reason this is not a performance nicety to
                # be traded away. Same treatment ``poll`` gives blocking
                # imaplib and ``google_chat`` gives ``getaddrinfo``.
                file_path = await anyio.to_thread.run_sync(
                    functools.partial(
                        FileStorageService.store_file,
                        user_id=str(owner_id),
                        file_id=str(file_id),
                        filename=filename,
                        content=content,
                    )
                )
            except Exception:
                logger.error(
                    "Failed to store channel attachment filename=%s size=%d "
                    "channel=%s",
                    filename,
                    size,
                    channel.id,
                    exc_info=True,
                )
                skipped.append(SkippedAttachment(filename, REASON_STORAGE_ERROR))
                continue

            # The bytes are on disk; nothing below needs them. Released before
            # the row is built rather than at the end of the iteration, so the
            # next fetch's blob is the only large object alive.
            del content

            db.add(
                FileUpload(
                    id=file_id,
                    user_id=owner_id,
                    filename=filename,
                    file_path=file_path,
                    file_size=size,
                    mime_type=mime_type,
                    # A person sent it, and it has not been attached to a
                    # message yet — exactly a web upload's starting state, so
                    # the existing GC reclaims it if the message never lands.
                    origin="user",
                    status="temporary",
                    # NULL: that column is for agent *output* attachments.
                    session_id=None,
                    file_metadata=_provenance(
                        channel=channel, inbound=inbound, ref=ref, index=index
                    ),
                )
            )

            stored_paths.append(file_path)
            accepted.append((file_id, filename, True))
            running_usage += size
            aggregate_bytes += size

        if stored_paths:
            try:
                db.commit()
            except Exception:
                logger.error(
                    "Failed to commit %d channel attachment row(s) (channel=%s); "
                    "unlinking what was written",
                    len(stored_paths),
                    channel_id,
                    exc_info=True,
                )
                db.rollback()
                # Nothing else reclaims these: the GC sweeps ``FileUpload``
                # rows, and the rows that named them just rolled back. Only the
                # NEW rows are affected — a reused row was committed by an
                # earlier delivery and is still there, so it keeps its place in
                # the result instead of being mourned as a storage failure.
                _unlink_all(stored_paths)
                skipped.extend(
                    SkippedAttachment(name, REASON_STORAGE_ERROR)
                    for _file_id, name, is_new in accepted
                    if is_new
                )
                survivors = [
                    (file_id, name)
                    for file_id, name, is_new in accepted
                    if not is_new
                ]
                return ChannelAttachmentResult(
                    file_ids=[file_id for file_id, _name in survivors],
                    skipped=skipped,
                    accepted_filenames=[name for _file_id, name in survivors],
                    # Every survivor of a failed commit is by definition a
                    # reused row: the new ones were just rolled back.
                    reused_file_ids=[file_id for file_id, _name in survivors],
                )

            logger.info(
                "Materialised %d channel attachment(s) for owner=%s channel=%s "
                "(%d reused, %d skipped)",
                len(stored_paths),
                owner_id,
                channel_id,
                len(accepted) - len(stored_paths),
                len(skipped),
            )

        return ChannelAttachmentResult(
            file_ids=[file_id for file_id, _name, _is_new in accepted],
            skipped=skipped,
            accepted_filenames=[name for _file_id, name, _is_new in accepted],
            # The ``is_new`` flag, already tracked for the commit-failure
            # branch, doing its second job: naming the rows a *previous*
            # delivery of this message created. Those are ``"attached"`` by
            # now, and the session pipeline refuses an attached file unless the
            # caller names it — see ``ChannelAttachmentResult``.
            reused_file_ids=[
                file_id for file_id, _name, is_new in accepted if not is_new
            ],
        )

    # ------------------------------------------------------------------
    # Byte resolution
    # ------------------------------------------------------------------

    @staticmethod
    async def _resolve_bytes(
        *,
        channel: ServerChannel,
        adapter: ChannelAdapter,
        refs: list[ChannelAttachmentRef],
        skip_indices: set[int] | None = None,
    ) -> dict[int, tuple[bytes | None, str | None]]:
        """Get the bytes for each ref, indexed by its position in ``refs``.

        Refs that already carry ``content`` (email) and refs the adapter has
        already declared unobtainable (a Drive file) resolve without any I/O.
        The rest are fetched **concurrently**, under one shared deadline.

        ``skip_indices`` are positions the caller has already satisfied from an
        earlier delivery of the same message. They are absent from the result
        entirely — no bytes are resolved for them, and on a fetch transport no
        request is issued — which is what keeps a redelivery from re-spending
        the bandwidth as well as the quota.

        The concurrency is not about throughput. For a webhook transport this
        runs inside the synchronous request the platform has to ack, so the
        worst case that matters is wall-clock: ten sequential fetches would
        cost ten timeouts, whereas one task group under one
        ``move_on_after`` costs one. Whatever landed before the deadline is
        kept — this is ``move_on_after``, not ``fail_after``, precisely so a
        slow tenth attachment does not throw away nine good ones.

        **``CHANNEL_ATTACHMENT_FETCH_TIMEOUT_SECONDS`` is spent twice, and the
        second spend is the one that decides the outcome.** The adapter uses it
        as the per-request HTTP timeout; *this* method uses the same number as
        the deadline for the **whole message's** fetch phase. So the real bound
        on a message is one timeout, not one per attachment — better than the
        plan's ``TIMEOUT × MAX_PER_MESSAGE`` worst case, and deliberately so:
        step 6.5 sits inside a webhook Google expects acked in about thirty
        seconds, and a budget that scaled with the attachment count would miss
        that window rather than drop a file.

        The cost of being stricter than advertised is the tail of a large
        legitimate message. With the default caps the limiter below admits two
        fetches at a time, so ten handles are five sequential rounds inside one
        30s budget; whatever has not finished is skipped. Those skips are named
        apart from real timeouts (``fetch_budget_exhausted`` for a task still
        queued and never issued, ``timeout`` for one that was in flight),
        because an operator's next move differs completely between the two —
        the first is a caps question, the second is Google's latency.
        """
        results: dict[int, tuple[bytes | None, str | None]] = {}
        pending: list[tuple[int, ChannelAttachmentRef]] = []
        # Bytes already resident because the transport handed them over. They
        # count against the same budget the fetches spend from — email's
        # attachments are just as much memory as Chat's.
        resident = 0

        skip = skip_indices or set()
        for index, ref in enumerate(refs):
            if index in skip:
                continue
            if ref.content is not None:
                results[index] = (ref.content, None)
                resident += len(ref.content)
            elif ref.unavailable_reason:
                results[index] = (None, ref.unavailable_reason)
            elif ref.handle:
                pending.append((index, ref))
            else:
                results[index] = (None, REASON_NO_CONTENT)

        if not pending:
            return results

        budget = settings.channel_attachment_max_aggregate_bytes
        ceiling = settings.channel_attachment_max_file_bytes
        # In-flight fetches are capped so peak memory stays proportional to the
        # per-message aggregate rather than to the *count* cap. Without this,
        # ten concurrent fetches of a 25MB-ceilinged body peak at 250MB for a
        # message whose advertised aggregate is 50MB.
        #
        # **The honest bound is ``budget + concurrency × ceiling``**, not
        # ``budget``: ``resident`` counts only what has already *arrived*, so a
        # task admitted at ``budget - 1`` can still be joined by ``concurrency``
        # bodies in flight. With the defaults that is 50MB + 2 × 25MB = 100MB
        # for one message, and there is no process-wide cap above it, so K
        # simultaneous webhook deliveries cost K times that. Stated rather than
        # tightened: reserving the ceiling up front instead would refuse the
        # third of ten *small* files on a message that fits the budget ten
        # times over, which is a worse failure than a documented constant
        # factor. Lowering ``CHANNEL_ATTACHMENT_MAX_AGGREGATE_MB`` moves both
        # halves of the bound together and is the lever that actually works.
        limiter = anyio.CapacityLimiter(max(1, min(4, budget // max(1, ceiling))))
        # Indices that got as far as issuing a request. The post-group sweep
        # reads this to tell "the upstream was slow" apart from "the deadline
        # passed while this was still queued behind the limiter".
        started: set[int] = set()

        async def _fetch_one(index: int, ref: ChannelAttachmentRef) -> None:
            nonlocal resident
            async with limiter:
                # Re-checked here, not before ``start_soon``: every task is
                # spawned at once, so the only place this can see what earlier
                # fetches actually cost is after acquiring the limiter.
                if resident >= budget:
                    results[index] = (None, REASON_AGGREGATE_LIMIT)
                    return
                started.add(index)
                try:
                    content = await adapter.fetch_attachment(channel, ref)
                except ChannelAttachmentUnavailable as e:
                    results[index] = (None, e.reason or REASON_NO_CONTENT)
                except Exception:
                    # Backstop. An adapter that lands here has lost its own
                    # reason text, which is why the contract says not to rely
                    # on it.
                    logger.warning(
                        "Adapter %s raised an unexpected error fetching an "
                        "attachment (channel=%s)",
                        type(adapter).__name__,
                        channel.id,
                        exc_info=True,
                    )
                    results[index] = (None, REASON_UPSTREAM_ERROR)
                else:
                    resident += len(content)
                    results[index] = (content, None)

        with anyio.move_on_after(settings.CHANNEL_ATTACHMENT_FETCH_TIMEOUT_SECONDS):
            async with anyio.create_task_group() as tg:
                for index, ref in pending:
                    tg.start_soon(_fetch_one, index, ref)

        for index, _ref in pending:
            if index in results:
                continue
            if index in started:
                logger.warning(
                    "Attachment fetch did not finish within the %ss "
                    "whole-message budget (channel=%s)",
                    settings.CHANNEL_ATTACHMENT_FETCH_TIMEOUT_SECONDS,
                    channel.id,
                )
                results[index] = (None, REASON_TIMEOUT)
            else:
                # Never issued a request: cancelled while still waiting on the
                # limiter. Naming this ``timeout`` would send whoever debugs it
                # to Google's latency when the answer is the concurrency cap
                # against the number of attachments this message brought.
                logger.warning(
                    "Attachment was still queued when the %ss whole-message "
                    "fetch budget ran out — no request was issued for it "
                    "(concurrency=%d, queued=%d, channel=%s)",
                    settings.CHANNEL_ATTACHMENT_FETCH_TIMEOUT_SECONDS,
                    limiter.total_tokens,
                    len(pending),
                    channel.id,
                )
                results[index] = (None, REASON_FETCH_BUDGET_EXHAUSTED)

        return results


# ----------------------------------------------------------------------
# Module-level helpers
# ----------------------------------------------------------------------


def _safe_display_name(raw: str | None) -> str:
    """Sanitise a transport-declared filename into something safe to show.

    Runs the same ``sanitize_filename`` the storage layer runs — path
    separators, ``..`` and control characters never reach the filesystem, a
    log line, or the transcript. A name that sanitises away entirely (or was
    never there) becomes a fixed placeholder rather than an empty string,
    which would render as a blank in the sender's skip notice.
    """
    cleaned = FileStorageService.sanitize_filename(raw or "").strip()
    # ``.`` / ``..`` survive the character filter but are not names.
    if not cleaned or cleaned.strip(".") == "":
        return _FALLBACK_FILENAME
    return cleaned


def _resolve_mime_type(declared: str | None, filename: str) -> str | None:
    """Decide what type a file actually is (§4.3 step 2).

    The transport-declared content type is attacker-influenced, so it is used
    only when it says something. When it is absent — or the universal
    "I don't know" of ``application/octet-stream`` — the type is re-derived
    from the *sanitised* extension, and the derived value is what the
    allowlist then sees.

    Returns ``None`` when nothing resolves. That is a rejection, not a reason
    to default to ``application/octet-stream``: defaulting would hand an
    unknown file whatever verdict the allowlist happens to have for that type.
    """
    value = (declared or "").split(";", 1)[0].strip().lower()
    if not value or value == "application/octet-stream":
        guessed, _ = mimetypes.guess_type(filename)
        value = (guessed or "").split(";", 1)[0].strip().lower()

    if not value or len(value) > _MIME_MAX_LENGTH or not _MIME_SHAPE.match(value):
        return None
    return value


def _unlink_all(paths: list[str]) -> None:
    """Best-effort removal of upload bytes no surviving row names."""
    for path in paths:
        try:
            FileStorageService.delete_file(path)
        except Exception:
            logger.debug("Could not unlink orphaned upload %s", path)


def _provenance(
    *,
    channel: ServerChannel,
    inbound: ChannelInboundMessage,
    ref: ChannelAttachmentRef,
    index: int,
) -> dict[str, str | None]:
    """The §3.3 provenance block, written into ``file_uploads.file_metadata``.

    An untyped JSON column that already exists — no schema change. It keeps
    what the transport *claimed* alongside what the platform decided, which is
    the only record of a mismatch once the file is on disk under its sanitised
    name.

    Four of these keys are also the **idempotency key** read back by
    :func:`_existing_materializations`: channel, thread, external message id
    and position. Provenance and identity are the same record here, which is
    why the block is written for every accepted file and not only when it is
    interesting to a human.
    """
    return {
        _PROV_SOURCE: _PROV_SOURCE_VALUE,
        _PROV_CHANNEL_ID: str(channel.id),
        "channel_type": channel.channel_type,
        _PROV_THREAD_KEY: inbound.thread_key,
        _PROV_EXTERNAL_MESSAGE_ID: inbound.external_message_id,
        _PROV_INDEX: str(index),
        "declared_mime_type": ref.mime_type,
        "declared_filename": ref.filename,
    }


def _existing_materializations(
    *,
    db: DBSession,
    channel_id: uuid.UUID,
    inbound: ChannelInboundMessage,
    owner_id: uuid.UUID,
) -> dict[int, tuple[uuid.UUID, str, int]]:
    """Files this exact message already produced, by attachment position.

    Returns ``{index: (file_id, filename, file_size)}``. Empty when there is
    nothing to reuse — and empty is always a *safe* answer: the caller simply
    materialises, which is what happened before this lookup existed.

    **The key is ``(binding, external_message_id, position)``.** The binding is
    ``(server_channel_id, thread_key)``; all four terms live in the provenance
    block :func:`_provenance` writes, so no column and no migration is
    involved. A message with no thread key or no external message id has no
    key at all and is not deduplicable — it is materialised every time, which
    is the pre-existing behaviour and is why the pipeline refuses to route a
    message with no thread key long before this runs.

    ``marked_for_deletion`` rows are excluded: those bytes are already promised
    back to the owner, and resurrecting one into a live message would put a
    file the GC is about to reclaim into an agent's workspace.

    **Never raises.** A failure here degrades to "no reuse" — a duplicate
    store, the behaviour this function exists to prevent — rather than to the
    materialiser's total backstop, which would turn a bad query into *every*
    attachment on the message being reported to the sender as a storage error.
    """
    external_id = (inbound.external_message_id or "").strip()
    thread_key = (inbound.thread_key or "").strip()
    if not external_id or not thread_key:
        return {}

    try:
        rows = db.exec(
            select(
                FileUpload.id,
                FileUpload.filename,
                FileUpload.file_size,
                FileUpload.file_metadata[_PROV_INDEX].as_string(),
            )
            .where(
                FileUpload.user_id == owner_id,
                FileUpload.status != "marked_for_deletion",
                FileUpload.file_metadata[_PROV_SOURCE].as_string()
                == _PROV_SOURCE_VALUE,
                FileUpload.file_metadata[_PROV_CHANNEL_ID].as_string()
                == str(channel_id),
                FileUpload.file_metadata[_PROV_THREAD_KEY].as_string() == thread_key,
                FileUpload.file_metadata[_PROV_EXTERNAL_MESSAGE_ID].as_string()
                == external_id,
            )
            # Deterministic winner if a pre-fix double-store already left two
            # rows at the same position: the older one, which is the one an
            # earlier delivery of this message actually handed to the agent.
            .order_by(col(FileUpload.uploaded_at))
        ).all()
    except Exception:
        logger.warning(
            "Could not look up already-materialised attachments for message %s "
            "on channel %s; materialising from scratch",
            external_id,
            channel_id,
            exc_info=True,
        )
        return {}

    found: dict[int, tuple[uuid.UUID, str, int]] = {}
    for file_id, filename, file_size, raw_index in rows:
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            # Written before this key existed, or hand-edited. Not a key, so
            # not reusable; the ref it belongs to is materialised again.
            continue
        found.setdefault(index, (file_id, filename, file_size))
    return found
