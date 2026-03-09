"""In-memory event bus for WebSocket fan-out."""

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class Event:
    topic: str  # e.g. "task.new", "submission.new", "wallet.update"
    data: dict[str, Any]
    target_agent_ids: list[str] | None = None  # None = broadcast


class EventBus:
    """Simple async event bus with per-agent queues."""

    def __init__(self):
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._topic_filters: dict[str, set[str]] = {}

    def subscribe(self, agent_id: str, topics: set[str] | None = None) -> asyncio.Queue:
        """Subscribe an agent to events. Returns a Queue to await on."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers[agent_id] = queue
        if topics:
            self._topic_filters[agent_id] = topics
        return queue

    def unsubscribe(self, agent_id: str):
        """Remove an agent's subscription."""
        self._subscribers.pop(agent_id, None)
        self._topic_filters.pop(agent_id, None)

    async def publish(self, event: Event):
        """Publish an event to matching subscribers."""
        for agent_id, queue in list(self._subscribers.items()):
            # Check target filter
            if event.target_agent_ids and agent_id not in event.target_agent_ids:
                continue

            # Check topic filter
            topic_filters = self._topic_filters.get(agent_id)
            if topic_filters:
                # Match on prefix: "tasks.*" matches "task.new"
                topic_base = event.topic.split(".")[0]
                if not any(
                    event.topic == f or f"{topic_base}.*" in topic_filters or f == "*"
                    for f in topic_filters
                ):
                    continue

            try:
                queue.put_nowait({
                    "topic": event.topic,
                    "data": event.data,
                })
            except asyncio.QueueFull:
                logger.warning("Event queue full for agent %s, dropping event", agent_id)


# Global singleton
event_bus = EventBus()
