# Base Sleuth — Strategy Evolution Log

This document tracks the evolution of the gem-hunting strategy based on paper trading results on Base chain.

**AGENT INSTRUCTION**: Update this document after every 10 trades with performance analysis and strategy adjustments. This is how you learn and improve.

---

## Current Strategy Version: 1.3

**Last Updated**: 2026-02-08
**Based on**: Live Clanker API data analysis (431K+ tokens, 38K/day)

### Active Scanning Strategy

| Source | Interval | Priority | Expected Hit Rate |
|--------|----------|----------|-------------------|
| Champagne scan | 120s | ⭐ Highest | ~58% have real liquidity |
| Direct launches (firehose) | 30s | Medium | ~5% survive |
| Clawnch launches | 30s | Low | ~2% survive |
| Bankr launches | skipped | ❌ Skipped | <0.5% survive |

### Active Scoring Weights

| Stage | Weight | Threshold |
|-------|--------|-----------|
| S2 DEX Metrics | 0.30 | liq≥$500, vol_1h≥$50 |
| S3 Momentum | 0.20 | 5+ buys/hr, 50% surge |
| S4 Smart Money | 0.20 | any tracked wallet |
| S5 Context | 0.15 | social links, origin URL |
| Champagne Bonus | +0.15 | flat additive |
| **Alert Threshold** | — | **≥ 0.45** |

### Active Exit Rules
- Stop Loss: -30% (mandatory, never override)
- TP1: +50% (sell 33%)
- TP2: +100% (sell 33%)
- TP3: +300% (sell remaining)
- Time stop: Exit if flat after 24h

### Active Pre-Filter
- Skip Bankr: **true** (92% of tokens)
- Require social links: **false**
- Scam keywords: rug, scam, honeypot, ponzi, fake, drain
- Recheck delay: 300s (DexScreener indexing time)

---

## Evolution History

### Iteration #0 — 2026-02-07 — Initial Setup

**Strategy**: Based on comprehensive analysis of live Clanker data.

**Key Findings That Shaped v1.0**:
1. 431K+ tokens deployed, ~38K/day — need aggressive pre-filtering
2. 92% are Bankr bot launches — auto-skip saves massive API cost
3. Only 96 champagne tokens out of 431K (0.02%) — strongest quality signal
4. Champagne tokens: 58% have real liquidity ($5K+) vs <1% regular
5. Only ~5% of all tokens appear on DexScreener at all
6. DexScreener needs 2-5 minutes to index new Base tokens

**Hypotheses to Validate**:
- Is 0.45 score threshold too low/high for quality alerts?
- Does momentum (Stage 3) actually predict 1h-forward returns?
- How many smart money wallets do we need tracked to be useful?
- Is skipping ALL Bankr tokens too aggressive? (Some champagne are Bankr)
- Does time-of-day matter for Base token launches?
- What's the optimal recheck delay? (Currently 300s)

**Baseline Metrics**:
- Pre-filter rejection rate: ~92% (Bankr skip)
- Expected eval candidates per day: ~3,000
- Expected alerts per day: unknown (need data)
- Champagne alerts per day: 0-2 (very rare)

---

### Iteration #1 — 2026-02-07 — Breakout Scanner

**Trigger**: $SEVEN (0x0DBFe...3c6b) launched 5 days before scan, pumped +412%
with $85K liquidity and 337 buys/hr. Firehose missed it because it only
watches NEW launches. Token scored 44% when manually evaluated — just 1%
below gem threshold.

**Problem Identified**: Firehose-only scanning creates a blind spot for
delayed breakouts. Tokens can be dormant for days then suddenly gain
momentum from community discovery, influencer mention, or marketing push.

**Solution — Breakout Scanner (Loop 3)**:
- Uses DexScreener trending/boosted/profiles endpoints
- Filters to Base chain, liq≥$10K, vol24h≥$5K, momentum required
- Cross-references DB to avoid re-alerting
- Breakout tokens get +0.10 score bonus
- Polls every 180s (3 min)
- Cooldown: 3600s per token to prevent spam

