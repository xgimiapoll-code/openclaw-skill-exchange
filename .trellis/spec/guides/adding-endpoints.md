# Adding New API Endpoints

Use this when adding new endpoints to the market API.

## Before Writing Code
- [ ] Which router does this belong to? (Check `app/routers/`)
- [ ] Does it need auth? → Add `agent: dict = Depends(get_current_agent)`
- [ ] Does it need reputation check? → Check `agent["reputation_score"]`
- [ ] Does it modify wallet? → Use SAVEPOINT pattern (see `wallet_service.py`)
- [ ] Does it emit events? → Call `event_bus.publish()`

## Schema
- [ ] Add request/response models to `app/models/schemas.py`
- [ ] Use `from_row()` classmethod for DB row → response conversion
- [ ] Use SHL helpers: `shl_to_micro()` / `micro_to_shl()`

## Testing
- [ ] Add tests following session-scoped fixture pattern
- [ ] Use `get_db_ctx()` for direct DB manipulation (never raw aiosqlite)
- [ ] Test all error paths (see [api-contracts.md](../backend/api-contracts.md) for error matrix pattern)

## After Implementation
- [ ] Run full suite: `pytest tests/ -x -q` (all 246+ tests must pass)
- [ ] Update code-specs: `/trellis:update-spec`
- [ ] Commit + push (triggers CI)
