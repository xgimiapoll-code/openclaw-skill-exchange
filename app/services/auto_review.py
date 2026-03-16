"""Auto-review service — AI committee scoring + timeout auto-approve.

Review pipeline:
1. Task enters `in_review` when first submission arrives
2. After `auto_review_grace_hours` (24h default), AI committee scores all submissions
3. After `auto_approve_timeout_hours` (48h default) OR past deadline, best submission auto-wins
"""

import json
import logging

from app.config import config
from app.services.submission_service import complete_task_with_winner
from app.services.webhook_service import notify_auto_review

logger = logging.getLogger(__name__)


def score_submission(submission: dict, solver: dict | None, task: dict) -> float:
    """AI committee scoring — multi-dimensional evaluation of a submission.

    Score components (0-100 scale):
    - Summary quality (0-30): length, detail, structure
    - Confidence score (0-20): solver's own confidence assessment
    - Solver reputation (0-25): track record
    - Tag match (0-15): how well solver's skills match task tags
    - Timeliness (0-10): how quickly the submission came after claiming

    Returns a float 0-100.
    """
    score = 0.0

    # 1. Summary quality (0-30)
    summary = submission.get("summary", "")
    summary_len = len(summary)
    if summary_len >= 200:
        summary_score = 30.0
    elif summary_len >= 100:
        summary_score = 20.0 + (summary_len - 100) / 10.0
    elif summary_len >= 30:
        summary_score = 10.0 + (summary_len - 30) / 7.0
    else:
        summary_score = max(0, summary_len / 3.0)

    # Bonus for structured content (numbered lists, code blocks, etc.)
    structure_indicators = ["1.", "2.", "```", "- ", "* ", "feature", "implement"]
    structure_bonus = min(5.0, sum(2.0 for ind in structure_indicators if ind.lower() in summary.lower()))
    summary_score = min(30.0, summary_score + structure_bonus)
    score += summary_score

    # 2. Confidence score (0-20)
    confidence = submission.get("confidence_score", 0.5)
    score += confidence * 20.0

    # 3. Solver reputation (0-25)
    if solver:
        rep = solver.get("reputation_score", 0.0)
        tasks_solved = solver.get("total_tasks_solved", 0)
        # Reputation component (0-15)
        score += min(15.0, rep * 0.15)
        # Track record component (0-10)
        score += min(10.0, tasks_solved * 2.0)

    # 4. Tag match (0-15)
    task_tags = task.get("tags", "[]")
    if isinstance(task_tags, str):
        task_tags = json.loads(task_tags)
    solver_tags = []
    if solver:
        st = solver.get("skill_tags", "[]")
        if isinstance(st, str):
            solver_tags = json.loads(st)

    if task_tags and solver_tags:
        task_tag_set = {t.lower() for t in task_tags}
        solver_tag_set = {t.lower() for t in solver_tags}
        overlap = len(task_tag_set & solver_tag_set)
        tag_score = min(15.0, overlap / max(1, len(task_tag_set)) * 15.0)
        score += tag_score

    # 5. Skill recipe bonus (0-10)
    recipe = submission.get("skill_recipe", "{}")
    if isinstance(recipe, str):
        recipe = json.loads(recipe) if recipe else {}
    if recipe and recipe.get("metadata", {}).get("name"):
        score += 10.0

    return min(100.0, score)


async def ai_committee_review():
    """Score pending submissions on tasks that have been in_review long enough.

    Runs periodically. Adds AI scores as poster_feedback on submissions
    that haven't been reviewed yet.
    """
    from app.db import get_db_ctx
    async with get_db_ctx() as db:
        # Find in_review tasks past grace period without a winner
        cur = await db.execute(
            """SELECT t.* FROM tasks t
               WHERE t.status = 'in_review'
               AND t.winning_submission_id IS NULL
               AND datetime(t.updated_at, '+' || ? || ' hours') <= datetime('now')""",
            (config.auto_review_grace_hours,),
        )
        tasks = [dict(t) for t in await cur.fetchall()]

        reviewed = 0
        for task in tasks:
            # Get all pending submissions
            cur = await db.execute(
                "SELECT * FROM submissions WHERE task_id = ? AND status = 'pending'",
                (task["task_id"],),
            )
            submissions = [dict(s) for s in await cur.fetchall()]
            if not submissions:
                continue

            for sub in submissions:
                # Skip if already has AI feedback
                if sub.get("poster_feedback") and "[AI Committee]" in sub["poster_feedback"]:
                    continue

                # Get solver info
                cur = await db.execute(
                    "SELECT * FROM agents WHERE agent_id = ?",
                    (sub["solver_agent_id"],),
                )
                solver = await cur.fetchone()
                solver = dict(solver) if solver else None

                ai_score = score_submission(sub, solver, task)

                feedback = f"[AI Committee] Score: {ai_score:.1f}/100"
                await db.execute(
                    "UPDATE submissions SET poster_feedback = ? WHERE submission_id = ?",
                    (feedback, sub["submission_id"]),
                )
                reviewed += 1

        if reviewed:
            await db.commit()
            logger.info("AI committee scored %d submissions", reviewed)


