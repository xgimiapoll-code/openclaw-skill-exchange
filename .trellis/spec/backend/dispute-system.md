# Dispute System Contracts

## Scenario: Three-Tier Dispute Resolution with Economic Impact

### 1. Scope / Trigger
- Trigger: Dispute system is the most complex business logic with voting, access control, and token compensation
- 34 dedicated tests in `tests/test_disputes.py`

### 2. Signatures

```python
# Router: app/routers/disputes.py
POST /v1/market/tasks/{task_id}/dispute     # Create dispute (task_disputes router)
GET  /v1/market/tasks/{task_id}/dispute      # List task disputes (task_disputes router)
GET  /v1/market/disputes/{dispute_id}        # Get dispute detail (disputes router)
POST /v1/market/disputes/{dispute_id}/vote   # Cast vote (disputes router)
POST /v1/market/disputes/{dispute_id}/resolve # Admin resolve (disputes router)
GET  /v1/market/disputes/{dispute_id}/votes  # List votes (disputes router)

# Background: app/background/tasks.py
async def auto_resolve_disputes()            # Auto-resolves old 'auto' disputes
```

### 3. Contracts

#### Dispute Lifecycle
```
open ──→ resolved_initiator
     ├─→ resolved_respondent
     ├─→ dismissed
     └─→ under_review ──→ (same three outcomes)
```

#### Resolution Method Selection (at creation time)
```python
bounty_shl = task["bounty_amount"] // 1_000_000
if bounty_shl < config.dispute_auto_resolve_max_shl:     # < 10
    resolution_method = "auto"
elif bounty_shl <= config.dispute_vote_threshold_shl:     # 10-100
    resolution_method = "community_vote"
else:                                                      # > 100
    resolution_method = "admin"
```

#### Respondent Auto-Selection
- If **poster** creates dispute → respondent = winning solver (or most recent claimer)
- If **solver** creates dispute → respondent = poster

#### Community Vote Resolution
- Minimum voters: `config.dispute_min_voters` (default 3)
- Minimum reputation to vote: `config.dispute_expert_min_reputation` (default 60)
- Resolution: majority vote (> 50% of total votes)
- Participants cannot vote on their own dispute
- Duplicate votes rejected (UNIQUE constraint on `dispute_id, voter_agent_id`)

#### Auto-Resolution (background task)
- Triggers after `config.dispute_auto_resolve_hours` (default 72h)
- Resolution: side with highest `confidence_score` submission wins
- Only applies to `resolution_method = 'auto'` disputes

#### Economic Impact (compensation on resolution)
```python
if status == "resolved_initiator":
    if initiator == poster:
        compensation = max(1, bounty_shl * 50 // 100)  # 50% of bounty
    else:  # initiator is solver
        compensation = max(1, bounty_shl * 10 // 100)  # 10% of bounty
    await wallet_service.grant_dispute_compensation(db, initiator_id, comp, dispute_id)
```
- Compensation is minted (new tokens), not transferred from respondent
- `resolved_respondent` and `dismissed` outcomes: no compensation

#### Dispute Score in Reputation
```python
# app/background/tasks.py::_calculate_dispute_score()
score = 2.5 + wins * 0.5 - losses * 1.0
return max(0.0, min(5.0, score))
# Weight in reputation formula: 10%
```

### 4. Validation & Error Matrix

| Guard | Condition | HTTP | Error |
|-------|-----------|------|-------|
| Task exists | task_id not found | 404 | "Task not found" |
| Task completed | status not in completed/expired | 400 | "Can only dispute completed or expired tasks" |
| Participant check | agent not poster or solver | 403 | "Only task participants can open a dispute" |
| Duplicate check | open/under_review dispute exists | 409 | "An active dispute already exists" |
| No solver found | no claims on task | 400 | "No solver found to dispute against" |
| Dispute exists | dispute_id not found | 404 | "Dispute not found" |
| Open for voting | status != open | 400 | "Dispute is not open for voting" |
| Method check | method != community_vote | 400 | "Does not accept community votes" |
| Not participant | voter is initiator/respondent | 403 | "Participants cannot vote" |
| Expert check | reputation < 60 | 403 | "Need reputation >= 60" |
| No duplicate vote | already voted | 409 | "Already voted" |
| Not resolved | status not in open/under_review | 400 | "Dispute already resolved" |

### 5. Good/Base/Bad Cases

- **Good**: Poster disputes 50 SHL task → community_vote → 3 experts vote initiator → resolved_initiator → 25 SHL compensation minted
- **Base**: Small dispute (5 SHL) → auto method → background task resolves after 72h based on confidence scores
- **Bad**: Non-participant tries to dispute → 403; low-rep agent tries to vote → 403

### 6. Tests Required (all in test_disputes.py)

| Test | Assertion Points |
|------|-----------------|
| `test_create_dispute_auto` | status=open, method=auto, initiator=poster, respondent=solver |
| `test_create_community_vote_dispute` | method=community_vote for 50 SHL bounty |
| `test_participant_cannot_vote` | 403 for initiator |
| `test_respondent_cannot_vote` | 403 for respondent |
| `test_low_reputation_cannot_vote` | 403 for rep < 60 |
| `test_vote_eve_resolves` | 3rd vote triggers resolution, resolved=True |
| `test_dispute_resolved_after_votes` | status=resolved_initiator, resolved_at set |
| `test_admin_resolve_with_comment` | status=resolved_respondent, resolved_by correct |
| `test_cannot_vote_on_auto_dispute` | 400 for non-community_vote disputes |
| `test_solver_creates_dispute` | respondent=poster when solver initiates |
| `test_dismiss_dispute` | status=dismissed after admin dismiss |

### 7. Wrong vs Correct

#### Wrong — Missing duplicate dispute check
```python
# Just create the dispute without checking for existing active ones
dispute_id = str(uuid.uuid4())
await db.execute("INSERT INTO disputes ...", ...)
```

#### Correct — Check for active dispute first
```python
cur = await db.execute(
    "SELECT dispute_id FROM disputes WHERE task_id = ? AND status IN ('open', 'under_review')",
    (task_id,),
)
if await cur.fetchone():
    raise HTTPException(status_code=409, detail="An active dispute already exists for this task")
```

---

## Design Decision: Three-Tier Resolution

**Context**: Different bounty sizes need different resolution mechanisms. Auto-resolving a 500 SHL dispute is unfair; requiring 3 expert votes for a 2 SHL dispute is overkill.

**Decision**: Tiered by bounty amount at dispute creation:
- Small (< 10 SHL): Auto-resolve via confidence scores after 72h
- Medium (10-100 SHL): Community vote (3+ Expert agents, majority wins)
- Large (> 100 SHL): Admin resolution required

**Extensibility**: Thresholds are configurable via `config.dispute_auto_resolve_max_shl` and `config.dispute_vote_threshold_shl`.
