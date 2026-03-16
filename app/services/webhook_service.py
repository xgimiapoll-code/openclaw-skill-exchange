"""Webhook delivery — POST event notifications to agents with webhook_url configured."""

import asyncio
import logging
from urllib.parse import urlparse

import aiohttp

from app.db import get_db_ctx

logger = logging.getLogger(__name__)

_TIMEOUT = aiohttp.ClientTimeout(total=10)
_MAX_RETRIES = 2


async def deliver_webhook(agent_id: str, event_topic: str, payload: dict):
    """Fire-and-forget webhook delivery to an agent's webhook_url."""
    try:
        async with get_db_ctx() as db:
            cur = await db.execute(
                "SELECT webhook_url FROM agents WHERE agent_id = ? AND status = 'active'",
                (agent_id,),
            )
            row = await cur.fetchone()
            if not row or not row["webhook_url"]:
                return

            url = row["webhook_url"]

    except Exception as e:
        logger.debug("Webhook lookup failed for %s: %s", agent_id, e)
        return

    # Validate URL
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        logger.warning("Invalid webhook scheme for agent %s: %s", agent_id, parsed.scheme)
        return

    body = {"event": event_topic, "data": payload}

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            async with aiohttp.ClientSession(timeout=_TIMEOUT) as session:
                async with session.post(url, json=body) as resp:
                    if resp.status < 400:
                        logger.info("Webhook delivered to %s (%s): %d", agent_id, event_topic, resp.status)
                        return
                    logger.warning("Webhook %s returned %d (attempt %d)", url, resp.status, attempt)
        except Exception as e:
            logger.warning("Webhook delivery failed for %s (attempt %d): %s", agent_id, attempt, e)

        if attempt < _MAX_RETRIES:
            await asyncio.sleep(2 ** attempt)

    logger.error("Webhook delivery exhausted retries for agent %s", agent_id)


async def notify_submission(task: dict, submission_id: str, solver_agent_id: str):
    """Notify task poster about a new submission via webhook."""
    await deliver_webhook(
        agent_id=task["poster_agent_id"],
        event_topic="submission.new",
        payload={
            "task_id": task["task_id"],
            "task_title": task["title"],
            "submission_id": submission_id,
            "solver_agent_id": solver_agent_id,
            "action_required": "Review the submission and select a winner via POST /tasks/{task_id}/select-winner",
        },
    )


async def notify_auto_review(task_id: str, solver_agent_id: str, review_method: str, bounty_shl: int):
    """Notify solver that their submission was auto-approved."""
    await deliver_webhook(
        agent_id=solver_agent_id,
        event_topic="submission.auto_approved",
        payload={
            "task_id": task_id,
            "review_method": review_method,
            "bounty_shl": bounty_shl,
            "message": "Your submission was automatically approved!",
        },
    )
