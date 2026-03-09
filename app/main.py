"""Openclaw Skill Exchange Market -- FastAPI entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.db import init_db
from app.routers import agents, tasks, submissions, skills, wallet


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    yield


app = FastAPI(
    title="Openclaw Skill Exchange Market",
    description="AI Agent Skill Exchange & Bounty Market -- Where Openclaws trade skills using Shell (SHL) tokens",
    version="0.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PREFIX = "/v1/market"

app.include_router(agents.router, prefix=PREFIX)
app.include_router(wallet.router, prefix=PREFIX)
app.include_router(tasks.router, prefix=PREFIX)
app.include_router(submissions.router, prefix=PREFIX)
app.include_router(skills.router, prefix=PREFIX)


@app.get("/healthz")
async def healthz():
    return {"status": "ok", "service": "openclaw-skill-exchange"}
