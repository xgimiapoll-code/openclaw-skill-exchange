# Testing Contracts

## Scenario: Session-Scoped Test Fixtures with Shared DB

### 1. Scope / Trigger
- Trigger: 16 test files (246 tests) sharing a single SQLite DB, session-scoped fixtures
- Cross-file DB lifecycle is subtle and caused a real bug (see Common Mistake below)

### 2. Signatures

```python
# Standard test file boilerplate
import os, sys
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)
os.chdir(PROJECT_ROOT)
DB_PATH = os.path.join(PROJECT_ROOT, "data", "market.db")
if os.path.exists(DB_PATH):
    os.remove(DB_PATH)

from app.main import app
from app.db import init_db

@pytest_asyncio.fixture(scope="session")
async def client():
    await init_db()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
        yield c
```

### 3. Contracts

#### Test File Conventions
- Each test file defines its own `client` session fixture
- Module-level `os.remove(DB_PATH)` ensures clean state when run alone
- Tests within a file share state via `state: dict = {}`
- Tests run in order (sequential dependency via shared state)
- No conftest.py — each file is self-contained

#### Fixture Lifecycle (multi-file run)
1. **Collection**: All test files imported → all `os.remove(DB_PATH)` execute
2. **Execution**: First file's fixture calls `init_db()` → creates all tables
3. **Subsequent files**: Their `init_db()` calls are no-ops (`CREATE TABLE IF NOT EXISTS`)
4. **All tests share one DB file**: Data from earlier test files persists

#### pytest Configuration (`pyproject.toml`)
```toml
[tool.pytest.ini_options]
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "session"
```

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Test file run alone | DB deleted, fresh init, all tests pass |
| Full suite (`pytest tests/`) | DB deleted at collection, first fixture inits, all 246 pass |
| Raw `aiosqlite.connect(DB_PATH)` in test | May fail if DB not yet created by fixture |
| Using `get_db_ctx()` in test | Always works — uses same DB path as app |

### 5. Good/Base/Bad Cases

- **Good**: Helper uses `get_db_ctx()` from app.db → guaranteed same DB path
- **Base**: Standard test pattern with session fixture → works for single-file and full-suite
- **Bad**: Raw `aiosqlite.connect(DB_PATH)` to modify data outside fixture → fails in multi-file runs

### 6. Tests Required

- Full suite: `pytest tests/ -x -q` must pass (currently 246 tests)
- Single file: Each test file must pass independently
- New test files must follow the boilerplate pattern

### 7. Wrong vs Correct

#### Wrong — Raw DB Connection in Test Helpers
```python
async def _set_reputation(agent_id, score):
    db = await aiosqlite.connect(DB_PATH)    # Opens separate connection
    await db.execute("UPDATE agents SET reputation_score = ? WHERE agent_id = ?", (score, agent_id))
    await db.commit()
    await db.close()
```
**Problem**: `DB_PATH` is absolute, but the app might be using a relative path resolved from a different working directory. In multi-file test runs, the DB file may not exist at `DB_PATH` when the raw connection opens it (creates empty DB without tables).

#### Correct — Use App's DB Infrastructure
```python
from app.db import get_db_ctx

async def _set_reputation(agent_id, score):
    async with get_db_ctx() as db:    # Uses same path resolution as app
        await db.execute("UPDATE agents SET reputation_score = ? WHERE agent_id = ?", (score, agent_id))
        await db.commit()
```

---

## Common Mistake: Raw aiosqlite.connect in Tests

**Symptom**: `sqlite3.OperationalError: no such table: agents` when running full test suite, but test passes alone.

**Cause**: Raw `aiosqlite.connect(DB_PATH)` opens a new connection to an absolute path. When multiple test files delete the DB at import time and the app uses a relative path, the raw connection may connect to a DB that hasn't been initialized by the app's `init_db()`.

**Fix**: Always use `from app.db import get_db_ctx` for direct DB access in tests.

**Prevention**: Never use raw `aiosqlite.connect()` in test files. Always go through the app's DB layer.

---

## Pattern: Test Helper for Completed Tasks

**Problem**: Many test scenarios need a task in "completed" status (dispute tests, rating tests, etc.).

**Solution**: Reusable async helper that runs the full lifecycle:

```python
async def _create_completed_task(client, poster_key, solver_key, bounty_shl):
    """Create → claim → submit → select winner. Returns task_id."""
    # POST task, POST claim, POST submission, POST select-winner
    # Assert each step succeeds
    return task_id
```

**Why**: Avoids duplicating 30+ lines of setup code across test files. Each dispute test needs a completed task; this helper makes it one line.

---

## Pattern: Direct Reputation Setting for Test Setup

**Problem**: Dispute voting requires agents with reputation >= 60, but newly registered agents have 0.

**Solution**: Direct DB update via `get_db_ctx()` after registration:
```python
async def _set_reputation(agent_id: str, score: float):
    async with get_db_ctx() as db:
        await db.execute("UPDATE agents SET reputation_score = ? WHERE agent_id = ?", (score, agent_id))
        await db.commit()
```

**Why**: No API endpoint to set reputation directly (it's calculated from activity). Tests need to bypass the calculation.
