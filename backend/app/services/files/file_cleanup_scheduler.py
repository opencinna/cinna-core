import logging
from apscheduler.schedulers.background import BackgroundScheduler
from sqlmodel import Session

from app.core.db import engine
from app.services.files.garbage_collection_service import GarbageCollectionService

logger = logging.getLogger(__name__)

scheduler = BackgroundScheduler()


def run_cleanup():
    """Run garbage collection tasks"""
    try:
        with Session(engine) as session:
            marked_count = GarbageCollectionService.cleanup_marked_files(session)
            temp_count = GarbageCollectionService.cleanup_abandoned_temp_files(session)
            orphaned_count = GarbageCollectionService.cleanup_orphaned_message_files(session)
            logger.info(
                f"Cleanup complete: {marked_count} marked files, "
                f"{temp_count} temp files, {orphaned_count} orphaned files deleted"
            )
    except Exception as e:
        logger.error(f"Cleanup job failed: {e}")


def start_scheduler():
    """Start background scheduler (call on app startup)"""
    scheduler.add_job(run_cleanup, "interval", hours=1, id="file_cleanup")
    scheduler.start()
    logger.info("File cleanup scheduler started (runs every hour)")


def shutdown_scheduler():
    """Stop background scheduler (call on app shutdown)"""
    scheduler.shutdown()
    logger.info("File cleanup scheduler stopped")
