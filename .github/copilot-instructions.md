# GitHub Copilot — Base Sleuth Custom Instructions

You are working on **Base Sleuth**, an autonomous hidden-gem discovery agent for Base blockchain tokens launched via Clanker.

## Before doing ANYTHING, read these files for context:

1. `base-sleuth/SKILL.md` — Core strategy, pipeline architecture, scoring weights, trading rules
2. `base-sleuth/references/clanker_intel.md` — API quirks, pagination, response shapes, ecosystem data
3. `base-sleuth/references/strategy_evolution.md` — Current strategy version and active parameters
4. `base-sleuth/references/token_blacklist.md` — Patterns and deployers to avoid
5. `base-sleuth/references/wallet_watchlist.md` — Smart money wallets being tracked

## Project Architecture

- **4 async loops**: firehose (30s), champagne scanner (120s), breakout scanner (180s), eval pipeline (10s)
- **5-stage scoring**: pre-filter → reject gate → DEX metrics → momentum → smart money → context
- **Bankr detection**: 92% of Clanker launches are Bankr bot spam — auto-skipped via description parsing
- **Champagne tag**: Only 0.02% of tokens, but 58% have real liquidity — highest priority signal
- **Breakout scanner**: Uses DexScreener trending/boosted/profiles to find delayed movers on Base

## Key Files

| File | Role |
|------|------|
| `clanker_tracker/main.py` | 4-loop async orchestrator (firehose, champagne, breakout, eval) |
| `clanker_tracker/filters.py` | Pre-filter + 5-stage scoring pipeline |
| `clanker_tracker/clanker_client.py` | Cursor-based API polling + champagne scan + breakout scanner |
| `clanker_tracker/models.py` | SQLAlchemy async ORM (Token, TokenContext, TokenMetrics) |
| `clanker_tracker/config.py` | Pydantic config with all thresholds and weights |
| `clanker_tracker/notifier.py` | Telegram alerts with badges, score breakdown, and inline buttons |
| `clanker_tracker/bot_commands.py` | Interactive Telegram bot (commands, inline keyboard, force scan) |
| `clanker_tracker/context_resolver.py` | Origin tracing (social URLs → page scrape → DDG) |
| `config.example.yaml` | All configurable settings with comments |
| `data/smart_money_wallets.txt` | Machine-readable wallet list for Stage 4 |
| `alembic/` | PostgreSQL schema migrations (Alembic) |
| `docker-compose.yml` | PostgreSQL + bot deployment stack |
| `Dockerfile` | Production container image |

## Clanker API Facts (IMPORTANT — don't get these wrong)

- Pagination is **cursor-based only** — `page` parameter is ignored
- Cursor is base64-encoded JSON: `{"id":"2026-02-07T03:50:49"}`
- Always returns 10 tokens per page regardless of `pageSize`
- `socialLinks` is `[{name, link}]` format, NOT a flat URL array
- `pool_address` can be 66-char bytes32 in clanker_v4
- `champagne=true` query param filters to curated tokens only

## Tech Stack

- Python 3.12, async (`asyncio`, `httpx`, `SQLAlchemy 2.0 async`)
- **PostgreSQL** (asyncpg) for production, SQLite for dev/testing
- **Alembic** for database schema migrations
- **Docker Compose** for 24/7 deployment (PostgreSQL + bot containers)
- Pydantic config, structlog logging
- `python-telegram-bot` for notifications, `BeautifulSoup` for scraping

## Rules

- Always use async/await patterns — the entire codebase is async
- Use `structlog` for logging, never `print()`
- All new config fields go in `config.py` (Pydantic) AND `config.example.yaml`
- New scoring signals should be added as a stage in `filters.py`
- New DB columns require an Alembic migration in `alembic/versions/`
- When updating strategy, also update `base-sleuth/references/strategy_evolution.md`
- Keep `base-sleuth/SKILL.md` in sync with any pipeline changes
