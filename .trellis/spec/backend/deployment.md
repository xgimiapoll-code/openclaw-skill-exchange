# Deployment Contracts

## Scenario: Docker + Production Deployment Stack

### 1. Scope / Trigger
- Trigger: v0.5.0 adds `docker-compose.prod.yml`, `nginx.conf`, updated `Dockerfile`
- Cross-layer: env vars, port mapping, PG connection, reverse proxy, WebSocket routing

### 2. Signatures

#### Docker Compose Profiles

```bash
# Default (SQLite)
docker compose up

# With PostgreSQL (dev)
docker compose --profile pg up

# Production (PG + Nginx)
docker compose -f docker-compose.prod.yml up -d
```

#### Dockerfile Build
```dockerfile
FROM python:3.12-slim
COPY pyproject.toml app/ alembic/ alembic.ini .
RUN pip install ".[postgresql]"   # Includes asyncpg
EXPOSE 8100
CMD uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8100}
```

### 3. Contracts

#### docker-compose.yml Services

| Service | Profile | Port | DB |
|---------|---------|------|-----|
| `market` | (default) | 8100 | SQLite `data/market.db` |
| `postgres` | `pg` | 5432 | PostgreSQL 16 |
| `market-pg` | `pg` | 8100 | PostgreSQL via `MARKET_DATABASE_URL` |

#### docker-compose.prod.yml Services

| Service | Port | Notes |
|---------|------|-------|
| `postgres` | (internal) | Healthcheck: `pg_isready` |
| `market` | (internal) | Depends on postgres healthy |
| `nginx` | 80 | Reverse proxy to market:8100 |

#### Environment Variables (Production)

| Key | Source | Required |
|-----|--------|----------|
| `POSTGRES_PASSWORD` | `.env` | Yes (prod) |
| `MARKET_DATABASE_URL` | compose env | Auto-set in prod compose |
| `MARKET_CORS_ORIGINS` | `.env` | No (default `*`) |
| `MARKET_LOG_FORMAT` | `.env` | No (default `json` in prod) |
| `MARKET_LOG_LEVEL` | `.env` | No (default `WARNING` in prod) |

#### Nginx Routing

| Location | Behavior |
|----------|----------|
| `/` | Proxy to `market:8100` with standard headers |
| `/v1/market/ws` | WebSocket upgrade + 86400s read timeout |
| `/healthz` | Proxy with `access_log off` |

### 4. Validation & Error Matrix

| Condition | Behavior |
|-----------|----------|
| `POSTGRES_PASSWORD` not set (prod) | Docker compose fails to start postgres |
| postgres not healthy | market service waits (depends_on condition) |
| Nginx can't reach market | 502 Bad Gateway |
| `market-pg` and `market` both active | Port 8100 conflict — use only one profile |

### 5. Good/Base/Bad Cases

- **Good**: `docker compose -f docker-compose.prod.yml up -d` with `.env` file → PG + Nginx + market running
- **Base**: `docker compose up` → SQLite-only, no PG, no Nginx
- **Bad**: Running `--profile pg` without stopping default `market` → port conflict on 8100

### 6. Tests Required

- Docker build succeeds: `docker build -t test .`
- Compose validation: `docker compose config` and `docker compose -f docker-compose.prod.yml config`
- Health check reachable through Nginx: `curl localhost/healthz`

### 7. Wrong vs Correct

#### Wrong — Dockerfile missing alembic
```dockerfile
COPY pyproject.toml .
COPY app/ app/
RUN pip install .   # Missing alembic files and asyncpg
```

#### Correct — Dockerfile with alembic + PG support
```dockerfile
COPY pyproject.toml .
COPY app/ app/
COPY alembic/ alembic/
COPY alembic.ini .
RUN pip install ".[postgresql]" && mkdir -p data
```

---

## Common Mistake: Profile Conflict

**Symptom**: Port 8100 already in use when running `--profile pg`

**Cause**: Both `market` (default) and `market-pg` (pg profile) try to bind port 8100.

**Fix**: Stop the default services first:
```bash
docker compose down
docker compose --profile pg up
```

**Prevention**: `market-pg` is only in the `pg` profile, so it won't start with a bare `docker compose up`. But if `market` was already running, you must stop it first.
