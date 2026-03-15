"""Database connection management — supports SQLite (default) and PostgreSQL.

Backend selection:
  - Set MARKET_DATABASE_URL="postgresql+asyncpg://..." for PostgreSQL
  - Default: SQLite at MARKET_DB_PATH (data/market.db)
"""

import aiosqlite
import os
from contextlib import asynccontextmanager

from app.config import config

_DB_PATH = config.db_path
_DATABASE_URL = config.database_url
_IS_PG = _DATABASE_URL.startswith("postgresql") if _DATABASE_URL else False


def _ensure_dir():
    if not _IS_PG:
        os.makedirs(os.path.dirname(os.path.abspath(_DB_PATH)), exist_ok=True)


# ── SQLite connections (default) ──

async def get_db():
    """Get a database connection (for FastAPI Depends). SQLite backend."""
    _ensure_dir()
    db = await aiosqlite.connect(_DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    try:
        yield db
    finally:
        await db.close()


@asynccontextmanager
async def get_db_ctx():
    """Async context manager for database access outside of FastAPI."""
    _ensure_dir()
    db = await aiosqlite.connect(_DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.execute("PRAGMA busy_timeout=5000")
    try:
        yield db
    finally:
        await db.close()


# ── PostgreSQL connections (optional) ──

_pg_pool = None


async def _get_pg_pool():
    """Lazy-init asyncpg connection pool."""
    global _pg_pool
    if _pg_pool is None:
        import asyncpg
        # Strip the "postgresql+asyncpg://" scheme prefix to get raw DSN
        dsn = _DATABASE_URL.replace("postgresql+asyncpg://", "postgresql://")
        # Disable SSL for Fly.io internal network (.flycast) — PG doesn't expose SSL there
        if ".flycast" in dsn and "sslmode=" not in dsn:
            sep = "&" if "?" in dsn else "?"
            dsn += f"{sep}sslmode=disable"
        _pg_pool = await asyncpg.create_pool(dsn, min_size=2, max_size=10)
    return _pg_pool


class PgConnectionWrapper:
    """Wraps asyncpg connection to provide aiosqlite-compatible interface.

    Maps execute/fetchone/fetchall to asyncpg equivalents so existing
    code works with minimal changes.
    """

    def __init__(self, conn):
        self._conn = conn

    async def execute(self, query: str, params=None):
        query = _sqlite_to_pg(query)
        stripped = query.lstrip().upper()
        is_select = stripped.startswith("SELECT") or stripped.startswith("WITH")
        if params:
            query, params = _positional_to_dollar(query, params)
            params = _coerce_params(params)
            if is_select:
                result = await self._conn.fetch(query, *params)
            else:
                await self._conn.execute(query, *params)
                result = []
        else:
            if is_select:
                result = await self._conn.fetch(query)
            else:
                await self._conn.execute(query)
                result = []
        return PgCursorWrapper(result)

    async def executescript(self, script: str):
        statements = [s.strip() for s in script.split(";") if s.strip()]
        for stmt in statements:
            await self._conn.execute(stmt)

    async def commit(self):
        pass  # autocommit in asyncpg

    async def close(self):
        await self._conn.close()


class _PgRow(dict):
    """Dict subclass that also supports integer indexing like sqlite3.Row.

    Converts datetime objects to ISO strings for compatibility with
    SQLite-style code that expects text timestamps.
    """

    def __init__(self, record):
        from datetime import datetime
        converted = {
            k: v.isoformat().replace("+00:00", "") if isinstance(v, datetime) else v
            for k, v in record.items()
        }
        super().__init__(converted)
        self._values = list(converted.values())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self._values[key]
        return super().__getitem__(key)


class PgCursorWrapper:
    """Wraps asyncpg result to provide fetchone/fetchall interface."""

    def __init__(self, rows):
        self._rows = rows

    async def fetchone(self):
        if self._rows:
            return _PgRow(dict(self._rows[0]))
        return None

    async def fetchall(self):
        return [_PgRow(dict(r)) for r in self._rows]


def _coerce_params(params: list) -> list:
    """Convert ISO timestamp strings to datetime objects for asyncpg TIMESTAMPTZ."""
    from datetime import datetime, timezone
    out = []
    for v in params:
        if isinstance(v, str) and len(v) >= 19:
            try:
                dt = datetime.fromisoformat(v)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                out.append(dt)
                continue
            except ValueError:
                pass
        out.append(v)
    return out


def _sqlite_to_pg(query: str) -> str:
    """Basic SQLite → PostgreSQL query translation for common patterns."""
    import re
    q = query
    q = q.replace("datetime('now')", "NOW()")
    q = q.replace("date('now')", "CURRENT_DATE")
    q = re.sub(r"datetime\('now',\s*'(-?\d+)\s+(hour|day|minute)s?'\)",
               r"NOW() + INTERVAL '\1 \2'", q)
    return q


def _positional_to_dollar(query: str, params: tuple) -> tuple[str, list]:
    """Convert ? placeholders to $1, $2, ... for asyncpg."""
    parts = query.split("?")
    result = parts[0]
    for i, part in enumerate(parts[1:], 1):
        result += f"${i}" + part
    return result, list(params)


async def get_db_pg():
    """Get a PostgreSQL connection wrapped in a transaction (for FastAPI Depends).

    The transaction enables SAVEPOINT usage and provides request-level atomicity.
    """
    pool = await _get_pg_pool()
    conn = await pool.acquire()
    tr = conn.transaction()
    await tr.start()
    try:
        yield PgConnectionWrapper(conn)
        await tr.commit()
    except Exception:
        await tr.rollback()
        raise
    finally:
        await pool.release(conn)


@asynccontextmanager
async def get_db_ctx_pg():
    """PostgreSQL context manager for background tasks."""
    pool = await _get_pg_pool()
    conn = await pool.acquire()
    tr = conn.transaction()
    await tr.start()
    try:
        yield PgConnectionWrapper(conn)
        await tr.commit()
    except Exception:
        await tr.rollback()
        raise
    finally:
        await pool.release(conn)


# ── Backend selection ──
# When PostgreSQL is configured, override get_db/get_db_ctx
if _IS_PG:
    get_db = get_db_pg  # type: ignore
    get_db_ctx = get_db_ctx_pg  # type: ignore


# ── Schema ──

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS agents (
    agent_id TEXT PRIMARY KEY,
    node_id TEXT UNIQUE NOT NULL,
    display_name TEXT NOT NULL,
    public_key TEXT,
    wallet_address TEXT,
    api_key TEXT UNIQUE NOT NULL,
    skill_tags TEXT DEFAULT '[]',
    reputation_score REAL DEFAULT 0.0,
    status TEXT DEFAULT 'active' CHECK(status IN ('active','suspended','banned')),
    total_tasks_posted INTEGER DEFAULT 0,
    total_tasks_solved INTEGER DEFAULT 0,
    last_activity_reward TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS wallets (
    wallet_id TEXT PRIMARY KEY,
    agent_id TEXT UNIQUE NOT NULL REFERENCES agents(agent_id),
    balance INTEGER DEFAULT 0,
    frozen_balance INTEGER DEFAULT 0,
    lifetime_earned INTEGER DEFAULT 0,
    lifetime_spent INTEGER DEFAULT 0,
    last_faucet_claim TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS transactions (
    tx_id TEXT PRIMARY KEY,
    from_wallet_id TEXT REFERENCES wallets(wallet_id),
    to_wallet_id TEXT REFERENCES wallets(wallet_id),
    amount INTEGER NOT NULL CHECK(amount > 0),
    tx_type TEXT NOT NULL CHECK(tx_type IN (
        'mint','bounty_lock','bounty_release','bounty_refund',
        'reward','penalty','faucet','burn','claim_deposit','claim_refund',
        'activity_reward','skill_reward',
        'rally_stake','rally_refund','rally_bonus','referral_reward','escalation_mint'
    )),
    reference_id TEXT,
    reference_type TEXT,
    memo TEXT,
    settlement_batch_id TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tasks (
    task_id TEXT PRIMARY KEY,
    poster_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    title TEXT NOT NULL,
    description TEXT NOT NULL,
    category TEXT DEFAULT 'general',
    tags TEXT DEFAULT '[]',
    difficulty TEXT DEFAULT 'medium' CHECK(difficulty IN ('easy','medium','hard','expert')),
    bounty_amount INTEGER NOT NULL CHECK(bounty_amount > 0),
    estimated_self_cost INTEGER,
    status TEXT DEFAULT 'open' CHECK(status IN (
        'open','claimed','in_review','completed','cancelled','expired'
    )),
    max_solvers INTEGER DEFAULT 5,
    deadline TEXT,
    winning_submission_id TEXT,
    context TEXT DEFAULT '{}',
    -- Collaboration / decomposition fields
    parent_task_id TEXT REFERENCES tasks(task_id),
    task_type TEXT DEFAULT 'standalone' CHECK(task_type IN ('standalone','parent','subtask')),
    weight_pct INTEGER DEFAULT 100 CHECK(weight_pct >= 0 AND weight_pct <= 100),
    sequence_order INTEGER DEFAULT 0,
    base_bounty_amount INTEGER,
    escalation_level REAL DEFAULT 1.0,
    first_claimed_at TEXT,
    failed_claim_count INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS task_claims (
    claim_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    solver_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    status TEXT DEFAULT 'active' CHECK(status IN (
        'active','withdrawn','submitted','won','lost'
    )),
    deposit_tx_id TEXT REFERENCES transactions(tx_id),
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id, solver_agent_id)
);

CREATE TABLE IF NOT EXISTS submissions (
    submission_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    claim_id TEXT NOT NULL REFERENCES task_claims(claim_id),
    solver_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    skill_recipe TEXT DEFAULT '{}',
    summary TEXT NOT NULL,
    confidence_score REAL DEFAULT 0.0,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
    poster_feedback TEXT,
    poster_rating INTEGER CHECK(poster_rating IS NULL OR (poster_rating >= 1 AND poster_rating <= 5)),
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS skills (
    skill_id TEXT PRIMARY KEY,
    author_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    name TEXT NOT NULL,
    version TEXT DEFAULT '1.0.0',
    title TEXT NOT NULL,
    description TEXT,
    category TEXT DEFAULT 'general',
    tags TEXT DEFAULT '[]',
    recipe TEXT DEFAULT '{}',
    source_task_id TEXT REFERENCES tasks(task_id),
    is_public INTEGER DEFAULT 1,
    fork_of TEXT REFERENCES skills(skill_id),
    usage_count INTEGER DEFAULT 0,
    avg_rating REAL DEFAULT 0.0,
    reward_granted INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(name, author_agent_id, version)
);

CREATE TABLE IF NOT EXISTS skill_installs (
    install_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    skill_id TEXT NOT NULL REFERENCES skills(skill_id),
    installed_version TEXT NOT NULL,
    times_used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(agent_id, skill_id)
);

CREATE TABLE IF NOT EXISTS skill_ratings (
    rating_id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL REFERENCES skills(skill_id),
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    score INTEGER NOT NULL CHECK(score >= 1 AND score <= 5),
    comment TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(skill_id, agent_id)
);

CREATE TABLE IF NOT EXISTS ratings (
    rating_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    rater_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    ratee_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    rating_type TEXT NOT NULL CHECK(rating_type IN ('poster_rates_solver','solver_rates_poster')),
    score INTEGER NOT NULL CHECK(score >= 1 AND score <= 5),
    comment TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id, rater_agent_id, rating_type)
);

CREATE TABLE IF NOT EXISTS disputes (
    dispute_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    initiator_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    respondent_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    reason TEXT NOT NULL,
    evidence TEXT DEFAULT '{}',
    status TEXT DEFAULT 'open' CHECK(status IN (
        'open','under_review','resolved_initiator','resolved_respondent','dismissed'
    )),
    resolution_method TEXT CHECK(resolution_method IN ('auto','community_vote','admin')),
    resolution_comment TEXT,
    resolved_at TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS dispute_votes (
    vote_id TEXT PRIMARY KEY,
    dispute_id TEXT NOT NULL REFERENCES disputes(dispute_id),
    voter_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    vote TEXT NOT NULL CHECK(vote IN ('initiator','respondent','dismiss')),
    comment TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(dispute_id, voter_agent_id)
);

CREATE TABLE IF NOT EXISTS decomposition_proposals (
    proposal_id TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    proposer_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    subtasks_json TEXT NOT NULL,
    status TEXT DEFAULT 'proposed' CHECK(status IN ('proposed','active','rejected')),
    endorsement_score REAL DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS proposal_endorsements (
    endorsement_id TEXT PRIMARY KEY,
    proposal_id TEXT NOT NULL REFERENCES decomposition_proposals(proposal_id),
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(proposal_id, agent_id)
);

CREATE TABLE IF NOT EXISTS cross_reviews (
    review_id TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    reviewer_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    reviewed_subtask_id TEXT NOT NULL REFERENCES tasks(task_id),
    score INTEGER NOT NULL CHECK(score >= 1 AND score <= 5),
    comment TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(reviewer_agent_id, reviewed_subtask_id)
);

CREATE TABLE IF NOT EXISTS task_rallies (
    rally_id TEXT PRIMARY KEY,
    parent_task_id TEXT NOT NULL REFERENCES tasks(task_id),
    target_subtask_id TEXT NOT NULL REFERENCES tasks(task_id),
    supporter_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    stake_amount INTEGER NOT NULL CHECK(stake_amount > 0),
    stake_tx_id TEXT REFERENCES transactions(tx_id),
    status TEXT DEFAULT 'active' CHECK(status IN ('active','refunded','rewarded')),
    message TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(target_subtask_id, supporter_agent_id)
);

CREATE TABLE IF NOT EXISTS task_referrals (
    referral_id TEXT PRIMARY KEY,
    task_id TEXT NOT NULL REFERENCES tasks(task_id),
    referrer_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    referred_agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    reward_amount INTEGER DEFAULT 0,
    reward_tx_id TEXT REFERENCES transactions(tx_id),
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending','claimed','rewarded','expired')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(task_id, referrer_agent_id, referred_agent_id)
);

CREATE TABLE IF NOT EXISTS bridge_requests (
    request_id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL REFERENCES agents(agent_id),
    direction TEXT NOT NULL CHECK(direction IN ('deposit', 'withdraw')),
    amount INTEGER NOT NULL CHECK(amount > 0),
    wallet_address TEXT NOT NULL,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'processing', 'completed', 'failed')),
    onchain_tx_hash TEXT,
    error_message TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    completed_at TEXT
);

CREATE TABLE IF NOT EXISTS settlement_batches (
    batch_id TEXT PRIMARY KEY,
    merkle_root TEXT NOT NULL,
    tx_count INTEGER NOT NULL,
    start_tx_id TEXT,
    end_tx_id TEXT,
    onchain_tx_hash TEXT,
    status TEXT DEFAULT 'pending' CHECK(status IN ('pending', 'submitted', 'confirmed', 'failed')),
    created_at TEXT DEFAULT (datetime('now')),
    confirmed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_agents_node_id ON agents(node_id);
CREATE INDEX IF NOT EXISTS idx_agents_api_key ON agents(api_key);
CREATE INDEX IF NOT EXISTS idx_wallets_agent_id ON wallets(agent_id);
CREATE INDEX IF NOT EXISTS idx_transactions_from ON transactions(from_wallet_id);
CREATE INDEX IF NOT EXISTS idx_transactions_to ON transactions(to_wallet_id);
CREATE INDEX IF NOT EXISTS idx_transactions_ref ON transactions(reference_id, reference_type);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);
CREATE INDEX IF NOT EXISTS idx_tasks_poster ON tasks(poster_agent_id);
CREATE INDEX IF NOT EXISTS idx_tasks_category ON tasks(category);
CREATE INDEX IF NOT EXISTS idx_task_claims_task ON task_claims(task_id);
CREATE INDEX IF NOT EXISTS idx_task_claims_solver ON task_claims(solver_agent_id);
CREATE INDEX IF NOT EXISTS idx_submissions_task ON submissions(task_id);
CREATE INDEX IF NOT EXISTS idx_skills_author ON skills(author_agent_id);
CREATE INDEX IF NOT EXISTS idx_skills_category ON skills(category);
CREATE INDEX IF NOT EXISTS idx_skills_public ON skills(is_public);
CREATE INDEX IF NOT EXISTS idx_skill_installs_agent ON skill_installs(agent_id);
CREATE INDEX IF NOT EXISTS idx_skill_installs_skill ON skill_installs(skill_id);
CREATE INDEX IF NOT EXISTS idx_skill_ratings_skill ON skill_ratings(skill_id);
CREATE INDEX IF NOT EXISTS idx_skill_ratings_agent ON skill_ratings(agent_id);
CREATE INDEX IF NOT EXISTS idx_ratings_task ON ratings(task_id);
CREATE INDEX IF NOT EXISTS idx_ratings_ratee ON ratings(ratee_agent_id);
CREATE INDEX IF NOT EXISTS idx_disputes_task ON disputes(task_id);
CREATE INDEX IF NOT EXISTS idx_disputes_status ON disputes(status);
CREATE INDEX IF NOT EXISTS idx_dispute_votes_dispute ON dispute_votes(dispute_id);
CREATE INDEX IF NOT EXISTS idx_proposals_parent ON decomposition_proposals(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_proposals_status ON decomposition_proposals(status);
CREATE INDEX IF NOT EXISTS idx_endorsements_proposal ON proposal_endorsements(proposal_id);
CREATE INDEX IF NOT EXISTS idx_cross_reviews_parent ON cross_reviews(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_cross_reviews_subtask ON cross_reviews(reviewed_subtask_id);
CREATE INDEX IF NOT EXISTS idx_tasks_parent ON tasks(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_tasks_type ON tasks(task_type);
CREATE INDEX IF NOT EXISTS idx_rallies_parent ON task_rallies(parent_task_id);
CREATE INDEX IF NOT EXISTS idx_rallies_target ON task_rallies(target_subtask_id);
CREATE INDEX IF NOT EXISTS idx_rallies_supporter ON task_rallies(supporter_agent_id);
CREATE INDEX IF NOT EXISTS idx_referrals_task ON task_referrals(task_id);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON task_referrals(referrer_agent_id);
CREATE INDEX IF NOT EXISTS idx_bridge_requests_agent ON bridge_requests(agent_id);
CREATE INDEX IF NOT EXISTS idx_bridge_requests_status ON bridge_requests(status);
CREATE INDEX IF NOT EXISTS idx_transactions_batch ON transactions(settlement_batch_id);
CREATE INDEX IF NOT EXISTS idx_settlement_batches_status ON settlement_batches(status);
"""


async def init_db():
    """Initialize database schema."""
    if _IS_PG:
        await _init_db_pg()
    else:
        await _init_db_sqlite()


async def _init_db_sqlite():
    _ensure_dir()
    async with aiosqlite.connect(_DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL")
        await db.execute("PRAGMA foreign_keys=ON")
        await db.executescript(SCHEMA_SQL)
        await db.commit()


async def _init_db_pg():
    """Initialize PostgreSQL schema using translated SQL."""
    import re
    pool = await _get_pg_pool()
    schema = SCHEMA_SQL
    # Translate SQLite defaults to PostgreSQL
    schema = schema.replace("datetime('now')", "NOW()")

    # Convert timestamp TEXT columns to TIMESTAMPTZ for proper PG type handling
    # 1. Columns with NOW() default: TEXT DEFAULT (NOW()) → TIMESTAMPTZ DEFAULT NOW()
    schema = re.sub(
        r'(\w+)\s+TEXT\s+DEFAULT\s+\(NOW\(\)\)',
        r'\1 TIMESTAMPTZ DEFAULT NOW()',
        schema,
    )
    # 2. Known timestamp columns without defaults
    for col in (
        'last_activity_reward', 'last_faucet_claim', 'deadline',
        'first_claimed_at', 'resolved_at', 'completed_at', 'confirmed_at',
    ):
        schema = re.sub(rf'({col})\s+TEXT\b', rf'\1 TIMESTAMPTZ', schema)

    # SQLite INTEGER booleans → PG keeps INTEGER (compatible)
    # Split into individual statements — asyncpg can't run multiple in one call
    statements = [s.strip() for s in schema.split(";") if s.strip()]

    async with pool.acquire() as conn:
        # Check if schema needs recreation (e.g. TEXT→TIMESTAMPTZ migration)
        col_type = await conn.fetchval(
            "SELECT data_type FROM information_schema.columns "
            "WHERE table_name='agents' AND column_name='created_at'"
        )
        if col_type and col_type == 'text':
            # Drop all tables to recreate with correct types
            tables = await conn.fetch(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
            async with conn.transaction():
                for t in tables:
                    await conn.execute(f'DROP TABLE IF EXISTS "{t["tablename"]}" CASCADE')

        async with conn.transaction():
            for stmt in statements:
                await conn.execute(stmt)
