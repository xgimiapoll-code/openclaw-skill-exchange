# Database Contracts

## Scenario: Dual-Backend Database Layer (SQLite / PostgreSQL)

### 1. Scope / Trigger
- Trigger: Project supports both SQLite and PostgreSQL backends via `app/db.py`
- Alembic migration must stay in sync with `SCHEMA_SQL` in `db.py`

### 2. Signatures

```python
# FastAPI dependency (auto-selects backend)
async def get_db() -> AsyncGenerator[Connection, None]

# Context manager for background tasks
@asynccontextmanager
async def get_db_ctx() -> AsyncGenerator[Connection, None]

# Schema initialization (called once at startup)
async def init_db() -> None
```

### 3. Contracts

#### Backend Selection (at import time)
```python
_IS_PG = config.database_url.startswith("postgresql") if config.database_url else False
```

- If `MARKET_DATABASE_URL` is set and starts with `postgresql` → asyncpg backend
- Otherwise → aiosqlite backend with WAL journaling

#### SQLite Connection Setup
Every connection MUST set:
```sql
PRAGMA journal_mode=WAL
PRAGMA foreign_keys=ON
PRAGMA busy_timeout=5000
```

#### PgConnectionWrapper Interface
Maps aiosqlite-style API to asyncpg:
- `execute(query, params)` → `fetch(query, *params)` (returns `PgCursorWrapper`)
- `commit()` → no-op (asyncpg autocommit)
- `close()` → releases connection to pool

#### Query Translation (`_sqlite_to_pg`)
| SQLite | PostgreSQL |
|--------|-----------|
| `datetime('now')` | `NOW()` |
| `date('now')` | `CURRENT_DATE` |
| `datetime('now', '-7 hours')` | `NOW() + INTERVAL '-7 hour'` |
| `?` placeholders | `$1, $2, ...` |

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| Missing `data/` directory | `_ensure_dir()` creates it |
| PG pool not initialized | Lazy init on first `get_db_pg()` call |
| Invalid DSN | `asyncpg.InvalidDSLError` at first connection |
| SAVEPOINT in PG | Works (asyncpg supports it within transactions) |

### 5. Good/Base/Bad Cases

- **Good**: SQLite WAL mode, `PRAGMA foreign_keys=ON`, SAVEPOINT for atomic operations
- **Base**: Fresh DB → `init_db()` creates all 24 tables + 30 indexes
- **Bad**: Running Alembic migration without `resolution_comment` column → disputes resolve endpoint silently drops comment

### 6. Tests Required

All 246 tests run against SQLite. PostgreSQL path needs:
- Schema creation via `_init_db_pg()`
- SAVEPOINT atomicity (wallet operations)
- Query translation for datetime functions

### 7. Wrong vs Correct

#### Wrong — Alembic migration missing column
```sql
-- disputes table in Alembic migration (BEFORE fix)
CREATE TABLE disputes (
    ...
    resolution_method TEXT,
    resolved_at TEXT,     -- resolution_comment was MISSING here
    created_at TEXT
);
```

#### Correct — Schema and migration must match
```sql
-- disputes table (matches SCHEMA_SQL in db.py)
CREATE TABLE disputes (
    ...
    resolution_method TEXT,
    resolution_comment TEXT,   -- Added: stores admin resolve comment
    resolved_at TEXT,
    created_at TEXT
);
```

> **Gotcha**: `SCHEMA_SQL` in `db.py` is the source of truth. When adding columns, update BOTH `db.py` AND the Alembic migration. For pre-deployment projects, amend the initial migration rather than creating a new one.

---

## Design Decision: Dual-Backend with Wrapper Pattern

**Context**: Need to support both SQLite (simple dev/deploy) and PostgreSQL (production scale).

**Options Considered**:
1. SQLAlchemy ORM — heavy abstraction, we only need raw SQL
2. Wrapper pattern — thin shim that maps aiosqlite API to asyncpg

**Decision**: Wrapper pattern (`PgConnectionWrapper`) because:
- No ORM dependency
- Raw SQL stays visible and debuggable
- Query translation is minimal (datetime functions + placeholder style)
- 497 lines total for full dual-backend support

**Extensibility**: Add new translations to `_sqlite_to_pg()` as needed. Keep patterns simple — if a query needs complex PG-specific syntax, use `if _IS_PG:` conditional.
