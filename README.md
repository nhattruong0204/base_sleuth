# Clanker Token Tracker (Base Sleuth)

Autonomous hidden-gem discovery agent for **Base blockchain** via [Clanker](https://clanker.world). Filters 38K+ daily token launches (92% Bankr bot spam) to surface the <0.02% worth watching. Runs 3 concurrent async loops — firehose scanner, champagne curated scanner, and a 5-stage scoring pipeline — then sends Telegram alerts for gems.

## How It Works

```
Clanker API ─┬─ Firehose (30s) ──────────┐
             │                            ├──▶ Pre-filter ─▶ DexScreener ─▶ 5-Stage Score ─▶ Telegram
             └─ Champagne (120s) ─────────┘        │              (batch 30)        │
                  curated gems               Skip Bankr 92%                   Alert if ≥ 0.45
```

### 3 Concurrent Loops

| Loop | Interval | Purpose |
|------|----------|---------|
| **Firehose** | 30s | Streams ALL new tokens via cursor pagination |
| **Champagne** | 120s | Scans `champagne=true` curated gems (0.02% of all tokens, 58% have real liquidity) |
| **Eval** | 10s | Pre-filter → batch DexScreener → 5-stage scoring → Telegram alerts |

### 5-Stage Scoring Pipeline

| Stage | Weight | What It Checks |
|-------|--------|---------------|
| **Pre-filter** | gate | Skip Bankr (92%), scam keywords, require socials (optional) |
| **S1 Reject** | gate | Scam names, no data — champagne auto-passes |
| **S2 DEX Metrics** | 30% | Liquidity ≥$500, volume, buy/sell ratio, holders |
| **S3 Momentum** | 20% | Price surge, buys/hr, volume velocity |
| **S4 Smart Money** | 20% | Known profitable wallet matches |
| **S5 Context** | 15% | Social links, origin URL, description quality |
| **Champagne Bonus** | +15% | Flat additive for curated tokens |

### Bankr Detection

92% of Clanker launches come from Bankr bot (detectable via description text: "bankrbot", "bankr terminal"). These are auto-skipped in the pre-filter to avoid wasting DexScreener API calls.

### Context Resolution (No X API Needed)

3 strategies to trace tokens back to their origin:
1. Parse `socialLinks` array from Clanker API response
2. Scrape `clanker.world/clanker/{address}` for origin links
3. DuckDuckGo fallback: `$SYMBOL bankrbot site:x.com`

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
| `clanker.poll_interval_seconds` | 30 | Firehose polling interval |
| `clanker.champagne_poll_interval_seconds` | 120 | Champagne scan interval |
| `filtering.skip_bankr` | true | Auto-reject 92% Bankr bot launches |
| `filtering.score_threshold` | 0.45 | Minimum score to trigger alert (0–1) |
| `filtering.recheck_delay_seconds` | 300 | Wait for DexScreener indexing |
| `dexscreener.batch_size` | 30 | Addresses per batch lookup (API max: 30) |
| `telegram.bot_token` | — | Telegram Bot API token |
| `telegram.chat_id` | — | Target chat/channel ID |

Environment variable overrides: `CLANKER_API_KEY`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, `DATABASE_URL`, `BASE_RPC_WS`.

## Project Structure

```
clanker_tracker/
├── __init__.py
├── config.py            # Pydantic config models, YAML loader
├── models.py            # SQLAlchemy async models (Token, TokenContext, TokenMetrics)
├── clanker_client.py    # Cursor-based polling + champagne scan + platform detection
├── context_resolver.py  # Traces tokens back to X/Farcaster origin (no X API)
├── filters.py           # Pre-filter + 5-stage scoring pipeline + batch DexScreener
├── notifier.py          # Telegram alerts with champagne badges & momentum signals
└── main.py              # 3-loop async orchestrator with priority queue

base-sleuth/                # Agent skill — self-improving discovery system
├── _meta.json
├── SKILL.md                # Core skill: strategy, protocol, self-improvement rules
└── references/
    ├── trading_journal.md      # Paper trade logs with reasoning & outcomes
    ├── strategy_evolution.md   # Strategy iterations with performance data
    ├── wallet_watchlist.md     # Smart money wallets (Base chain)
    ├── token_blacklist.md      # Deployers, tokens, patterns to avoid
    └── clanker_intel.md        # Live Clanker/Base ecosystem intelligence

data/
├── smart_money_wallets.txt   # Machine-readable wallet list (one per line)
└── clanker_tracker.db        # SQLite database (auto-created)
```

## Agent Skill

The `base-sleuth/` directory contains a **self-improving agent skill** that guides autonomous operation:

- **SKILL.md** — Core strategy, entry/exit rules, Telegram protocol, self-improvement loop
- **References** — Living documents updated after every 10 trades with performance data
- The agent paper trades, tracks results, and refines its own strategy over time

## APIs Used

| API | Purpose | Auth |
|-----|---------|------|
| Clanker | `https://www.clanker.world/api/tokens` — new launches + champagne scan | Public |
| DexScreener | `https://api.dexscreener.com/latest/dex/tokens/{addr}` — batch metrics | Free |
| DexScreener Boost | `/token-boosts/top/v1` — tokens paying for visibility | Free |
| DuckDuckGo HTML | Fallback origin search | Free |
| Telegram Bot | Alerts & portfolio updates | Bot token |

## Tech Stack

- Python 3.11+, fully async (`asyncio`)
- `httpx` for HTTP, `SQLAlchemy 2.0` async for DB
- SQLite (dev) / PostgreSQL (prod)
- `BeautifulSoup` for scraping, `structlog` for logging
- `Pydantic` for config, `python-telegram-bot` for notifications

## Key Data Insights (Feb 2026)

| Metric | Value |
|--------|-------|
| Total Clanker tokens | 431,558+ |
| New tokens/day | ~38,000 (~26/min) |
| Bankr bot % | ~92% |
| Champagne tokens (total) | ~96 (0.02%) |
| Champagne with real liquidity | 58% |
| Regular tokens with any DEX presence | <5% |

## Roadmap

- [x] Bankr detection via description text analysis
- [x] Champagne curated token scanning
- [x] 5-stage scoring pipeline with momentum detection
- [x] Batch DexScreener lookups (30 per request)
- [x] Self-improving agent skill with paper trading
- [ ] Smart money wallet list population via on-chain analysis
- [ ] WebSocket monitoring for `TokenCreated` events via Base RPC
- [ ] DexScreener Boost API integration (marketing signal)
- [ ] Web dashboard for browsing scored tokens
- [ ] Automated backtesting against historical champagne tokens
