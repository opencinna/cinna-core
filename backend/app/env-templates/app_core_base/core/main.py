from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.routing import APIRoute
import logging

from core.server.routes import router
from core.server.status_watcher import start_status_watcher

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: launch the STATUS.md mtime watcher in a background thread
    start_status_watcher()
    logger.info("Agent Environment Server started")
    yield
    # Shutdown: daemon thread exits automatically


app = FastAPI(
    title="Agent Environment Server",
    version="1.0.0",
    generate_unique_id_function=custom_generate_unique_id,
    lifespan=lifespan,
)

# Include API routes
app.include_router(router)
