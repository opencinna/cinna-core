"""
Email Polling Scheduler - Background job that periodically polls IMAP mailboxes
and triggers email processing.

Runs every 3 minutes:
1. Polls all enabled agents' IMAP mailboxes for new emails
2. Processes newly stored emails (routes to clones, creates sessions)
3. Retries emails pending clone readiness
"""
import asyncio
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.db import engine
from app.services.email.polling_service import EmailPollingService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

POLL_INTERVAL_MINUTES = 5


def run_email_polling():
    """Background job: poll mailboxes, then process new and pending emails."""
    try:
        asyncio.run(_poll_and_process())
    except Exception as e:
        logger.error(f"Email polling job failed: {e}", exc_info=True)


async def _poll_and_process():
    """Async implementation: poll + process emails."""
    from app.services.email.processing_service import EmailProcessingService

    with Session(engine) as session:
        # 1. Poll all enabled agents' mailboxes
        stored_ids = EmailPollingService.poll_all_enabled_agents(session)
        if stored_ids:
            logger.info(f"Email polling: {len(stored_ids)} new emails stored")

        # 2. Process newly stored emails
        for email_id in stored_ids:
            try:
                await EmailProcessingService.process_incoming_email(session, email_id)
            except Exception as e:
                logger.error(
                    f"Failed to process new email {email_id}: {e}", exc_info=True
                )

        # 3. Retry emails pending clone readiness
        try:
            pending_count = await EmailProcessingService.process_pending_emails(session)
            if pending_count > 0:
                logger.info(f"Processed {pending_count} previously pending emails")
        except Exception as e:
            logger.error(f"Failed to process pending emails: {e}", exc_info=True)


def start_scheduler():
    """Start background scheduler for email polling (call on app startup)."""
    scheduler.add_job(
        run_email_polling,
        "interval",
        minutes=POLL_INTERVAL_MINUTES,
        id="email_polling",
    )
    scheduler.start()
    logger.info(f"Email polling scheduler started (runs every {POLL_INTERVAL_MINUTES} minutes)")


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Email polling scheduler stopped")
