"""Tests for Ed25519 signature authentication."""

import base64
import hashlib
from datetime import datetime, timezone, timedelta

import pytest
from httpx import ASGITransport, AsyncClient

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.main import app

BASE = "http://test"


def _make_keypair():
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes_raw()
    pub_b64 = base64.b64encode(pub_bytes).decode()
    return priv, pub_b64


def _sign_request(priv_key, method, path, body=b"", ts=None):
    if ts is None:
        ts = datetime.now(timezone.utc).isoformat()
    body_hash = hashlib.sha256(body).hexdigest()
    payload = f"{method}\n{path}\n{ts}\n{body_hash}".encode()
    sig = priv_key.sign(payload)
    return base64.b64encode(sig).decode(), ts


@pytest.fixture(scope="module")
async def client():
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url=BASE) as c:
        yield c


@pytest.fixture(scope="module")
async def agent_with_key(client):
    priv, pub_b64 = _make_keypair()
    # Register
    r = await client.post("/v1/market/agents/register", json={
        "node_id": "ed25519-test-agent",
        "display_name": "Ed25519 Test",
        "public_key": pub_b64,
        "skill_tags": ["python"],
    })
    assert r.status_code == 201
    data = r.json()
    return {
        "agent_id": data["agent"]["agent_id"],
        "api_key": data["api_key"],
        "priv_key": priv,
        "pub_b64": pub_b64,
    }


async def test_register_with_public_key(agent_with_key):
    """Agent registered with public key."""
    assert agent_with_key["pub_b64"]


async def test_set_public_key(client, agent_with_key):
    """Update public key via PUT endpoint."""
    _, new_pub = _make_keypair()
    r = await client.put(
        "/v1/market/agents/me/public-key",
        json={"public_key": new_pub},
        headers={"Authorization": f"Bearer {agent_with_key['api_key']}"},
    )
    assert r.status_code == 200
    assert r.json()["public_key"] == new_pub

    # Restore original key
    r = await client.put(
        "/v1/market/agents/me/public-key",
        json={"public_key": agent_with_key["pub_b64"]},
        headers={"Authorization": f"Bearer {agent_with_key['api_key']}"},
    )
    assert r.status_code == 200


async def test_set_invalid_public_key(client, agent_with_key):
    """Reject invalid public key."""
    r = await client.put(
        "/v1/market/agents/me/public-key",
        json={"public_key": "not-valid-base64!!!"},
        headers={"Authorization": f"Bearer {agent_with_key['api_key']}"},
    )
    assert r.status_code == 400


async def test_signed_request(client, agent_with_key):
    """Make an authenticated request using Ed25519 signature."""
    path = "/v1/market/agents/me"
    sig, ts = _sign_request(agent_with_key["priv_key"], "GET", path)
    r = await client.get(path, headers={
        "X-Signature": sig,
        "X-Timestamp": ts,
        "X-Agent-Id": agent_with_key["agent_id"],
    })
    assert r.status_code == 200
    assert r.json()["agent_id"] == agent_with_key["agent_id"]


async def test_expired_timestamp(client, agent_with_key):
    """Reject request with expired timestamp."""
    path = "/v1/market/agents/me"
    old_ts = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    sig, _ = _sign_request(agent_with_key["priv_key"], "GET", path, ts=old_ts)
    r = await client.get(path, headers={
        "X-Signature": sig,
        "X-Timestamp": old_ts,
        "X-Agent-Id": agent_with_key["agent_id"],
    })
    assert r.status_code == 401
    assert "expired" in r.json()["detail"].lower() or "future" in r.json()["detail"].lower()


async def test_wrong_signature(client, agent_with_key):
    """Reject request with wrong signature."""
    other_priv, _ = _make_keypair()
    path = "/v1/market/agents/me"
    sig, ts = _sign_request(other_priv, "GET", path)
    r = await client.get(path, headers={
        "X-Signature": sig,
        "X-Timestamp": ts,
        "X-Agent-Id": agent_with_key["agent_id"],
    })
    assert r.status_code == 401
    assert "Invalid signature" in r.json()["detail"]


async def test_missing_headers(client, agent_with_key):
    """Reject request with missing signature headers and no Bearer token."""
    r = await client.get("/v1/market/agents/me", headers={
        "X-Signature": "something",
        # Missing X-Timestamp and X-Agent-Id
    })
    assert r.status_code in (401, 403)