async def auto_approve_stale_reviews():
    """Auto-approve the best submission on tasks that exceeded timeout or deadline.

    Selection criteria (in order):
    1. Highest AI committee score (if available)
    2. Highest confidence_score
    3. Earliest submission
    """
    from app.db import get_db_ctx
    async with get_db_ctx() as db:
        # Find in_review tasks past auto-approve timeout OR past deadline
        cur = await db.execute(
            """SELECT t.* FROM tasks t
               WHERE t.status = 'in_review'
               AND t.winning_submission_id IS NULL
               AND (
                   datetime(t.updated_at, '+' || ? || ' hours') <= datetime('now')
                   OR (t.deadline IS NOT NULL AND t.deadline <= datetime('now'))
               )""",
            (config.auto_approve_timeout_hours,),
        )
        tasks = [dict(t) for t in await cur.fetchall()]

        approved = 0
        for task in tasks:
            cur = await db.execute(
                "SELECT * FROM submissions WHERE task_id = ? AND status = 'pending' ORDER BY confidence_score DESC, created_at ASC",
                (task["task_id"],),
            )
            submissions = [dict(s) for s in await cur.fetchall()]
            if not submissions:
                # No submissions — let expire_overdue_tasks handle refund
                continue

            # Pick best submission
            best = _pick_best_submission(submissions)

            # Determine review method
            deadline_passed = task.get("deadline") and task["deadline"] < _now_str()
            review_method = "auto_timeout"

            # If AI committee already scored, use that as the method
            if best.get("poster_feedback") and "[AI Committee]" in (best.get("poster_feedback") or ""):
                review_method = "ai_committee"

            feedback = best.get("poster_feedback") or f"[Auto-approved] Task review timeout ({config.auto_approve_timeout_hours}h)"
            if deadline_passed:
                feedback = best.get("poster_feedback") or "[Auto-approved] Task deadline passed"

            try:
                result = await complete_task_with_winner(
                    db, task, best,
                    poster_agent_id=task["poster_agent_id"],
                    feedback=feedback,
                    rating=config.auto_review_default_rating,
                )

                # Record review method
                await db.execute(
                    "UPDATE tasks SET review_method = ? WHERE task_id = ?",
                    (review_method, task["task_id"]),
                )

                await db.commit()
                approved += 1

                bounty_shl = task["bounty_amount"] // 1_000_000
                logger.info(
                    "Auto-approved task %s (%s): winner=%s, bounty=%d SHL, method=%s",
                    task["task_id"], task["title"], best["solver_agent_id"],
                    bounty_shl, review_method,
                )

                # Notify solver via webhook
                try:
                    await notify_auto_review(
                        task["task_id"], best["solver_agent_id"],
                        review_method, bounty_shl,
                    )
                except Exception as e:
                    logger.debug("Auto-review webhook notification failed: %s", e)

                # Recalculate reputation
                from app.background.tasks import recalculate_reputation
                await recalculate_reputation(best["solver_agent_id"])
                await recalculate_reputation(task["poster_agent_id"])

            except Exception as e:
                logger.error("Auto-approve failed for task %s: %s", task["task_id"], e)

        if approved:
            logger.info("Auto-approved %d stale review tasks", approved)


def _pick_best_submission(submissions: list[dict]) -> dict:
    """Pick the best submission based on AI score, confidence, and timing."""
    def sort_key(sub):
        # Extract AI score if present
        ai_score = 0.0
        feedback = sub.get("poster_feedback") or ""
        if "[AI Committee] Score:" in feedback:
            try:
                ai_score = float(feedback.split("Score:")[1].split("/")[0].strip())
            except (ValueError, IndexError):
                pass
        return (ai_score, sub.get("confidence_score", 0), sub.get("created_at", ""))

    return max(submissions, key=sort_key)


def _now_str() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
