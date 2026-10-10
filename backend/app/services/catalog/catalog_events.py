"""CatalogEvents — realtime hints and audit for catalog items.

:meth:`CatalogEvents.granted` sends ``catalog.granted {kind, uuid}`` to each
user who just gained access, and :meth:`CatalogEvents.updated` sends
``catalog.updated {kind, uuid, id, revision_number, head_sha?}`` after a new
revision, each into the user's ``user_{id}`` room (never broadcast, which would
reach guest and webapp sockets). Events are refresh hints only;
the catalog listing is the recovery path.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable
from typing import Any

from sqlmodel import Session

from app.models.events.event import EventType
from app.models.events.security_event import SecurityEvent
from app.services.events.event_service import event_service

logger = logging.getLogger(__name__)


def add_audit_event(
    session: Session,
    user_id: uuid.UUID,
    event_type: str,
    details: dict,
) -> None:
    """Stage a low-severity catalog ``SecurityEvent`` (caller commits)."""
    session.add(
        SecurityEvent(
            user_id=user_id,
            event_type=event_type,
            severity="low",
            details=json.dumps(details, default=str),
        )
    )


class CatalogEvents:
    """Static entry points; safe to call from routes and background tasks."""

    @staticmethod
    async def granted(
        kind: str, item_uuid: uuid.UUID, user_ids: Iterable[uuid.UUID]
    ) -> None:
        """``catalog.granted {kind, uuid}`` to each user who just gained access."""
        meta = {"kind": kind, "uuid": str(item_uuid)}
        await CatalogEvents._emit_each(EventType.CATALOG_GRANTED, item_uuid, user_ids, meta)

    @staticmethod
    async def updated(
        item_uuid: uuid.UUID, user_ids: Iterable[uuid.UUID], meta: dict
    ) -> None:
        """``catalog.updated`` (``meta`` as documented on the event type) to
        each of ``user_ids`` (the item's update audience)."""
        await CatalogEvents._emit_each(EventType.CATALOG_UPDATED, item_uuid, user_ids, meta)

    @staticmethod
    async def revision_published(
        session: Session, kind: str, item: Any, revision_number: int
    ) -> None:
        """``catalog.updated`` after a cloud bundle or skill package revision
        commit, to the item's update audience (grants ∪ cloud and local
        installers, P9). Never raises: the publish has already succeeded."""
        # Lazy: local_install_service → catalog_access_service → this module.
        from app.services.catalog.catalog_access_service import BUNDLE, SKILL
        from app.services.catalog.local_install_service import LocalInstallService

        spec = BUNDLE if kind == BUNDLE.wire_kind else SKILL
        try:
            audience = LocalInstallService.update_audience(session, spec, item)
        except Exception:
            logger.exception("Failed to resolve catalog.updated audience for %s", item.id)
            # Leave the caller's session usable after a failed query.
            session.rollback()
            return
        meta = {
            "kind": spec.wire_kind,
            "uuid": str(item.id),
            "id": item.bundle_id if spec is BUNDLE else item.package_id,
            "revision_number": revision_number,
        }
        await CatalogEvents.updated(item.id, audience, meta)

    @staticmethod
    async def _emit_each(
        event_type: str, item_uuid: uuid.UUID, user_ids: Iterable[uuid.UUID], meta: dict
    ) -> None:
        for user_id in sorted(set(user_ids), key=str):
            try:
                await event_service.emit_event(
                    event_type=event_type,
                    model_id=item_uuid,
                    user_id=user_id,
                    meta=meta,
                )
            except Exception:
                logger.exception("Failed to emit %s to user %s", event_type, user_id)
