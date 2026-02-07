# Clanker Token Tracker (Base Sleuth)

Monitors **all** new token launches on [Clanker](https://clanker.world) (Base chain), identifies which ones were launched via [Bankr bot](https://x.com/bankrbot), traces each token back to its origin tweet or Farcaster cast, scores quality through a 4-stage pipeline, and sends Telegram alerts for the best finds.

## How It Works

```
Clanker API → Discover → Trace Origin → Score → Alert
   (30s)       new         (no X API      4-stage    Telegram
   poll       tokens        needed)       filter     notification
```

### Pipeline

1. **Poll** — `GET /api/tokens` every 30s, deduplicate via `clanker_id` watermark
2. **Bankr Detection** — check `requestorAddress` against known Bankr deployer wallets
3. **Context Resolution** — 3 strategies, no X API required:
   - Parse `social_media_urls` from Clanker API response
   - Scrape `clanker.world/clanker/{address}` for origin links
   - DuckDuckGo fallback: `$SYMBOL bankrbot site:x.com`
4. **4-Stage Scoring**:
   - **Stage 1**: Instant reject (no pool, scam keywords, low mcap)
   - **Stage 2**: Early metrics via DexScreener (volume, holders, buy/sell ratio)
   - **Stage 3**: Smart money detection (known profitable wallets)
   - **Stage 4**: Context quality (traceable origin, project idea)
5. **Telegram Alert** — rich HTML message with origin context, score breakdown, trading links

## Quick Start

```bash
# Clone
git clone https://github.com/nhattruong0204/base_sleuth.git
cd base_sleuth

# Setup
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# Configure
cp config.example.yaml config.yaml
# Edit config.yaml — add your Telegram bot token & chat ID

# Run
python -m clanker_tracker.main
```

## Configuration

Copy `config.example.yaml` to `config.yaml`. Key settings:

| Setting | Default | Description |
|---------|---------|-------------|
| `clanker.poll_interval_seconds` | 30 | How often to check for new tokens |
| `filtering.score_threshold` | 0.55 | Minimum score to trigger alert (0–1) |
| `filtering.smart_money_weight` | 2.0 | Weight multiplier for smart money signal |
| `telegram.bot_token` | — | Telegram Bot API token |
| `telegram.chat_id` | — | Target chat/channel ID |

Environment variable overrides: `CLANKER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`, `BASE_RPC_WS`.

## Project Structure

```
clanker_tracker/
├── __init__.py
├── config.py            # Pydantic config models, YAML loader
├── models.py            # SQLAlchemy async models (Token, TokenContext, TokenMetrics)
├── clanker_client.py    # Polls Clanker public API for new token launches
├── context_resolver.py  # Traces tokens back to X/Farcaster origin (no X API)
├── filters.py           # 4-stage quality scoring pipeline
├── notifier.py          # Telegram alerts with context
└── main.py              # Async orchestrator with polling loops
```

## APIs Used

- **Clanker**: `https://www.clanker.world/api/tokens` (public, no auth)
- **DexScreener**: `https://api.dexscreener.com/latest/dex/tokens/{address}` (free)
- **DuckDuckGo HTML**: Fallback search for origin tweets

## Tech Stack

- Python 3.11+, fully async (`asyncio`)
- `httpx` for HTTP, `SQLAlchemy 2.0` async for DB
- SQLite (dev) / PostgreSQL (prod)
- `BeautifulSoup` for scraping, `structlog` for logging
- `Pydantic` for config, `python-telegram-bot` for notifications

## Roadmap

- [ ] Actual Bankr deployer address discovery
- [ ] Smart money wallet list population
- [ ] WebSocket monitoring for `TokenCreated` events via Base RPC
- [ ] Web dashboard for browsing scored tokens
