"""
Email Sending Scheduler - Background job that processes the outgoing email queue.

Runs every 2 minutes, sends all pending outgoing emails via SMTP.
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.db import engine
from app.services.email.sending_service import EmailSendingService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()

SEND_INTERVAL_MINUTES = 2


def run_email_sending():
    """Background job: process outgoing email queue."""
    try:
        with Session(engine) as session:
            sent_count = EmailSendingService.send_pending_emails(session)
            if sent_count > 0:
                logger.info(f"Email sending complete: {sent_count} emails sent")
            else:
                logger.debug("Email sending complete: no pending emails")
    except Exception as e:
        logger.error(f"Email sending job failed: {e}", exc_info=True)


def start_scheduler():
    """Start background scheduler for email sending (call on app startup)."""
    scheduler.add_job(
        run_email_sending,
        "interval",
        minutes=SEND_INTERVAL_MINUTES,
        id="email_sending",
    )
    scheduler.start()
    logger.info(f"Email sending scheduler started (runs every {SEND_INTERVAL_MINUTES} minutes)")


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)."""
    scheduler.shutdown()
    logger.info("Email sending scheduler stopped")
