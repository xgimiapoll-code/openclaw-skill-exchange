"""Pydantic schemas for request/response validation."""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field


# ── Micro-SHL helpers ──

SHL_UNIT = 1_000_000  # 1 SHL = 1,000,000 micro-SHL


def shl_to_micro(shl: int) -> int:
    return shl * SHL_UNIT


def micro_to_shl(micro: int) -> float:
    return micro / SHL_UNIT


# ── Agent ──

class AgentRegister(BaseModel):
    node_id: str = Field(..., min_length=1, max_length=128)
    display_name: str = Field(..., min_length=1, max_length=64)
    public_key: str | None = None
    skill_tags: list[str] = Field(default_factory=list)


class AgentOut(BaseModel):
    agent_id: str
    node_id: str
    display_name: str
    skill_tags: list[str]
    reputation_score: float
    status: str
    total_tasks_posted: int
    total_tasks_solved: int
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> AgentOut:
        tags = row.get("skill_tags", "[]")
        if isinstance(tags, str):
            tags = json.loads(tags)
        return cls(
            agent_id=row["agent_id"],
            node_id=row["node_id"],
            display_name=row["display_name"],
            skill_tags=tags,
            reputation_score=row.get("reputation_score", 0.0),
            status=row.get("status", "active"),
            total_tasks_posted=row.get("total_tasks_posted", 0),
            total_tasks_solved=row.get("total_tasks_solved", 0),
            created_at=row.get("created_at", ""),
        )


class AgentRegisterOut(BaseModel):
    agent: AgentOut
    api_key: str
    wallet_balance_shl: float


# ── Wallet ──

class WalletOut(BaseModel):
    wallet_id: str
    agent_id: str
    balance_shl: float
    frozen_balance_shl: float
    lifetime_earned_shl: float
    lifetime_spent_shl: float

    @classmethod
    def from_row(cls, row: dict) -> WalletOut:
        return cls(
            wallet_id=row["wallet_id"],
            agent_id=row["agent_id"],
            balance_shl=micro_to_shl(row["balance"]),
            frozen_balance_shl=micro_to_shl(row["frozen_balance"]),
            lifetime_earned_shl=micro_to_shl(row["lifetime_earned"]),
            lifetime_spent_shl=micro_to_shl(row["lifetime_spent"]),
        )


class TransactionOut(BaseModel):
    tx_id: str
    from_wallet_id: str | None
    to_wallet_id: str | None
    amount_shl: float
    tx_type: str
    reference_id: str | None
    memo: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> TransactionOut:
        return cls(
            tx_id=row["tx_id"],
            from_wallet_id=row.get("from_wallet_id"),
            to_wallet_id=row.get("to_wallet_id"),
            amount_shl=micro_to_shl(row["amount"]),
            tx_type=row["tx_type"],
            reference_id=row.get("reference_id"),
            memo=row.get("memo"),
            created_at=row.get("created_at", ""),
        )


class FaucetOut(BaseModel):
    success: bool
    amount_shl: float
    new_balance_shl: float
    message: str


# ── Task ──

class TaskCreate(BaseModel):
    title: str = Field(..., min_length=1, max_length=256)
    description: str = Field(..., min_length=1)
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    difficulty: str = "medium"
    bounty_shl: int = Field(..., gt=0)
    estimated_self_cost_shl: int | None = None
    max_solvers: int = Field(default=5, ge=1, le=50)
    deadline_hours: int | None = None
    context: dict[str, Any] = Field(default_factory=dict)


class TaskOut(BaseModel):
    task_id: str
    poster_agent_id: str
    title: str
    description: str
    category: str
    tags: list[str]
    difficulty: str
    bounty_shl: float
    estimated_self_cost_shl: float | None
    status: str
    max_solvers: int
    deadline: str | None
    winning_submission_id: str | None
    claim_count: int = 0
    submission_count: int = 0
    created_at: str

    @classmethod
    def from_row(cls, row: dict, claim_count: int = 0, submission_count: int = 0) -> TaskOut:
        tags = row.get("tags", "[]")
        if isinstance(tags, str):
            tags = json.loads(tags)
        est = row.get("estimated_self_cost")
        return cls(
            task_id=row["task_id"],
            poster_agent_id=row["poster_agent_id"],
            title=row["title"],
            description=row["description"],
            category=row.get("category", "general"),
            tags=tags,
            difficulty=row.get("difficulty", "medium"),
            bounty_shl=micro_to_shl(row["bounty_amount"]),
            estimated_self_cost_shl=micro_to_shl(est) if est else None,
            status=row["status"],
            max_solvers=row.get("max_solvers", 5),
            deadline=row.get("deadline"),
            winning_submission_id=row.get("winning_submission_id"),
            claim_count=claim_count,
            submission_count=submission_count,
            created_at=row.get("created_at", ""),
        )


class TaskListOut(BaseModel):
    tasks: list[TaskOut]
    total: int
    page: int
    page_size: int


# ── Submission ──

class SubmissionCreate(BaseModel):
    summary: str = Field(..., min_length=1)
    skill_recipe: dict[str, Any] = Field(default_factory=dict)
    confidence_score: float = Field(default=0.5, ge=0.0, le=1.0)


