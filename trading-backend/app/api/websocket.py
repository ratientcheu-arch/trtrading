"""
WebSocket manager — handles connections, broadcasts events to the frontend.
"""
import json
import asyncio
from typing import Optional
from fastapi import WebSocket, WebSocketDisconnect
from app.config import settings
from app.utils.logging import get_logger

logger = get_logger(__name__)


class WebSocketManager:
    def __init__(self):
        self._connections: list[WebSocket] = []

    async def connect(self, ws: WebSocket):
        await ws.accept()
        self._connections.append(ws)
        logger.info(f"WebSocket client connected ({len(self._connections)} total)")

    def disconnect(self, ws: WebSocket):
        if ws in self._connections:
            self._connections.remove(ws)
        logger.info(f"WebSocket client disconnected ({len(self._connections)} total)")

    async def broadcast(self, event_type: str, data: dict):
        """Broadcast an event to all connected clients."""
        message = json.dumps({"type": event_type, "data": data})
        disconnected = []
        for ws in self._connections:
            try:
                await ws.send_text(message)
            except Exception:
                disconnected.append(ws)
        for ws in disconnected:
            self.disconnect(ws)

    async def send_to(self, ws: WebSocket, event_type: str, data: dict):
        try:
            await ws.send_text(json.dumps({"type": event_type, "data": data}))
        except Exception:
            self.disconnect(ws)

    @property
    def client_count(self) -> int:
        return len(self._connections)


ws_manager = WebSocketManager()
