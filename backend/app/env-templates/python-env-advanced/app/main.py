from fastapi import FastAPI
from fastapi.routing import APIRoute
import logging

from app.server.routes import router

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)

logger = logging.getLogger(__name__)


def custom_generate_unique_id(route: APIRoute) -> str:
    return f"{route.tags[0]}-{route.name}"


app = FastAPI(
    title="Agent Environment Server",
    version="1.0.0",
    generate_unique_id_function=custom_generate_unique_id,
)

# Include API routes
app.include_router(router)

logger.info("Agent Environment Server started")
