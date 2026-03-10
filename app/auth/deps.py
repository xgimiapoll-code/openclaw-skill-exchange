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
    if request.headers.get("X-Signature"):
        from app.auth.signature import get_agent_by_signature
        return await get_agent_by_signature(request, db)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid or inactive API key",
    )
