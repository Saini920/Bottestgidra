"""WebSocket connection manager — live progress to the browser."""
import asyncio
from typing import Dict, Set

from fastapi import WebSocket


class ConnectionManager:
    def __init__(self):
        self.channels: Dict[str, Set[WebSocket]] = {}

    async def connect(self, channel: str, ws: WebSocket):
        await ws.accept()
        self.channels.setdefault(channel, set()).add(ws)

    def disconnect(self, channel: str, ws: WebSocket):
        if channel in self.channels:
            self.channels[channel].discard(ws)
            if not self.channels[channel]:
                self.channels.pop(channel, None)

    async def broadcast(self, channel: str, data: dict):
        """Push a JSON-serializable event to everyone subscribed to a channel."""
        dead = []
        for ws in list(self.channels.get(channel, ())):
            try:
                await ws.send_json(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(channel, ws)

    async def close_channel(self, channel: str):
        for ws in list(self.channels.get(channel, ())):
            try:
                await ws.close()
            except Exception:
                pass
        self.channels.pop(channel, None)
