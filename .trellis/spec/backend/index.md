# Backend Code-Specs

## Spec Files

| File | Status | Covers |
|------|--------|--------|
| [api-contracts.md](api-contracts.md) | Active | API endpoint signatures, response contracts, error matrices |
| [database.md](database.md) | Active | Schema conventions, migration patterns, dual-backend (SQLite/PG) |
| [deployment.md](deployment.md) | Active | Docker, Nginx, environment variables, production config |
| [testing.md](testing.md) | Active | Test patterns, fixtures, session-scoped DB, dispute test model |
| [dispute-system.md](dispute-system.md) | Active | Dispute lifecycle, voting, resolution, economic impact |

## Architecture Overview

- **Framework**: FastAPI 0.115+ with lifespan context
- **DB**: SQLite WAL (default) / PostgreSQL via asyncpg (optional)
- **Auth**: Bearer API key + Ed25519 signature (dual-mode)
- **Background**: asyncio.create_task cleanup loop (300s interval)
- **Version**: 0.5.0
