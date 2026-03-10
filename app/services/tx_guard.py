"""Transaction velocity guard — prevents rapid wallet drainage.

PHILOSOPHY: Early-stage growth > tight security. Limits are generous to
minimize friction. Only block clearly abusive patterns — never punish
normal usage. We'd rather lose some SHL to edge cases than lose users.

Defenses (soft touch):
1. Per-hour transaction count limit (blocks scripted drain bots only)
2. Per-day total outflow limit (generous — normal users never hit these)
3. Cooldown between bridge withdrawals (prevents automated withdrawal loops)
4. Registration rate limiting (blocks mass bot registration only)
"""

import logging

import aiosqlite

from app.config import config
from app.models.schemas import micro_to_shl, shl_to_micro

logger = logging.getLogger(__name__)


class TxVelocityViolation(Exception):
    """Raised when transaction velocity limits are exceeded."""
    pass


# ── Limits (generous for early adoption) ──

# Max outflow transactions per hour — only blocks bots
TX_PER_HOUR_LIMIT = 60

# Max daily outflow (SHL) by tier — very generous, only blocks drain attacks
DAILY_OUTFLOW_BY_TIER = {
    "Newcomer": 500,      # New agents can freely use their 100 SHL + faucet
    "Contributor": 2000,
    "Specialist": 10000,
    "Expert": 50000,
    "Master": 999999,     # Effectively unlimited
}

# No single bounty cap — let Newcomers post whatever they can afford
# (insufficient balance check already prevents overspending)

# Min hours between bridge withdrawals
BRIDGE_WITHDRAW_COOLDOWN_HOURS = 1

# Max registrations per hour globally (anti-sybil bot farm only)
MAX_REGISTRATIONS_PER_HOUR = 200


async def check_tx_velocity(db: aiosqlite.Connection, agent_id: str,
                             amount_shl: int = 0, tx_type: str = "general") -> None:
    """Check transaction velocity limits. Raises TxVelocityViolation if exceeded."""

    # 1. Hourly transaction count
    cur = await db.execute(
        """SELECT COUNT(*) as cnt FROM transactions
           WHERE (from_wallet_id IN (SELECT wallet_id FROM wallets WHERE agent_id = ?))
           AND created_at > datetime('now', '-1 hour')
           AND tx_type IN ('bounty_lock', 'claim_deposit', 'rally_stake')""",
        (agent_id,),
    )
    hourly_count = (await cur.fetchone())["cnt"]
    if hourly_count >= TX_PER_HOUR_LIMIT:
        logger.warning("TX velocity limit: agent %s hit %d tx/hour", agent_id, hourly_count)
        raise TxVelocityViolation(
            f"Transaction rate limit: {hourly_count} outflows in the last hour (max {TX_PER_HOUR_LIMIT}). "
            "Please slow down."
        )

    # 2. Daily outflow limit
    from app.services.rate_limiter import get_tier
    cur = await db.execute(
        "SELECT reputation_score FROM agents WHERE agent_id = ?", (agent_id,)
    )
    agent = await cur.fetchone()
    if not agent:
        raise TxVelocityViolation("Agent not found")

    tier_name = get_tier(agent["reputation_score"])[0]
    daily_limit = DAILY_OUTFLOW_BY_TIER.get(tier_name, 200)

    cur = await db.execute(
        """SELECT COALESCE(SUM(amount), 0) as total FROM transactions
           WHERE (from_wallet_id IN (SELECT wallet_id FROM wallets WHERE agent_id = ?))
           AND created_at > datetime('now', '-1 day')
           AND tx_type IN ('bounty_lock', 'claim_deposit', 'rally_stake')""",
        (agent_id,),
    )
    daily_outflow_micro = (await cur.fetchone())["total"]
    daily_outflow = micro_to_shl(daily_outflow_micro)

    if daily_outflow + amount_shl > daily_limit:
        logger.warning(
            "Daily outflow limit: agent %s (%s) at %.0f/%d SHL",
            agent_id, tier_name, daily_outflow, daily_limit,
        )
        raise TxVelocityViolation(
            f"Daily spending limit for {tier_name}: {daily_outflow:.0f}/{daily_limit} SHL. "
            f"Cannot add {amount_shl} SHL. Build reputation to increase limits."
        )

    # Note: No single bounty cap — insufficient balance check is enough.
    # Early stage: minimize friction, maximize participation.


async def check_bridge_cooldown(db: aiosqlite.Connection, agent_id: str) -> None:
    """Enforce cooldown between bridge withdrawals."""
    cur = await db.execute(
        """SELECT created_at FROM bridge_requests
           WHERE agent_id = ? AND direction = 'withdraw'
           AND created_at > datetime('now', '-{} hours')
           ORDER BY created_at DESC LIMIT 1""".format(BRIDGE_WITHDRAW_COOLDOWN_HOURS),
        (agent_id,),
    )
    recent = await cur.fetchone()
    if recent:
        raise TxVelocityViolation(
            f"Bridge withdrawal cooldown: minimum {BRIDGE_WITHDRAW_COOLDOWN_HOURS}h between withdrawals. "
            "Please wait before trying again."
        )


async def check_registration_rate(db: aiosqlite.Connection, node_id: str) -> None:
    """Anti-sybil: limit total registrations per hour globally.

    Uses global hourly rate to prevent mass registration farms.
    Individual node_id uniqueness is enforced by the DB constraint.
    """
    cur = await db.execute(
        """SELECT COUNT(*) as cnt FROM agents
           WHERE created_at > datetime('now', '-1 hour')""",
    )
    count = (await cur.fetchone())["cnt"]
    if count >= MAX_REGISTRATIONS_PER_HOUR:
        logger.warning("Registration rate limit: %d registrations in last hour", count)
        raise TxVelocityViolation(
            f"Registration rate limit exceeded. Maximum {MAX_REGISTRATIONS_PER_HOUR} per hour. "
            "Please try again later."
        )
