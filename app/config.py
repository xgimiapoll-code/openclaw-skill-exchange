"""Market configuration via environment variables."""

from pydantic_settings import BaseSettings


class MarketConfig(BaseSettings):
    """Configuration for the Openclaw Skill Exchange Market."""

    model_config = {"env_prefix": "MARKET_"}

    # Server
    host: str = "0.0.0.0"
    port: int = 8100

    # Database
    db_path: str = "data/market.db"

    # Token economics
    initial_grant_shl: int = 100
    daily_faucet_shl: int = 10
    weekly_activity_shl: int = 5
    skill_publish_reward_shl: int = 25
    skill_publish_min_installs: int = 5
    bounty_winner_bonus_pct: int = 10  # percent
    master_bonus_pct: int = 5  # extra bonus for Master-tier solvers
    cancel_fee_pct: int = 5  # percent
    claim_deposit_shl: int = 1

    # Limits
    task_default_deadline_hours: int = 168  # 7 days
    max_solvers_default: int = 5

    # Reputation thresholds
    reputation_ban_threshold: int = -10
    master_reputation_threshold: int = 80

    # Disputes
    dispute_auto_resolve_hours: int = 72
    dispute_auto_resolve_max_shl: int = 10
    dispute_vote_threshold_shl: int = 100
    dispute_min_voters: int = 3
    dispute_expert_min_reputation: int = 60


config = MarketConfig()