class SubmissionOut(BaseModel):
    submission_id: str
    task_id: str
    claim_id: str
    solver_agent_id: str
    summary: str
    skill_recipe: dict[str, Any]
    confidence_score: float
    status: str
    poster_feedback: str | None
    poster_rating: int | None
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> SubmissionOut:
        recipe = row.get("skill_recipe", "{}")
        if isinstance(recipe, str):
            recipe = json.loads(recipe)
        return cls(
            submission_id=row["submission_id"],
            task_id=row["task_id"],
            claim_id=row["claim_id"],
            solver_agent_id=row["solver_agent_id"],
            summary=row["summary"],
            skill_recipe=recipe,
            confidence_score=row.get("confidence_score", 0.0),
            status=row.get("status", "pending"),
            poster_feedback=row.get("poster_feedback"),
            poster_rating=row.get("poster_rating"),
            created_at=row.get("created_at", ""),
        )


class SelectWinnerRequest(BaseModel):
    submission_id: str
    feedback: str | None = None
    rating: int = Field(..., ge=1, le=5)


class RateRequest(BaseModel):
    score: int = Field(..., ge=1, le=5)
    comment: str | None = None


# ── Skill ──

class SkillRecipeMetadata(BaseModel):
    name: str
    title: str | None = None
    description: str | None = None
    category: str | None = None
    tags: list[str] = Field(default_factory=list)
    difficulty: str | None = None


class SkillRecipeStep(BaseModel):
    step: int
    title: str
    action: str
    params: dict[str, Any] = Field(default_factory=dict)


class SkillRecipe(BaseModel):
    schema_version: str = "1.0.0"
    metadata: SkillRecipeMetadata
    steps: list[SkillRecipeStep] = Field(default_factory=list)


class SkillCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=128, pattern=r"^[a-z0-9][a-z0-9\-]*$")
    title: str = Field(..., min_length=1, max_length=256)
    description: str | None = None
    category: str = "general"
    tags: list[str] = Field(default_factory=list)
    recipe: dict[str, Any] = Field(default_factory=dict)
    is_public: bool = True
    version: str = "1.0.0"


class SkillOut(BaseModel):
    skill_id: str
    author_agent_id: str
    name: str
    version: str
    title: str
    description: str | None
    category: str
    tags: list[str]
    recipe: dict[str, Any]
    source_task_id: str | None
    is_public: bool
    fork_of: str | None
    usage_count: int
    avg_rating: float
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> SkillOut:
        tags = row.get("tags", "[]")
        if isinstance(tags, str):
            tags = json.loads(tags)
        recipe = row.get("recipe", "{}")
        if isinstance(recipe, str):
            recipe = json.loads(recipe)
        return cls(
            skill_id=row["skill_id"],
            author_agent_id=row["author_agent_id"],
            name=row["name"],
            version=row.get("version", "1.0.0"),
            title=row["title"],
            description=row.get("description"),
            category=row.get("category", "general"),
            tags=tags,
            recipe=recipe,
            source_task_id=row.get("source_task_id"),
            is_public=bool(row.get("is_public", 1)),
            fork_of=row.get("fork_of"),
            usage_count=row.get("usage_count", 0),
            avg_rating=row.get("avg_rating", 0.0),
            created_at=row.get("created_at", ""),
        )


class SkillInstallOut(BaseModel):
    install_id: str
    skill_id: str
    skill_name: str
    skill_title: str
    installed_version: str
    times_used: int
    created_at: str


class SkillListOut(BaseModel):
    skills: list[SkillOut]
    total: int
    page: int
    page_size: int


class SkillRateRequest(BaseModel):
    score: int = Field(..., ge=1, le=5)
    comment: str | None = None


# ── Dispute ──

class DisputeCreate(BaseModel):
    reason: str = Field(..., min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)


class DisputeOut(BaseModel):
    dispute_id: str
    task_id: str
    initiator_agent_id: str
    respondent_agent_id: str
    reason: str
    evidence: dict[str, Any]
    status: str
    resolution_method: str | None
    resolved_at: str | None
    created_at: str

    @classmethod
    def from_row(cls, row: dict) -> DisputeOut:
        evidence = row.get("evidence", "{}")
        if isinstance(evidence, str):
            evidence = json.loads(evidence)
        return cls(
            dispute_id=row["dispute_id"],
            task_id=row["task_id"],
            initiator_agent_id=row["initiator_agent_id"],
            respondent_agent_id=row["respondent_agent_id"],
            reason=row["reason"],
            evidence=evidence,
            status=row.get("status", "open"),
            resolution_method=row.get("resolution_method"),
            resolved_at=row.get("resolved_at"),
            created_at=row.get("created_at", ""),
        )


class DisputeResolveRequest(BaseModel):
    resolution: str = Field(..., pattern=r"^(initiator|respondent|dismiss)$")
    comment: str | None = None


class DisputeVoteRequest(BaseModel):
    vote: str = Field(..., pattern=r"^(initiator|respondent|dismiss)$")
    comment: str | None = None
