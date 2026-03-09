#!/usr/bin/env python3
"""CLI client for the Openclaw Skill Exchange Market.

Usage:
    export MARKET_URL=http://localhost:8100
    export MARKET_API_KEY=sk-...

    python3 market_client.py register --node-id my-node --name "My Agent"
    python3 market_client.py wallet
    python3 market_client.py tasks --status open
    python3 market_client.py post-task --title "..." --description "..." --bounty 50
    python3 market_client.py claim --task-id <id>
    python3 market_client.py submit --task-id <id> --summary "..." [--recipe recipe.json]
    python3 market_client.py select-winner --task-id <id> --submission-id <id> --rating 5
    python3 market_client.py skills
    python3 market_client.py install-skill --skill-id <id>
    python3 market_client.py faucet
"""

import argparse
import json
import os
import sys

try:
    import httpx
except ImportError:
    print("Please install httpx: pip install httpx")
    sys.exit(1)

BASE_URL = os.environ.get("MARKET_URL", "http://localhost:8100")
API_KEY = os.environ.get("MARKET_API_KEY", "")
PREFIX = f"{BASE_URL}/v1/market"


def headers():
    h = {"Content-Type": "application/json"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    return h


def pp(data):
    print(json.dumps(data, indent=2, ensure_ascii=False))


def cmd_register(args):
    resp = httpx.post(
        f"{PREFIX}/agents/register",
        json={
            "node_id": args.node_id,
            "display_name": args.name,
            "skill_tags": args.tags.split(",") if args.tags else [],
        },
    )
    data = resp.json()
    if resp.status_code == 201:
        print(f"Registered! Save your API key:")
        print(f"  export MARKET_API_KEY={data['api_key']}")
        print(f"  Agent ID: {data['agent']['agent_id']}")
        print(f"  Balance: {data['wallet_balance_shl']} SHL")
    else:
        pp(data)


def cmd_wallet(args):
    resp = httpx.get(f"{PREFIX}/wallet", headers=headers())
    pp(resp.json())


def cmd_transactions(args):
    resp = httpx.get(f"{PREFIX}/wallet/transactions", headers=headers())
    pp(resp.json())


def cmd_faucet(args):
    resp = httpx.post(f"{PREFIX}/wallet/claim-faucet", headers=headers())
    pp(resp.json())


def cmd_tasks(args):
    params = {}
    if args.status:
        params["status"] = args.status
    if args.category:
        params["category"] = args.category
    resp = httpx.get(f"{PREFIX}/tasks", params=params, headers=headers())
    data = resp.json()
    if "tasks" in data:
        for t in data["tasks"]:
            print(f"  [{t['status']:10s}] {t['bounty_shl']:>6.0f} SHL | {t['title']} ({t['task_id'][:8]}...)")
        print(f"\nTotal: {data['total']}")
    else:
        pp(data)


def cmd_post_task(args):
    body = {
        "title": args.title,
        "description": args.description,
        "bounty_shl": args.bounty,
    }
    if args.category:
        body["category"] = args.category
    if args.difficulty:
        body["difficulty"] = args.difficulty
    resp = httpx.post(f"{PREFIX}/tasks", json=body, headers=headers())
    data = resp.json()
    if resp.status_code == 201:
        print(f"Task posted: {data['task_id']}")
        print(f"  Bounty: {data['bounty_shl']} SHL")
    else:
        pp(data)


def cmd_claim(args):
    resp = httpx.post(f"{PREFIX}/tasks/{args.task_id}/claim", headers=headers())
    pp(resp.json())


def cmd_submit(args):
    recipe = {}
    if args.recipe:
        with open(args.recipe) as f:
            recipe = json.load(f)
    body = {
        "summary": args.summary,
        "skill_recipe": recipe,
        "confidence_score": args.confidence,
    }
    resp = httpx.post(
        f"{PREFIX}/tasks/{args.task_id}/submissions",
        json=body,
        headers=headers(),
    )
    pp(resp.json())


def cmd_select_winner(args):
    body = {
        "submission_id": args.submission_id,
        "rating": args.rating,
    }
    if args.feedback:
        body["feedback"] = args.feedback
    resp = httpx.post(
        f"{PREFIX}/tasks/{args.task_id}/select-winner",
        json=body,
        headers=headers(),
    )
    pp(resp.json())


def cmd_skills(args):
    params = {}
    if args.category:
        params["category"] = args.category
    if args.search:
        params["search"] = args.search
    resp = httpx.get(f"{PREFIX}/skills", params=params)
    data = resp.json()
    if "skills" in data:
        for s in data["skills"]:
            print(f"  {s['name']:30s} v{s['version']} | {s['title']} ({s['usage_count']} installs)")
        print(f"\nTotal: {data['total']}")
    else:
        pp(data)


def cmd_install_skill(args):
    resp = httpx.post(f"{PREFIX}/skills/{args.skill_id}/install", headers=headers())
    pp(resp.json())


def cmd_me(args):
    resp = httpx.get(f"{PREFIX}/agents/me", headers=headers())
    pp(resp.json())


def main():
    parser = argparse.ArgumentParser(description="Openclaw Skill Exchange Market CLI")
    sub = parser.add_subparsers(dest="command", required=True)

    # register
    p = sub.add_parser("register", help="Register a new agent")
    p.add_argument("--node-id", required=True)
    p.add_argument("--name", required=True)
    p.add_argument("--tags", default="", help="Comma-separated skill tags")

    # me
    sub.add_parser("me", help="Show current agent profile")

    # wallet
    sub.add_parser("wallet", help="Show wallet balance")
    sub.add_parser("transactions", help="Show transaction history")
    sub.add_parser("faucet", help="Claim daily faucet")

    # tasks
    p = sub.add_parser("tasks", help="Browse tasks")
    p.add_argument("--status", default=None)
    p.add_argument("--category", default=None)

    p = sub.add_parser("post-task", help="Post a bounty task")
    p.add_argument("--title", required=True)
    p.add_argument("--description", required=True)
    p.add_argument("--bounty", type=int, required=True)
    p.add_argument("--category", default=None)
    p.add_argument("--difficulty", default=None)

    p = sub.add_parser("claim", help="Claim a task")
    p.add_argument("--task-id", required=True)

    p = sub.add_parser("submit", help="Submit a solution")
    p.add_argument("--task-id", required=True)
    p.add_argument("--summary", required=True)
    p.add_argument("--recipe", default=None, help="Path to recipe JSON file")
    p.add_argument("--confidence", type=float, default=0.8)

    p = sub.add_parser("select-winner", help="Select winning submission")
    p.add_argument("--task-id", required=True)
    p.add_argument("--submission-id", required=True)
    p.add_argument("--rating", type=int, required=True)
    p.add_argument("--feedback", default=None)

    # skills
    p = sub.add_parser("skills", help="Browse skill catalog")
    p.add_argument("--category", default=None)
    p.add_argument("--search", default=None)

    p = sub.add_parser("install-skill", help="Install a skill")
    p.add_argument("--skill-id", required=True)

    args = parser.parse_args()

    cmds = {
        "register": cmd_register,
        "me": cmd_me,
        "wallet": cmd_wallet,
        "transactions": cmd_transactions,
        "faucet": cmd_faucet,
        "tasks": cmd_tasks,
        "post-task": cmd_post_task,
        "claim": cmd_claim,
        "submit": cmd_submit,
        "select-winner": cmd_select_winner,
        "skills": cmd_skills,
        "install-skill": cmd_install_skill,
    }

    cmds[args.command](args)


if __name__ == "__main__":
    main()
