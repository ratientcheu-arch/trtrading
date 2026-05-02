"""
FastAPI application — API-only process.
The bot runs in a separate process (bot_runner) and talks to MT5 via ZMQ.
Communication between API and bot goes through shared IPC files + PostgreSQL.
"""
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import asyncio
import time

from app.config import settings
from app.utils.logging import setup_logging, get_logger
from app.database import init_db
from app.api.routes import router
from app.api.websocket import ws_manager
from app.ipc import (
    read_json, read_new_events, STATUS_FILE, SIGNALS_FILE,
)

logger = get_logger(__name__)


async def _ws_event_relay():
    """Background task: relay bot events from IPC file to WebSocket clients."""
    last_ts = time.time()
    last_status = None
    while True:
        try:
            # Read new events from bot process
            events, last_ts = read_new_events(last_ts)
            for evt in events:
                await ws_manager.broadcast(evt.get("type", ""), evt.get("data", {}))

            # Also broadcast status periodically (even without events)
            status = read_json(STATUS_FILE)
            if status and status != last_status:
                await ws_manager.broadcast("bot_status", status)
                last_status = status

        except Exception as e:
            logger.debug(f"WS relay error: {e}")

        await asyncio.sleep(2)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    logger.info("Starting Day Trading API v5.1 — API-only process (two-process mode)")
    logger.info("Bot runs in separate process — API talks to MT5 via shared IPC only")

    # Init database (for read access to trades, performance history)
    await init_db()

    # Start WebSocket event relay
    relay_task = asyncio.create_task(_ws_event_relay())

    # Check if bot process is alive
    status = read_json(STATUS_FILE)
    if status:
        logger.info(f"Bot process detected — running={status.get('running', False)}, mt5={status.get('mt5_connected', False)}")
    else:
        logger.warning("Bot process not detected — start it with: python -m app.bot_runner")

    yield

    # Shutdown
    relay_task.cancel()
    logger.info("API process stopped.")


app = FastAPI(
    title="Day Trading Bot API",
    version="5.1.0",
    lifespan=lifespan,
)

# CORS
origins = [o.strip() for o in settings.allowed_origins.split(",")]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Routes
app.include_router(router)


@app.get("/")
async def root():
    return {
        "service": "Day Trading Bot",
        "version": "5.1.0",
        "architecture": "two-process (API + Bot)",
        "broker": "Fusion Markets MT5 (ZMQ bridge)",
        "docs": "/docs",
    }
