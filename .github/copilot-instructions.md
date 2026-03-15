# GitHub Copilot — Base Sleuth Custom Instructions

You are working on **Base Sleuth**, an autonomous hidden-gem discovery agent for Base blockchain tokens launched via Clanker.

## Before doing ANYTHING, read these files for context:

1. `.github/base-sleuth/SKILL.md` — Core strategy, pipeline architecture, scoring weights, trading rules
2. `.github/base-sleuth/references/clanker_intel.md` — API quirks, pagination, response shapes, ecosystem data
3. `.github/base-sleuth/references/strategy_evolution.md` — Current strategy version and active parameters
4. `.github/base-sleuth/references/token_blacklist.md` — Patterns and deployers to avoid
5. `.github/base-sleuth/references/wallet_watchlist.md` — Smart money wallets being tracked

## Project Architecture

- **18 async loops**: firehose (30s), champagne scanner (120s), breakout scanner (180s), gainers scanner (180s), binance trending (180s), eval pipeline (10s), PID outcome tracker (60s), wallet sync (300s), wallet monitor (120s), Nansen Telethon listener (persistent), champagne eval (600s), paper trading (300s), multi-wallet conviction (300s), token flow monitor (300s), portfolio watch (600s), milestone tracker (300s), gate-pending re-scan (600s)
- **5-stage scoring**: pre-filter → reject gate → DEX metrics → momentum → smart money → context + Binance audit/enrichment + Arkham holder intel/deployer profiling
- **Bankr detection**: 92% of Clanker launches are Bankr bot spam — auto-skipped via description parsing
- **Champagne tag**: Only 0.02% of tokens, but 58% have real liquidity — highest priority signal
- **Breakout scanner**: Uses DexScreener trending/boosted/profiles to find delayed movers on Base
- **Smart wallet tracking**: Arkham API (8 capabilities: wallet sync, swap monitor, holder intel, deployer profiling, token flows, portfolio watch) + Nansen bot signals
- **Live DEX data**: All Telegram messages include MCap, FDV, Liquidity, Price from DexScreener

## Key Files

| File | Role |
|------|------|
| `clanker_tracker/main.py` | 16-loop async orchestrator |
| `clanker_tracker/filters.py` | Pre-filter + 5-stage scoring pipeline + Binance audit + Arkham intel enrichment |
| `clanker_tracker/clanker_client.py` | Cursor-based API polling + champagne scan + breakout scanner |
| `clanker_tracker/binance_client.py` | Binance Skills Hub client (trending, audit, dynamic data, search, wallet) |
| `clanker_tracker/arkham_client.py` | Arkham Intel client (8 capabilities: wallet sync, swap monitor, holder intel, deployer profiling, token flows, portfolio watch) |
| `clanker_tracker/wallet_monitor.py` | Smart wallet swap monitor + multi-wallet conviction detection |
| `clanker_tracker/models.py` | SQLAlchemy async ORM (Token, TokenContext, TokenMetrics, SmartWallet, WalletSwap, PaperPosition) |
| `clanker_tracker/config.py` | Pydantic config with all thresholds and weights |
| `clanker_tracker/notifier.py` | Telegram alerts with live DEX data (7 types: alert, wallet buy, conviction, nansen, flow, milestone, dead) |
| `clanker_tracker/bot_commands.py` | Interactive Telegram bot (commands, inline keyboard, force scan) |
| `clanker_tracker/context_resolver.py` | Origin tracing (social URLs → page scrape → DDG) |
| `clanker_tracker/nansen_listener.py` | Telethon-based NansenBot listener for wallet signals |
| `config.example.yaml` | All configurable settings with comments |
| `data/smart_money_wallets.txt` | Machine-readable wallet list for Stage 4 |
| `alembic/` | PostgreSQL schema migrations (Alembic) |
| `docker-compose.yml` | PostgreSQL + bot deployment stack (source code volume-mounted) |
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
- **Telethon** for NansenBot Telegram listener
- Pydantic config, structlog logging
- `python-telegram-bot` for notifications, `BeautifulSoup` for scraping

## Rules

- Always use async/await patterns — the entire codebase is async
- Use `structlog` for logging, never `print()`
- Use `logger.warning()` or `logger.info()` for diagnostics — `logger.debug()` is invisible at default INFO level
- All new config fields go in `config.py` (Pydantic) AND `config.example.yaml`
- New scoring signals should be added as a stage in `filters.py`
- New DB columns require an Alembic migration in `alembic/versions/`
- When updating strategy, also update `.github/base-sleuth/references/strategy_evolution.md`
- Keep `.github/base-sleuth/SKILL.md` in sync with any pipeline changes

## Deployment

- Source code is **volume-mounted** in Docker: `./clanker_tracker:/app/clanker_tracker:ro`
- Python file changes → `docker compose restart bot` (no rebuild needed)
- Changes to `requirements.txt`, `Dockerfile`, `alembic/`, or `data/` → `docker compose up -d --build bot`
- `docker compose restart` does NOT rebuild the image — only picks up volume-mounted source
- Always verify with `docker compose logs bot --tail 20` after deploy
