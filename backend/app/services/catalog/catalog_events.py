"""CatalogEvents — realtime hints and audit for catalog items.

:meth:`CatalogEvents.granted` sends ``catalog.granted {kind, uuid}`` to each
user who just gained access, into their ``user_{id}`` room (never broadcast,
which would reach guest and webapp sockets). Events are refresh hints only;
the catalog listing is the recovery path.
"""
from __future__ import annotations

import json
import logging
import uuid
from collections.abc import Iterable

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
        for user_id in sorted(set(user_ids), key=str):
            try:
                await event_service.emit_event(
                    event_type=EventType.CATALOG_GRANTED,
                    model_id=item_uuid,
                    user_id=user_id,
                    meta=meta,
                )
            except Exception:
                logger.exception(
                    "Failed to emit %s to user %s", EventType.CATALOG_GRANTED, user_id
                )
