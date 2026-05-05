# Base Sleuth Codebase Index

Indexed: 2026-05-05

## Purpose

Base Sleuth is an async Python daemon for discovering and scoring newly launched Base-chain tokens, especially Clanker launches. It ingests tokens from several sources, filters obvious spam, enriches survivors with market and wallet intelligence, sends Telegram alerts, and tracks post-alert outcomes.

## Runtime Entry Points

- `python -m clanker_tracker.main [config_path]`: main daemon. If an argument is provided, it is treated as a config file path; there is no CLI help mode.
- `python -m clanker_tracker`: thin wrapper around `clanker_tracker.main`.
- Docker: `docker-compose.yml` runs PostgreSQL plus the bot container; the container entrypoint runs Alembic migrations before starting the daemon.
- Scripts:
  - `scripts/scan_gems.py`: one-shot live scanner for recent Clanker/Base candidates.
  - `scripts/deep_analysis.py`: production DB analysis against live DexScreener data.
  - `scripts/scrape_nansen_wallets.py`: Telethon scraper for @NansenBot wallet signals.

## Core Modules

- `clanker_tracker/main.py`: orchestrates all daemon loops, startup/shutdown, backoff, Telegram app startup, alerts, paper trading, outcome tracking, milestone tracking, and gate-pending rechecks.
- `clanker_tracker/config.py`: Pydantic config models and YAML/environment loader.
- `clanker_tracker/models.py`: SQLAlchemy async ORM models and engine/session helpers.
- `clanker_tracker/clanker_client.py`: Clanker API ingestion plus breakout and gainer scanners.
- `clanker_tracker/filters.py`: pre-filter and full scoring pipeline.
- `clanker_tracker/context_resolver.py`: origin URL resolution through Clanker social links, page scrape, and DuckDuckGo fallback.
- `clanker_tracker/notifier.py`: Telegram alert formatting and sending.
- `clanker_tracker/minara_client.py`: Minara Agent API wrapper for pre-alert BUY/WATCH/PASS thesis, optional alert gating, and API-key or x402 pay-as-you-go auth.
- `requirements-x402.txt`: optional dependencies for Minara x402 pay-as-you-go mode.
- `MINARA_SETUP.md`: Minara bot/x402 setup and Minara Skill install notes.
- `clanker_tracker/bot_commands.py`: interactive Telegram commands, menus, callbacks, status/stats views, manual scans, analysis reports, and Nansen message ingestion.
- `clanker_tracker/arkham_client.py`: Arkham wallet discovery, swap monitoring, holder/deployer intelligence, token flow, and portfolio watch.
- `clanker_tracker/wallet_monitor.py`: BaseScan wallet-transfer polling and conviction detection.
- `clanker_tracker/nansen_listener.py`: Telethon listener and parser for @NansenBot smart-alert messages.
- `clanker_tracker/binance_client.py`: Binance Web3 Skills trending, social hype, audit, enrichment, token search, and wallet balance APIs.

## Implemented Feature Inventory

### Token Discovery

- Clanker firehose polling with cursor/watermark deduplication.
- Clanker champagne-token polling for curated launches.
- First-run backfill over recent Clanker pages.
- DexScreener breakout scanner using boosts/profiles/trending-style endpoints.
- DexScreener search/gainers scanner with rotating Base-token keywords.
- Binance Skills Hub trending/social-hype scanner.
- Wallet conviction auto-ingest for tokens bought by multiple tracked wallets.

### Filtering And Scoring

- Fast pre-filter: optional Bankr skip and optional social-link requirement.
- Stage 1 reject: scam keywords and platform-name impersonation.
- Stage 2 DEX metrics: liquidity, market cap, volume, holders, buy/sell ratio, bot-buy spray, and wash-trade penalty.
- Stage 3 momentum: buys, short-window volume, price surge.
- Stage 4 smart money: known wallet overlap.
- Stage 5 context quality: social links and origin URL quality.
- Champagne score threshold override.
- Firehose scoring skip option, so firehose can be ingestion-only.
- Binance audit/enrichment score adjustments.
- Arkham holder and deployer intelligence score adjustments.
- Optional Minara pre-alert decision/thesis before Telegram gem alerts.
- Hard alert gates for market cap and liquidity.
- Contract-address-aware duplicate-symbol logic.

### Alerts And Telegram UI

