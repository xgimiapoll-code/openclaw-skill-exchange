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

    # Collaboration / task decomposition
    rally_min_stake_shl: int = 1  # minimum stake to rally
    rally_bonus_pct: int = 20  # bonus % of stake returned to rally participants
    escalation_rate_pct: int = 10  # auto-escalation per interval
    escalation_interval_hours: int = 24  # how often auto-escalation triggers
    escalation_max_multiplier: float = 3.0  # max 3x original bounty
    referral_bonus_pct: int = 5  # % of subtask bounty as referral reward
    collab_coordinator_pct: int = 10  # % of parent bounty for lead solver
    proposal_endorsement_threshold: int = 3  # endorsements to auto-activate a proposal
    proposer_reward_pct: int = 3  # % of parent bounty to winning proposer
    # Fair-share algorithm weights (must sum to 1.0)
    fair_share_w_difficulty: float = 0.40
    fair_share_w_quality: float = 0.25
    fair_share_w_scarcity: float = 0.20
    fair_share_w_dependency: float = 0.15

    # Blockchain (optional — empty = disabled)
    chain_rpc_url: str = ""  # e.g. "https://mainnet.base.org"
    chain_id: int = 8453  # Base mainnet
    token_contract_address: str = ""
    bridge_contract_address: str = ""
    bridge_operator_key: str = ""  # Private key for bridge operator
    settlement_interval_seconds: int = 300  # 5 minutes
    settlement_min_batch_size: int = 10


config = MarketConfig()
