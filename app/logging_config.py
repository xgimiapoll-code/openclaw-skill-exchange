"""Structured logging setup.

- JSON format when LOG_FORMAT=json (production / log aggregators)
- Human-readable format otherwise (development)
"""

import logging
import json
import sys
from datetime import datetime, timezone

from app.config import config


class JSONFormatter(logging.Formatter):
    """Emit one JSON object per log line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info and record.exc_info[0]:
            payload["exc"] = self.formatException(record.exc_info)
        # Attach extras added via logger.info("...", extra={...})
        for key in ("agent_id", "task_id", "tx_type", "amount", "action",
                     "dispute_id", "skill_id", "endpoint", "status_code",
                     "method", "path", "duration_ms"):
            val = getattr(record, key, None)
            if val is not None:
                payload[key] = val
        return json.dumps(payload, default=str)


def setup_logging() -> None:
    """Configure root logger based on LOG_FORMAT env var."""
    log_format = config.log_format
    log_level = getattr(logging, config.log_level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(log_level)

    # Remove existing handlers to avoid duplicates on reload
    root.handlers.clear()

    handler = logging.StreamHandler(sys.stderr)
    handler.setLevel(log_level)

    if log_format == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)-5.5s [%(name)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))

    root.addHandler(handler)

    # Quiet noisy libraries
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("aiosqlite").setLevel(logging.WARNING)
