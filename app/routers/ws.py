"""WebSocket endpoint for real-time notifications."""

import asyncio
import json
import logging

import aiosqlite
from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.db import get_db_ctx
from app.services.event_bus import event_bus

logger = logging.getLogger(__name__)

router = APIRouter(tags=["websocket"])


@router.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """WebSocket connection for real-time events.

    Authenticate via query param: ?token=<api_key>
    Optionally subscribe to topics: send {"subscribe": ["tasks.*", "wallet.*"]}
    """
    await websocket.accept()

    # Authenticate
    token = websocket.query_params.get("token")
    if not token:
        await websocket.send_json({"error": "Missing token query parameter"})
        await websocket.close(code=4001)
        return

    async with get_db_ctx() as db:
        cur = await db.execute(
            "SELECT agent_id FROM agents WHERE api_key = ? AND status = 'active'",
            (token,),
        )
        agent = await cur.fetchone()

    if not agent:
        await websocket.send_json({"error": "Invalid or inactive API key"})
        await websocket.close(code=4001)
        return

    agent_id = agent["agent_id"]
    queue = event_bus.subscribe(agent_id)

    await websocket.send_json({"type": "connected", "agent_id": agent_id})

    async def send_events():
        """Forward events from bus to WebSocket."""
        try:
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except asyncio.CancelledError:
            pass

    sender = asyncio.create_task(send_events())

    try:
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
                # Handle subscription updates
                if "subscribe" in msg:
                    topics = set(msg["subscribe"])
                    event_bus._topic_filters[agent_id] = topics
                    await websocket.send_json({"type": "subscribed", "topics": list(topics)})
            except json.JSONDecodeError:
                await websocket.send_json({"error": "Invalid JSON"})
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        event_bus.unsubscribe(agent_id)