- Main gem alerts with score, market context, origin/context, and chart/swap buttons.
- Optional Minara thesis block appended to gem alerts.
- Startup/shutdown pings.
- Wallet-buy alerts.
- Conviction alerts.
- Nansen signal alerts.
- Flow alerts for dump/accumulation signals.
- Milestone and dead-token alerts that can reply to the original alert message.
- Interactive bot commands: `/start`, `/menu`, `/status`, `/stats`, `/gems`, `/top`, `/scan`, `/config`, `/help`, `/realpnl`, `/wallets`, `/analysis`, `/positions`.
- Inline menus for status, stats, recent gems, top tokens, force scans, config, champagne, breakouts, PnL, wallets, and positions.

### Post-Alert Tracking

- PID-style outcome loop checking alerted tokens at 1h, 6h, and 24h.
- Outcome classification: `gem`, `survivor`, or `dead`.
- Conservative auto-tuning of `filtering.score_threshold`.
- Paper trading positions opened on alerts.
- Paper trading exits: stop loss, TP1/TP2/TP3, and time stop.
- Milestone tracker for all-time-high market cap and whole-number multipliers.
- Dead-token confirmation window and cleanup.
- Gate-pending loop for tokens that score well but initially fail market-cap/liquidity gates.

### Wallet Intelligence

- Static smart-money wallet file loaded into scoring.
- Arkham fomo-user wallet sync and profitability tiering.
- Arkham swap checks for tracked wallets.
- BaseScan transfer polling for watched wallets.
- Multi-wallet conviction detection within a configurable time window.
- NansenBot Telethon listener and parser.
- Nansen wallet data persistence to markdown and wallet files.

### Data And Persistence

- PostgreSQL is the intended production database.
- SQLite support exists in engine helper code, though local async SQLite startup should be verified before relying on it.
- Alembic migrations cover initial schema, widened symbols, alert outcomes, smart wallets/swaps, paper positions, milestone tracking, gate-pending fields, and alert message IDs.
- Current tracked data files:
  - `data/smart_money_wallets.txt`: 914 lines.
  - `data/nansen_wallets.md`: 1834 lines.
  - `wallets.txt`: 61 lines.

## Tests

- `tests/test_gate_pending.py`: gate-pending config, marking, rescans, max rechecks, duplicate-symbol behavior, and edge cases.
- `tests/test_milestone_tracker.py`: multiplier detection, ATH tracking, dead-token detection, PnL calculations, config, and notifier formatting.
- `tests/test_milestone_reply.py`: Telegram message ID return/storage and milestone/dead-token reply threading.

Latest local test run: `73 passed, 1 warning` using `.venv/bin/python -m pytest -q`. The warning is a Pydantic v2 deprecation for class-based `Config` in `BankrConfig`.

## Local Runtime Check

- `docker compose ps` cannot run in this WSL environment because Docker is not installed/integrated.
- `python -m clanker_tracker.main --help` starts the daemon; it is not a help command.
- A bounded daemon start with the current `config.yaml` reached `tracker.starting` and then timed out while starting against PostgreSQL.
- A bounded sanitized dry-run with SQLite and live integrations disabled also reached `tracker.starting` but did not complete startup within the timeout in this sandbox.

Practical implication: the code imports and tests pass, but a real bot run should be done with a reachable PostgreSQL service, network access to external APIs, and explicit control over Telegram credentials.

## Security And Operational Notes

- Keep real Telegram, Arkham, BaseScan, and Nansen credentials out of tracked files.
- `config.yaml` is ignored locally and may contain live credentials; do not commit it.
- The daemon can send real Telegram messages during startup and alerts when credentials are configured.
- `scripts/deep_analysis.py` assumes the Docker service hostname `db`; it is production-container oriented.
- Several live features depend on network access: Clanker, DexScreener, Telegram, Arkham, BaseScan, Binance Skills, DuckDuckGo, and optionally Nansen/Telethon.

## Good Extension Points

- Add a new discovery source by creating a scanner class that persists `Token` rows with a distinct `discovery_source`, then register a loop in `Tracker.start()`.
- Add a new scoring signal in `TokenFilter.evaluate()` and `_compute_weighted_score()`, with config fields in `FilteringConfig` or a dedicated config model.
- Add a new Telegram command in `bot_commands.py`, register it in `build_telegram_app()`, and expose it through the inline keyboard if it is user-facing.
- Add a new alert type as a method on `TelegramNotifier`, then call it from the relevant loop.
- Add new persistence fields through `models.py` plus an Alembic migration.
- Add focused tests under `tests/`; current tests are strongest around gate-pending and milestone behavior.
