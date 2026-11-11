"""Voice transcription ledger: one row per session, monthly sums for the cap.

Months are UTC calendar months. The ledger holds durations and outcomes
only, never transcript text or audio.
"""
from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func
from sqlmodel import Session, col, select

from app.models.users.user import User
from app.models.voice_transcription.usage import (
    VoiceTranscriptionUsage,
    VoiceTranscriptionUsagePublic,
    VoiceTranscriptionUsageReport,
    VoiceTranscriptionUsageRow,
)
from app.services.voice_transcription.config_service import (
    VoiceTranscriptionConfigService,
)


def month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    """``[start, end)`` of the UTC calendar month containing ``now``."""
    now = (now or datetime.now(UTC)).astimezone(UTC)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return start, end


def cap_seconds(cap_minutes: int | None) -> float | None:
    return None if cap_minutes is None else float(cap_minutes * 60)


class VoiceTranscriptionUsageService:
    @staticmethod
    def month_seconds(
        session: Session, user_id: uuid.UUID, now: datetime | None = None
    ) -> float:
        """Audio seconds the user has used this month (finished sessions)."""
        start, _ = month_bounds(now)
        total = session.exec(
            select(func.coalesce(func.sum(VoiceTranscriptionUsage.audio_seconds), 0.0)).where(
                VoiceTranscriptionUsage.user_id == user_id,
                VoiceTranscriptionUsage.started_at >= start,
            )
        ).one()
        return float(total or 0.0)

    @staticmethod
    def record(session: Session, row: VoiceTranscriptionUsage) -> None:
        session.add(row)
        session.commit()

    @staticmethod
    def user_usage(session: Session, user: User) -> VoiceTranscriptionUsagePublic:
        """``GET /speech/usage``: the caller's own month."""
        _, end = month_bounds()
        return VoiceTranscriptionUsagePublic(
            enabled=VoiceTranscriptionConfigService.resolve(session) is not None,
            used_seconds=VoiceTranscriptionUsageService.month_seconds(session, user.id),
            cap_seconds=cap_seconds(VoiceTranscriptionConfigService.cap_minutes(session)),
            period_end=end,
        )

    @staticmethod
    def admin_report(session: Session) -> VoiceTranscriptionUsageReport:
        """Per-user seconds and sessions for the current month, most first."""
        start, end = month_bounds()
        seconds = func.sum(VoiceTranscriptionUsage.audio_seconds)
        sessions = func.count(VoiceTranscriptionUsage.id)
        rows = session.exec(
            select(
                VoiceTranscriptionUsage.user_id,
                User.email,
                User.full_name,
                seconds,
                sessions,
            )
            .join(User, col(User.id) == col(VoiceTranscriptionUsage.user_id))
            .where(VoiceTranscriptionUsage.started_at >= start)
            .group_by(VoiceTranscriptionUsage.user_id, User.email, User.full_name)
            .order_by(seconds.desc(), User.email)
        ).all()
        report_rows = [
            VoiceTranscriptionUsageRow(
                user_id=user_id,
                email=email,
                full_name=full_name,
                seconds=float(total or 0.0),
                sessions=int(count or 0),
            )
            for user_id, email, full_name, total, count in rows
        ]
        return VoiceTranscriptionUsageReport(
            period_start=start,
            period_end=end,
            rows=report_rows,
            total_seconds=sum(r.seconds for r in report_rows),
            total_sessions=sum(r.sessions for r in report_rows),
        )