**Changes Made**:
- `clanker_client.py`: Added `BreakoutScanner` class
- `main.py`: Added 4th concurrent loop (`_breakout_loop`)
- `config.py`: Added `BreakoutConfig` section
- `filters.py`: Added `_breakout_bonus` to weighted score
- `notifier.py`: Added 📈 Breakout badge to alerts
- `models.py`: Added `is_breakout` + `discovery_source` columns
- `scripts/scan_gems.py`: Added Step 2.5 breakout scan

**Hypotheses to Validate**:
- What % of DexScreener boosted Base tokens are Clanker-deployed?
- Does the +0.10 breakout bonus help or cause false positives?
- Is 180s poll interval fast enough to catch breakouts early?
- How many breakout candidates per scan cycle on average?
- Do breakout tokens have better win rates than firehose discoveries?

---

### Iteration #2 — 2026-02-07 — PostgreSQL + 24/7 Daemon

**Trigger**: Need for persistent, always-on scanning with Telegram alerts.
SQLite insufficient for production — no concurrent access, no connection pooling,
no schema migrations.

**Changes — Database Migration**:
- Switched default database from SQLite to PostgreSQL (asyncpg driver)
- Added connection pool settings (pool_size=10, max_overflow=20, pool_recycle=3600s)
- Added pool_pre_ping=True for stale connection detection
- `models.py`: `create_engine()` now accepts pool params, auto-skips for SQLite
- `config.py`: `DatabaseConfig` updated with pool_size, max_overflow, pool_recycle fields

**Changes — Alembic Migrations**:
- Added `alembic/` directory with async env.py
- Initial migration `001_initial_schema.py` creates all 3 tables
- Partial index `ix_tokens_unscored` for fast eval loop queries
- Reads DATABASE_URL from env → config.yaml → alembic.ini (priority order)

**Changes — 24/7 Resilience**:
- `main.py`: Exponential backoff on all 4 loops (2^n seconds, cap 5 min)
- Per-loop error counters + backoff state
- Stats tracking: tokens_discovered, tokens_scored, alerts_sent, errors
- Heartbeat logging every 5 min with uptime + all stats
- Telegram startup/shutdown pings for operational awareness
- Eval loop now prioritizes: champagne → breakout → oldest unscored

**Changes — Docker Deployment**:
- `Dockerfile`: Multi-stage build, non-root user, health check
- `docker-compose.yml`: PostgreSQL 16 + bot, auto-restart, health deps
- `.env.example`: Template for TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID, DB creds
- `__main__.py`: `python -m clanker_tracker` entry point
- Entrypoint runs `alembic upgrade head` before starting bot

**Changes — Infrastructure**:
- `requirements.txt`: Added alembic>=1.13
- `config.example.yaml`: PostgreSQL as default, pool settings documented
- Version bumped to 1.0.0

---

### Iteration #3 — 2026-02-08 — Interactive Telegram Bot

**Trigger**: Need to interact with the bot in real-time from Telegram —
check status, view gems, force scans, browse champagne/breakout tokens.
Previously Telegram was one-way alerts only.

**Changes — Interactive Bot (bot_commands.py)**:
- New module `clanker_tracker/bot_commands.py` with full inline keyboard UI
- Uses `python-telegram-bot` v21+ `Application` in non-blocking mode
- Runs alongside all 4 scanner loops in the same event loop
- `build_telegram_app()` creates Application with all handlers registered
- Stores Tracker reference in `bot_data` for handler access to DB/stats

**Commands Added**:
- `/start`, `/menu` — Main dashboard with 9-button inline keyboard grid
- `/status` — Uptime, loop health (green/yellow/red), backoff states, session stats
- `/stats` — Full DB statistics (total, bankr, champagne, breakout, scored, pending, alerts)
- `/gems` — Recent gems above score threshold with DexScreener/Uniswap links
- `/top` — Top 10 highest scored tokens across all sources
- `/scan` — Force scan sub-menu (firehose/champagne/breakout/eval/all)
- `/config` — View all filter config, scoring weights, breakout settings
- `/help` — Complete command reference

