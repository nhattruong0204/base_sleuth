# Clanker Token Tracker

**Monitor all new Clanker token launches on Base, filter gems from trash, and traceback to originating X/Farcaster context.**

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                     DATA COLLECTION                         │
│                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────┐  │
│  │ Clanker API  │  │ Base RPC     │  │ Context Resolver │  │
│  │ Poll /tokens │  │ TokenCreated │  │ (clanker.world   │  │
│  │ every 30s    │  │ events (WS)  │  │  + web scrape)   │  │
│  └──────┬───────┘  └──────┬───────┘  └────────┬─────────┘  │
│         │                 │                    │            │
│         └────────┬────────┘                    │            │
│                  ▼                             │            │
│         ┌────────────────┐                     │            │
│         │  Dedup + Merge │◄────────────────────┘            │
│         └───────┬────────┘                                  │
└─────────────────┼───────────────────────────────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────┐
│                     STORAGE (SQLite/PostgreSQL)              │
│                                                             │
│  tokens | token_metrics | token_context | quality_scores    │
└─────────────────┬───────────────────────────────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────┐
│                     FILTERING PIPELINE                       │
│                                                             │
│  Stage 1: Instant reject (no liquidity, rug patterns)       │
│  Stage 2: Early metrics (5-30min volume, holders, ratio)    │
│  Stage 3: Smart money check (known profitable wallets)      │
│  Stage 4: Context quality (has real project? community?)    │
└─────────────────┬───────────────────────────────────────────┘
                  ▼
┌─────────────────────────────────────────────────────────────┐
│                     NOTIFICATIONS                            │
│                                                             │
│  Telegram bot: alerts with token info + traceback context   │
└─────────────────────────────────────────────────────────────┘
```

## Key Design Decisions

1. **Clanker API as primary source** — Public, no auth needed for reads, gives structured data
2. **Context resolution is async** — Don't block on scraping; enrich tokens after detection
3. **SQLite for dev, PostgreSQL for prod** — Same schema, swap via config
4. **Bankr identification** — Filter by known Bankr deployer addresses in `requestorAddress`
5. **X traceback without X API** — Scrape clanker.world token pages + web search fallback

## Setup

```bash
pip install -r requirements.txt
cp config.example.yaml config.yaml  # Edit with your settings
python -m clanker_tracker.main
```
