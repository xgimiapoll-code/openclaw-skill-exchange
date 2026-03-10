# Pre-Deployment Checklist

Use this before deploying the service to a new environment.

## Environment
- [ ] `.env` file created from `.env.example`
- [ ] `POSTGRES_PASSWORD` set to a secure value (not `openclaw_dev`)
- [ ] `MARKET_CORS_ORIGINS` set to actual domain (not `*`)
- [ ] `MARKET_LOG_FORMAT=json` for structured logging
- [ ] `MARKET_LOG_LEVEL=WARNING` or `INFO` (not DEBUG)

## Database
- [ ] PostgreSQL 16+ available and reachable
- [ ] `MARKET_DATABASE_URL` set correctly
- [ ] Alembic migration run: `alembic upgrade head`
- [ ] Verify `resolution_comment` column exists in `disputes` table
- [ ] See [database.md](../backend/database.md) for schema contract

## Docker
- [ ] `docker compose -f docker-compose.prod.yml config` validates
- [ ] `docker build .` succeeds
- [ ] Health check passes: `curl localhost/healthz`
- [ ] See [deployment.md](../backend/deployment.md) for Docker contracts

## Monitoring
- [ ] `/healthz` returns `{"status": "ok"}`
- [ ] `/metrics` returns Prometheus-format text
- [ ] Configure Prometheus scrape target (if applicable)
- [ ] Set up alerts on `status: degraded`

## Security
- [ ] CORS origins restricted to actual frontend domain
- [ ] API keys use Bearer token auth (not query params)
- [ ] No `.env` file committed to git
- [ ] Docker volumes for persistent data