**Inline Keyboard Menus**:
- Main dashboard: Status, Stats, Gems, Top, Force Scan, Config, Champagne, Breakouts, Help
- Scan sub-menu: Firehose Now, Champagne Now, Breakout Now, Eval Now, Scan All, Back
- Every response has Back to Menu button for easy navigation

**Changes — Alert Buttons (notifier.py)**:
- Token alert messages now include inline buttons: DexScreener, Uniswap, Clanker Page, Dashboard
- Users can trade directly from alert notifications

**Changes — main.py Integration**:
- Added `_start_telegram_bot()` / `_stop_telegram_bot()` lifecycle methods
- Application starts before scanner loops, stops during graceful shutdown
- `drop_pending_updates=True` to avoid replaying stale button presses
- Startup ping now mentions `/menu` for interactive access

**Files Modified**:
- `clanker_tracker/bot_commands.py` (new — 480+ lines)
- `clanker_tracker/main.py` (Telegram app lifecycle integration)
- `clanker_tracker/notifier.py` (alert inline buttons)
- `base-sleuth/SKILL.md` (interactive bot documentation)
- `.github/copilot-instructions.md` (added bot_commands.py to key files)

---

<!--
TEMPLATE FOR FUTURE ITERATIONS:

### Iteration #X — YYYY-MM-DD

**Trades Analyzed**: #XX to #XX

#### Performance Metrics
- Win Rate: XX% (target: >35%)
- Average Win: +XX%
- Average Loss: -XX%
- Profit Factor: X.XX
- Net P&L: +/-$XXX
- Largest Win: $XX (+XX%)
- Largest Loss: $XX (-XX%)
- Avg Hold Time: Xh
- Champagne Win Rate: XX%
- Non-Champagne Win Rate: XX%

#### Stage Accuracy Analysis

Which stages predicted winners?

| Stage | Accuracy | Correlation with Profit |
|-------|----------|------------------------|
| S2 DEX Metrics | XX% | +/- |
| S3 Momentum | XX% | +/- |
| S4 Smart Money | XX% | +/- |
| S5 Context | XX% | +/- |
| Champagne Bonus | XX% | +/- |

#### Pattern Analysis

**What Worked (Keep/Increase Weight)**:
1. [Pattern that predicted wins]
2. [Factor that correlated with success]

**What Failed (Remove/Decrease Weight)**:
1. [Pattern that led to losses]
2. [Factor that didn't predict well]

**New Observations**:
1. [New pattern discovered]
2. [Market condition insight]

#### Strategy Adjustments Made

**Scoring Weight Changes**:
- [Stage X]: Weight changed from Y to Z because [reason]
- [New signal added]: [Description] because [reason]

**Threshold Changes**:
- Score threshold: changed from X to Y because [reason]
- [Metric] minimum: changed from X to Y because [reason]

**Pre-Filter Changes**:
- [Filter]: Added/Removed because [reason]

#### Updated Hypothesis

[What you now believe about Base token survival based on data]

#### Next Iteration Focus

[What to watch for in next 10 trades]

-->

---

## Key Learnings Repository

**ADD INSIGHTS HERE AS DISCOVERED:**

### Market Conditions
- [e.g., "Base tokens perform better when ETH is consolidating"]

### Timing
- [e.g., "US market hours produce more organic launches"]

### Token Characteristics  
- Champagne tag is by far the strongest quality signal (58% liquidity rate)
- Non-Bankr tokens have ~10x higher survival rate
- [ADD MORE AS DISCOVERED]

### Platform Signals
- Direct launches > Clawnch > Bankr (quality ordering)
- [ADD MORE AS DISCOVERED]

### Clanker Ecosystem
- Token volume has been increasing (~38K/day as of Feb 2026)
- Champagne curation is extremely selective (96/431K)
- [ADD MORE AS DISCOVERED]

---

## Failed Strategies Archive

**Document strategies that were tried and abandoned:**

<!--
### [Strategy Name] — Abandoned YYYY-MM-DD

**What it was**: [Description]
**Why it failed**: [Reason with data]
**Lesson**: [What to remember]
-->
