"""Authentication dependencies for FastAPI."""

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
import aiosqlite

from app.db import get_db

security = HTTPBearer()


async def get_current_agent(
    credentials: HTTPAuthorizationCredentials = Depends(security),
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Validate API key and return current agent."""
    api_key = credentials.credentials
    cursor = await db.execute(
        "SELECT * FROM agents WHERE api_key = ? AND status = 'active'",
        (api_key,),
    )
    agent = await cursor.fetchone()
    if not agent:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or inactive API key",
        )
    return dict(agent)
