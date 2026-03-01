---
description: "Add a new feature to Base Sleuth — full context + test + deploy workflow"
mode: agent
tools: ["changes", "codebase", "fetch", "findTestFiles", "githubRepo", "problems", "usages"]
---

# Task: Add a New Feature to Base Sleuth

You are an expert Python async developer working on Base Sleuth, a Clanker token discovery agent running 24/7 on a VPS via Docker.

## Step 1 — Load Full Context

Read these files before writing any code:

- [SKILL.md](../base-sleuth/SKILL.md) — Strategy, architecture, scoring pipeline
- [clanker_intel.md](../base-sleuth/references/clanker_intel.md) — API behavior, response shapes
- [strategy_evolution.md](../base-sleuth/references/strategy_evolution.md) — Current thresholds and weights
- [token_blacklist.md](../base-sleuth/references/token_blacklist.md) — Patterns to avoid
- [wallet_watchlist.md](../base-sleuth/references/wallet_watchlist.md) — Smart money wallets
- [config.example.yaml](../../config.example.yaml) — All current config fields
- [filters.py](../../clanker_tracker/filters.py) — 5-stage scoring pipeline
- [main.py](../../clanker_tracker/main.py) — 9-loop async orchestrator
- [models.py](../../clanker_tracker/models.py) — SQLAlchemy async ORM schema
- [notifier.py](../../clanker_tracker/notifier.py) — Telegram alert sender (4 types: alert, wallet buy, conviction, nansen)
- [nansen_listener.py](../../clanker_tracker/nansen_listener.py) — Telethon-based NansenBot listener
- [bot_commands.py](../../clanker_tracker/bot_commands.py) — Telegram bot commands + inline keyboard
- [config.py](../../clanker_tracker/config.py) — Pydantic config classes
- [docker-compose.yml](../../docker-compose.yml) — Docker deployment config

## Step 2 — Plan

Before coding, explain:
1. Which files need to change and why
2. Which loops/stages are affected
3. Whether new config fields are needed (`config.py` + `config.example.yaml`)
4. Whether new DB columns are needed (`models.py` + Alembic migration)
5. Whether SKILL.md or references need updating

## Step 3 — Implement

Follow these rules strictly:
- All code must be **async/await** — the entire codebase is async
- Use `structlog` for logging, never `print()`
- New config fields → add to both `config.py` (Pydantic) AND `config.example.yaml`
- New scoring signals → add as a stage in `filters.py`
- New DB columns → update `models.py` AND create Alembic migration in `alembic/versions/`
- New dependencies → add to `requirements.txt`
- Use `logger.warning()` or `logger.info()` for important diagnostics — `logger.debug()` is hidden at default INFO log level and will silently swallow errors
- Test that all modules still import cleanly: `python -c "from clanker_tracker.main import Tracker"`

## Step 4 — Write Tests

Before deploying, write tests to catch bugs before they hit production:

### Unit Tests
- Create test files under `tests/` matching the module (e.g., `tests/test_notifier.py`)
- Mock external dependencies (DexScreener API, Telegram Bot API, DB sessions)
- Test each new function/method in isolation with known inputs and expected outputs
- Test edge cases: `None` values, empty responses, network timeouts, malformed data
- Verify error handling — ensure exceptions are caught and logged, not silently swallowed

### Integration Tests
- Test full async flows using `pytest-asyncio`
- API-dependent features: validate response shapes with `@pytest.mark.integration` (skippable in CI)
- DB-dependent features: use in-memory SQLite to verify queries
- Telegram formatting: assert final message strings contain expected sections (MCap, FDV, Liq, etc.)

### Run All Tests
```bash
python -m pytest tests/ -v --tb=short
python -c "from clanker_tracker.main import Tracker"  # full import chain check
```
Fix all failures before proceeding to deployment.

## Step 5 — Update Docs

After implementing, update:
- `.github/base-sleuth/SKILL.md` if pipeline architecture changed
- `.github/base-sleuth/references/strategy_evolution.md` if thresholds/weights changed
- `.github/base-sleuth/references/clanker_intel.md` if new API behavior discovered

## Step 6 — Deploy to Production

### How Docker Deployment Works
- **VPS**: `38.54.15.53` via `ssh -i ~/.ssh/lightnode_vps root@38.54.15.53`
- **Project dir**: `/opt/base-sleuth/`
- **Source code is volume-mounted**: `./clanker_tracker:/app/clanker_tracker:ro`
- Python file changes → `docker compose restart bot` (no rebuild needed)
- Changes to `requirements.txt`, `Dockerfile`, `alembic/`, or `data/` → `docker compose up -d --build bot` (full rebuild required)
- When in doubt → always use `docker compose up -d --build bot`

### Deployment Checklist
1. rsync changed files:
   ```bash
   rsync -avz -e "ssh -i ~/.ssh/lightnode_vps" ./clanker_tracker/ root@38.54.15.53:/opt/base-sleuth/clanker_tracker/
   ```
2. Restart or rebuild:
   - Python source only → `docker compose restart bot`
   - Dependencies/Docker/data → `docker compose up -d --build bot`
3. Verify startup — `docker compose logs bot --tail 20` — confirm all 9 loops running, no errors
4. Monitor first alert:
   ```bash
   docker compose logs bot -f | grep -E 'dex.live|telegram\.(sent|conviction|wallet_buy|nansen)'
   ```

### Pitfalls to Avoid
- `docker compose restart` does NOT rebuild the image — baked-in code stays old
- `logger.debug()` is invisible at INFO log level — use `logger.warning()` for diagnostics you need to see
- Always verify with logs after deploy — never assume restart = working

## User's Request

{{{ input }}}
