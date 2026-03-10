"""WebSocket and EventBus tests.

Covers:
- EventBus unit tests: broadcast, targeted delivery, topic filtering
- WebSocket integration: auth success, auth failures, topic subscription
"""

import os
import sys

import pytest
import pytest_asyncio
import httpx

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)

DB_PATH = os.path.join(PROJECT_ROOT, "data", "ws_test.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

os.environ["MARKET_DB_PATH"] = DB_PATH

import importlib
for mod in ["app.config", "app.db"]:
    if mod in sys.modules:
        del sys.modules[mod]

import app.config
importlib.reload(app.config)
import app.db
importlib.reload(app.db)

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402
from app.services.event_bus import EventBus, Event, event_bus  # noqa: E402

state: dict = {}


@pytest_asyncio.fixture(scope="module")
async def client():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def register(client, node_id, name, skill_tags=None):
    resp = await client.post(
        "/v1/market/agents/register",
        json={"node_id": node_id, "display_name": name, "skill_tags": skill_tags or []},
    )
    assert resp.status_code == 201
    data = resp.json()
    return data["api_key"], data["agent"]["agent_id"]


def auth(key):
    return {"Authorization": f"Bearer {key}"}


# ── Setup ──


async def test_setup(client):
    """Register agents for WebSocket tests."""
    state["alice_key"], state["alice_id"] = await register(
        client, "ws-alice", "Alice", ["python"]
    )
    state["bob_key"], state["bob_id"] = await register(
        client, "ws-bob", "Bob", ["docker"]
    )


# ── EventBus Unit Tests ──


async def test_event_bus_broadcast():
    """Events without target_agent_ids go to all subscribers."""
    bus = EventBus()
    q1 = bus.subscribe("agent-broadcast-1")
    q2 = bus.subscribe("agent-broadcast-2")

    await bus.publish(Event(topic="test.broadcast", data={"msg": "hello"}))

    assert not q1.empty()
    assert not q2.empty()

    e1 = q1.get_nowait()
    assert e1["topic"] == "test.broadcast"
    assert e1["data"]["msg"] == "hello"

    e2 = q2.get_nowait()
    assert e2["topic"] == "test.broadcast"

    bus.unsubscribe("agent-broadcast-1")
    bus.unsubscribe("agent-broadcast-2")


async def test_event_bus_targeted():
    """Targeted events only go to specified agents."""
    bus = EventBus()
    q1 = bus.subscribe("agent-target-1")
    q2 = bus.subscribe("agent-target-2")

    await bus.publish(Event(
        topic="test.targeted",
        data={"msg": "only-for-1"},
        target_agent_ids=["agent-target-1"],
    ))

    assert not q1.empty()
    assert q2.empty()

    e1 = q1.get_nowait()
    assert e1["topic"] == "test.targeted"
    assert e1["data"]["msg"] == "only-for-1"

    bus.unsubscribe("agent-target-1")
    bus.unsubscribe("agent-target-2")


async def test_event_bus_topic_filter_wildcard():
    """Wildcard topic filters (e.g. 'task.*') match events with that prefix."""
    bus = EventBus()
    q = bus.subscribe("agent-filter-1", topics={"task.*"})

    await bus.publish(Event(topic="task.new", data={"id": "t1"}))
    await bus.publish(Event(topic="task.completed", data={"id": "t2"}))
    await bus.publish(Event(topic="wallet.update", data={"balance": 100}))

    # Only task.* events should pass through
    assert q.qsize() == 2

    e1 = q.get_nowait()
    assert e1["topic"] == "task.new"
    e2 = q.get_nowait()
    assert e2["topic"] == "task.completed"

    bus.unsubscribe("agent-filter-1")


async def test_event_bus_topic_filter_exact():
    """Exact topic filters match only the specified topic."""
    bus = EventBus()
    q = bus.subscribe("agent-filter-exact", topics={"task.new"})

    await bus.publish(Event(topic="task.new", data={"id": "t1"}))
    await bus.publish(Event(topic="task.completed", data={"id": "t2"}))

    # Only the exact match should pass
    assert q.qsize() == 1
    e = q.get_nowait()
    assert e["topic"] == "task.new"

    bus.unsubscribe("agent-filter-exact")


async def test_event_bus_star_filter():
    """'*' filter matches all events."""
    bus = EventBus()
    q = bus.subscribe("agent-star", topics={"*"})

    await bus.publish(Event(topic="task.new", data={}))
    await bus.publish(Event(topic="wallet.update", data={}))
    await bus.publish(Event(topic="submission.new", data={}))

    assert q.qsize() == 3

    bus.unsubscribe("agent-star")


async def test_event_bus_no_filter_receives_all():
    """Subscriber without topic filter receives all events."""
    bus = EventBus()
    q = bus.subscribe("agent-nofilter")

    await bus.publish(Event(topic="task.new", data={}))
    await bus.publish(Event(topic="wallet.update", data={}))

    assert q.qsize() == 2

    bus.unsubscribe("agent-nofilter")


async def test_event_bus_unsubscribe():
    """Unsubscribed agents no longer receive events."""
    bus = EventBus()
    q = bus.subscribe("agent-unsub")

    await bus.publish(Event(topic="test.before", data={}))
    assert q.qsize() == 1

    bus.unsubscribe("agent-unsub")

    await bus.publish(Event(topic="test.after", data={}))
    # Queue should still have just the 1 event from before unsubscribe
    assert q.qsize() == 1


async def test_event_bus_queue_full_no_crash():
    """When queue is full, publishing does not crash (event is dropped)."""
    bus = EventBus()
    q = bus.subscribe("agent-full")

    # Fill the queue (maxsize=100)
    for i in range(100):
        await bus.publish(Event(topic="spam", data={"i": i}))

    assert q.qsize() == 100

    # Publishing one more should not raise
    await bus.publish(Event(topic="spam.overflow", data={}))
    assert q.qsize() == 100  # still 100, overflow was dropped

    bus.unsubscribe("agent-full")


# ── WebSocket Integration Tests (using starlette TestClient) ──


def test_ws_auth_success():
    """WebSocket connects with valid token and receives connected message."""
    from starlette.testclient import TestClient

    with TestClient(app) as tc:
        token = state["alice_key"]
        with tc.websocket_connect(f"/v1/market/ws?token={token}") as ws:
            data = ws.receive_json()
            assert data["type"] == "connected"
            assert data["agent_id"] == state["alice_id"]


def test_ws_auth_missing_token():
    """WebSocket without token receives error and close code 4001."""
    from starlette.testclient import TestClient

    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/market/ws") as ws:
            data = ws.receive_json()
            assert "error" in data
            assert "Missing token" in data["error"]


def test_ws_auth_invalid_token():
    """WebSocket with invalid token receives error and close code 4001."""
    from starlette.testclient import TestClient

    with TestClient(app) as tc:
        with tc.websocket_connect("/v1/market/ws?token=invalid-key-999") as ws:
            data = ws.receive_json()
            assert "error" in data
            assert "Invalid" in data["error"]


def test_ws_subscribe_topics():
    """Client can subscribe to topics after connecting."""
    from starlette.testclient import TestClient

    with TestClient(app) as tc:
        token = state["alice_key"]
        with tc.websocket_connect(f"/v1/market/ws?token={token}") as ws:
            # Consume the connected message
            connected = ws.receive_json()
            assert connected["type"] == "connected"

            # Send subscription request
            ws.send_json({"subscribe": ["task.*", "wallet.*"]})
            sub_ack = ws.receive_json()
            assert sub_ack["type"] == "subscribed"
            assert set(sub_ack["topics"]) == {"task.*", "wallet.*"}
