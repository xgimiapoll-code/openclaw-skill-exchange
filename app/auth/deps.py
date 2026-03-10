"""Authentication dependencies for FastAPI.

Supports two auth methods:
  1. Bearer API key (primary)
  2. Ed25519 signature headers (X-Signature, X-Timestamp, X-Agent-Id)
"""

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import aiosqlite

from app.db import get_db

security = HTTPBearer(auto_error=False)


async def get_current_agent(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(security),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Validate auth and return current agent.

    Accepts Bearer API key or Ed25519 signature headers.
    """
    # 1. Try Bearer API key
    if credentials:
        api_key = credentials.credentials
        cursor = await db.execute(
            "SELECT * FROM agents WHERE api_key = ? AND status = 'active'",
            (api_key,),
        )
        agent = await cursor.fetchone()
        if agent:
            return dict(agent)

    # 2. Try Ed25519 signature headers
    sig = request.headers.get("X-Signature")
    ts = request.headers.get("X-Timestamp")
    agent_id = request.headers.get("X-Agent-Id")

    if sig and ts and agent_id:
        from app.auth.signature import verify_ed25519, build_sign_payload, MAX_CLOCK_SKEW
        from datetime import datetime, timezone

        # Validate timestamp
        try:
            req_time = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if req_time.tzinfo is None:
                req_time = req_time.replace(tzinfo=timezone.utc)
        except ValueError:
            raise HTTPException(status_code=400, detail="Invalid X-Timestamp format")

        now = datetime.now(timezone.utc)
        if abs(now - req_time) > MAX_CLOCK_SKEW:
            raise HTTPException(status_code=401, detail="Timestamp expired or too far in the future")

        # Look up agent
        cursor = await db.execute(
            "SELECT * FROM agents WHERE agent_id = ? AND status = 'active'",
            (agent_id,),
        )
        agent = await cursor.fetchone()
        if not agent:
            raise HTTPException(status_code=401, detail="Agent not found or inactive")

        pub_key = agent["public_key"]
        if not pub_key:
            raise HTTPException(status_code=401, detail="Agent has no public key registered")

        body = await request.body()
        payload = build_sign_payload(request.method, request.url.path, ts, body)
        if not verify_ed25519(pub_key, sig, payload):
            raise HTTPException(status_code=401, detail="Invalid signature")

        return dict(agent)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or inactive API key",
    )
