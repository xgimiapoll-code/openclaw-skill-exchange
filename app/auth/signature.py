"""Ed25519 signature authentication — alternative to API key auth.

Request signing protocol:
  1. Client signs: "{METHOD}\n{PATH}\n{TIMESTAMP}\n{BODY_SHA256}"
  2. Sends headers: X-Signature (base64), X-Timestamp (ISO 8601), X-Agent-Id
  3. Server verifies signature + timestamp freshness (5 min window)
"""

import base64
import hashlib
from datetime import datetime, timezone, timedelta

import aiosqlite
from fastapi import Depends, HTTPException, Request, status

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from cryptography.exceptions import InvalidSignature

from app.db import get_db

MAX_CLOCK_SKEW = timedelta(minutes=5)


def verify_ed25519(public_key_b64: str, signature_b64: str, message: bytes) -> bool:
    """Verify an Ed25519 signature. Returns True if valid."""
    try:
        pub_bytes = base64.b64decode(public_key_b64)
        sig_bytes = base64.b64decode(signature_b64)
        pub_key = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub_key.verify(sig_bytes, message)
        return True
    except (InvalidSignature, ValueError, Exception):
        return False


def build_sign_payload(method: str, path: str, timestamp: str, body: bytes) -> bytes:
    """Build the canonical message to sign."""
    body_hash = hashlib.sha256(body).hexdigest()
    return f"{method}\n{path}\n{timestamp}\n{body_hash}".encode()


async def get_agent_by_signature(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """FastAPI dependency: authenticate via Ed25519 signature headers."""
    sig = request.headers.get("X-Signature")
    ts = request.headers.get("X-Timestamp")
    agent_id = request.headers.get("X-Agent-Id")

    if not sig or not ts or not agent_id:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing signature headers (X-Signature, X-Timestamp, X-Agent-Id)",
        )

    # Check timestamp freshness
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
    cur = await db.execute(
        "SELECT * FROM agents WHERE agent_id = ? AND status = 'active'",
        (agent_id,),
    )
    agent = await cur.fetchone()
    if not agent:
        raise HTTPException(status_code=401, detail="Agent not found or inactive")

    pub_key = agent["public_key"]
    if not pub_key:
        raise HTTPException(status_code=401, detail="Agent has no public key registered")

    # Read request body
    body = await request.body()

    # Verify signature
    payload = build_sign_payload(request.method, request.url.path, ts, body)
    if not verify_ed25519(pub_key, sig, payload):
        raise HTTPException(status_code=401, detail="Invalid signature")

    return dict(agent)


async def get_agent_flexible(
    request: Request,
    db: aiosqlite.Connection = Depends(get_db),
) -> dict:
    """Dual auth: accepts EITHER Bearer API key OR Ed25519 signature headers.

    Priority: Bearer token first, then signature headers.
    """
    # Try Bearer token
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        api_key = auth_header[7:]
        cur = await db.execute(
            "SELECT * FROM agents WHERE api_key = ? AND status = 'active'",
            (api_key,),
        )
        agent = await cur.fetchone()
        if agent:
            return dict(agent)

    # Try Ed25519 signature
    if request.headers.get("X-Signature"):
        return await get_agent_by_signature(request, db)

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Provide either a Bearer API key or Ed25519 signature headers",
    )
