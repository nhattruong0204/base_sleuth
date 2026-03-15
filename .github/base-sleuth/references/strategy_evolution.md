# Base Sleuth — Strategy Evolution Log

This document tracks the evolution of the gem-hunting strategy based on paper trading results on Base chain.

**AGENT INSTRUCTION**: Update this document after every 10 trades with performance analysis and strategy adjustments. This is how you learn and improve.

---

## Current Strategy Version: 1.9

**Last Updated**: 2026-03-15
**Based on**: Production DB analysis (336K tokens, 474 alerts, 26 gems) + Missed Gem Analysis

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

### Active Hard Gates (v1.9 — data-driven adjustments)
- MCap gate: **$25K** (lowered from $50K — 107 high-scoring tokens were blocked)
- Liquidity gate: **$5K**
- Duplicate symbol: **contract-address aware** (same symbol, different contract → allowed)
- Gate-pending re-scan: **enabled** (tokens that fail gates get re-checked every 10m for 3h)

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

### Iteration #4 — 2026-02-10 — Smart Wallet Tracking (Arkham Intel)

**Strategy Change**: Integrated Arkham Intel API for smart wallet discovery and monitoring.

#### What Changed
1. **Arkham Intel Integration** (`arkham_client.py`):
   - Fetches fomo-tagged wallets from Arkham Intel API
   - Analyzes PnL at 1d/7d/30d via historical USD balance snapshots
   - Classifies wallets into Tier 1 (3/3 profitable), 2, or 3
   - Monitors wallet swaps via /swaps endpoint (heavy, 1 req/sec)

2. **New DB Tables** (migration 004):
   - `smart_wallets` — address, tier, PnL metrics, Arkham entity/label
   - `wallet_swaps` — buy/sell records, tx_hash dedup, conviction tracking

3. **Wallet Buy Alerts**:
   - Telegram alert when tracked wallet buys any token on Base
   - Shows wallet tier, PnL history, buy amount
   - DexScreener + Uniswap + Arkham Explorer links

4. **Conviction Alerts** (highest priority):
   - Fires when a tracked wallet buys a token already in our DB
   - Cross-references against tokens table for exact match
   - Separate high-visibility alert format with 🔥 CONVICTION badge

5. **Stage 4 Wired to DB**:
   - `_stage4_smart_money()` now queries WalletSwap + SmartWallet tables
   - Tiered scoring: T1 wallets worth more than T3
   - Auto-updates `data/smart_money_wallets.txt` for compatibility

6. **8 Async Loops** (was 6):
   - Added: Wallet Sync (3600s) and Wallet Monitor (60s)
   - All loops share the same shutdown event and backoff system

7. **New /wallets Command**:
   - Shows tracked wallet count, tier breakdown, 24h swaps/convictions
   - Top 5 wallets with PnL and Arkham labels

#### Files Modified
- `config.py` (ArkhamConfig) + `config.example.yaml`
- `models.py` (SmartWallet, WalletSwap)
- `arkham_client.py` (NEW — ArkhamClient + WalletTracker)
- `notifier.py` (notify_wallet_buy, notify_conviction)
- `main.py` (wallet_sync_loop, wallet_monitor_loop)
- `filters.py` (Stage 4 wired to DB)
- `bot_commands.py` (/wallets command)
- `alembic/versions/004_add_smart_wallets_and_swaps.py`
- `base-sleuth/SKILL.md`, `strategy_evolution.md`

---

### Iteration #5 — 2026-02-15 — Data-Driven Overhaul (v0.7.0)

**Trigger**: Comprehensive analysis of all 289,775 tokens in production DB revealed
systemic issues: 83% false positive rate, firehose 0% gem rate, PID runaway to 0.75
ceiling, champagne only 3/390 alerted, 0 convictions fired.

#### What Changed — 6 Fixes

1. **Kill Firehose Scoring** (Fix 1):
   - Firehose scored 259,073 tokens with 0 gems — pure CPU waste
   - `skip_firehose_scoring: true` — firehose only ingests, never evaluates
   - Eval pipeline now filters `discovery_source != 'firehose'`
   - Profile/champagne/breakout/community_takeover tokens still scored normally

2. **Fix Champagne Pipeline** (Fix 2):
   - New `champagne_score_threshold: 0.30` (vs 0.45 normal)
   - New `_champagne_eval_loop()` — re-evaluates champagne tokens up to 3h
   - Problem: champagne tokens often have no DEX data at discovery time
   - Loop re-checks every 10 min with lower threshold — never misses a real one

3. **Multi-Wallet Conviction Engine** (Fix 3):
   - New `_multi_conviction_loop()` — finds tokens bought by 2+ wallets in 6h window
   - Reversed logic: wallet buy → check DexScreener (vs old: token → check wallets)
   - Auto-ingests new tokens from wallet buys if liquidity ≥ $10K
   - Fires high-conviction alerts with wallet count + details

4. **Fix PID Controller** (Fix 4):
   - `min_samples: 50` (was 10) — prevents overreaction to small dataset
   - `threshold_adjust_step: 0.005` (was 0.01) — smaller corrections
   - `threshold_max: 0.60` (hard ceiling, was 0.75)
   - Asymmetric: lowering easier (step×1.5) than raising (step×1.0)
   - All adjustments logged at INFO level for visibility

5. **Paper Trading Simulator** (Fix 5):
   - New `PaperPosition` model + Alembic migration 005
   - Auto-enters $1000 simulated position on every alert
   - Tracks SL (-30%), TP1 (+50%), TP2 (+100%), TP3 (+300%), time stop (24h)
   - `_paper_trading_loop()` checks positions every 5 min via DexScreener
   - New `/positions` Telegram command — portfolio dashboard with PnL, win rate
   - First data-driven measurement of actual bot profitability

6. **Wash-Trading Penalty** (Bonus):
   - Detects tokens with vol_1h > 2× liquidity AND 200+ buys
   - Applies 0.50× score multiplier — halves the score
   - Catches artificial volume inflation patterns

#### Architecture Changes
- **12 async loops** (was 9): Added champagne_eval, paper_trading, multi_conviction
- **New model**: PaperPosition (paper_positions table)
- **New config classes**: ChampagneEvalConfig, PaperTradingConfig, MultiWalletConvictionConfig
- **New bot command**: /positions — paper trading dashboard
- **Updated startup ping**: Shows all 12 loops

#### Data-Driven Rationale
| Metric | Before | Expected After |
|--------|--------|----------------|
| Firehose tokens scored | 259,073 | 0 |
| Champagne alert rate | 3/390 (0.8%) | ~50%+ with re-eval |
| PID threshold | 0.75 (stuck at ceiling) | 0.45-0.55 range |
| Conviction alerts fired | 0 | Multi-wallet triggers |
| Bot profitability measured | ❌ Never | ✅ Paper trading |
| Wash-trade false positives | Unknown | Filtered by 0.50× penalty |

#### Files Modified
- `config.py` — 3 new config classes + FilteringConfig (wash/firehose/champagne fields) + PID fixes
- `filters.py` — Wash-trading penalty in Stage 2
- `models.py` — PaperPosition model
- `main.py` — 3 new loops, eval loop changes, PID rewrite, startup ping
- `bot_commands.py` — /positions command + dashboard builder
- `config.example.yaml` — All new config sections documented
- `alembic/versions/005_add_paper_positions.py` — New table migration
- `strategy_evolution.md` — This entry

---

### Iteration #6 — 2026-03-03 — Binance Skills Hub Integration (v0.8.0)

**Trigger**: Binance launched a public Skills Hub marketplace giving AI agents
native access to crypto data — CEX rankings, DEX metrics, wallet tracking,
security audits, and holder analysis. Multiple skills support Base chain (8453),
providing data sources complementary to DexScreener and Arkham.

**Problem Identified**: The scoring pipeline relied solely on DexScreener for
DEX metrics and Arkham/Nansen for smart money signals. Binance Web3 Skills
offer free, no-API-key endpoints with unique data not available elsewhere:
- Token security audits (honeypot/scam/rug detection)
- KOL, smart money, and pro holder counts/percentages
- Social hype rankings with AI-generated sentiment summaries
- Cross-platform trending tokens (Binance's own ranking algorithm)

**Solution — Binance Skills Hub Integration**:

New file: `binance_client.py` — Full client for 6 Binance Skills APIs:
1. **Trending Scanner (new loop)** — Discovers Base tokens trending on Binance
   using Unified Token Rank (rank_type=10,11) and Social Hype Leaderboard.
   Polls every 180s, ingests new tokens for scoring.
2. **Token Security Audit** — Pre-alert honeypot/scam detection. Tokens with
   HIGH/BLOCKED risk get score zeroed. MEDIUM risk gets -0.15 penalty.
   LOW risk gets +0.05 bonus. Tax > 10% triggers penalty.
3. **Token Dynamic Data** — Enriches scoring with KOL holder count (+0.05),
   Binance smart money holder count (+0.08), and pro holder bonus (+0.03).
4. **Token Search** — Cross-chain token lookup by keyword/address.
5. **Wallet Balance** — Query wallet holdings on Base (for future wallet
   monitoring enhancement).
6. **Social Hype** — Social buzz rankings with sentiment analysis.

**Changes Made**:
- `clanker_tracker/binance_client.py`: New module — `BinanceSkillsClient` +
  `BinanceTrendingScanner` classes
- `clanker_tracker/config.py`: Added `BinanceSkillsConfig` (26 settings)
- `clanker_tracker/filters.py`: Binance audit + enrichment in score calculation
  - `set_binance_client()` injection method
  - `_compute_weighted_score()` extended with audit/enrichment bonuses
- `clanker_tracker/main.py`: New `_binance_trending_loop()`, initialization
- `config.yaml` / `config.example.yaml`: Full Binance Skills configuration
- `.github/base-sleuth/references/strategy_evolution.md`: This entry

**Scoring Impact**:
- LOW risk audit: +0.05 bonus (verified safe contracts)
- MEDIUM risk: -0.15 penalty (proceed with caution)
- HIGH risk: score × 0.3 (heavy penalty)
- BLOCKED: score = 0 (hard reject)
- Tax > 10%: -0.10 penalty
- KOL holders: +0.05 bonus
- Smart money (Binance): +0.08 bonus
- Pro holders (3+): +0.03 bonus
- Binance trending discovery: +0.05 score bonus

**Key Design Decisions**:
- All Binance APIs are public — no API key required
- Audit runs during scoring (after Stage 5, before final score)
- Trending scanner is a separate loop (like breakout/gainers)
- Uses same skip_addresses list as gainers scanner
- Rate-limited at 0.5s between calls to be polite

---

### Iteration #7 — 2026-03-03 — Arkham Deep Intelligence Suite (v0.9.0)

**Trigger**: Arkham Intel API provides 60+ endpoints for on-chain intelligence
beyond basic wallet tracking. The existing integration only used
`/intelligence/address` for wallet identification and `/swaps` for trade
detection. Expanding to holder analysis, deployer profiling, and token flow
monitoring adds institutional-grade intelligence to the scoring pipeline.

**Problem Identified**: The scoring pipeline had blind spots:
1. No insight into WHO holds a token (whales, funds, exchanges)
2. No profiling of the deployer (known builder vs serial scammer)
3. No monitoring of fund flows after alerts (dump detection)
4. No portfolio surveillance of top wallets (new position discovery)

**Solution — Arkham Deep Intelligence Suite (4 features)**:

**1. Token Holder Intelligence (THI)**:
Uses `GET /token/holders/{chain}/{address}` + `POST /intelligence/address_enriched/batch`
to analyze the quality of a token's holder base:
- Identifies known entities (funds, VCs, DAOs, exchanges) among top holders
- Scores holder quality (0-1) based on entity type diversity
- Detects concentration risk (top 10 holders owning >80%)
- Flags exchange hot wallet dominance (dumping risk)
- Integrated into `_compute_weighted_score()` as additive adjustments

**2. Deployer Profiling**:
Uses `GET /intelligence/contract/{chain}/{address}` + address enrichment
to assess the deployer's reputation:
- Identifies deployer entity (known builder → bonus, scam tag → reject)
- Detects serial deployers (many deploys in short time → suspicious)
- Checks proxy/upgradeable contracts (rug risk)
- Risk levels: safe (+0.10), unknown (neutral), risky (-0.10), dangerous (reject)

**3. Token Flow Monitoring (new loop)**:
Uses `GET /token/top_flow/{chain}/{address}` (heavy endpoint, 1 req/sec)
to monitor fund flows for alerted tokens with open paper positions:
- Detects dump warnings: large outflows from whales/funds
- Detects accumulation signals: sustained inflows from smart money
- Sends Telegram alerts with entity names and USD amounts
- Polls every 300s, checks up to 5 tokens per cycle

**4. Portfolio Watch (new loop)**:
Uses `GET /balances/address/{address}` to monitor top-tier wallet holdings:
- Detects new Base token positions taken by highest-confidence wallets
- Cross-references with tracked tokens for conviction signals
- Polls every 600s, checks top 5 wallets per cycle

**Changes Made**:
- `clanker_tracker/arkham_client.py`: 6 new methods on `ArkhamClient` +
  new `TokenFlowMonitor` class (~600 lines added):
  - `fetch_token_top_holders()`, `batch_identify_addresses()`,
  - `fetch_contract_intel()`, `fetch_token_flows()`,
  - `fetch_wallet_portfolio()`, `analyze_holder_quality()`,
  - `profile_deployer()`, `TokenFlowMonitor.check_flows()`
- `clanker_tracker/config.py`: ~60 new fields in `ArkhamConfig`
  (THI, deployer, flow, portfolio settings)
- `clanker_tracker/filters.py`: Arkham enrichment in `evaluate()` +
  scoring adjustments in `_compute_weighted_score()`
- `clanker_tracker/main.py`: `set_arkham_client()` wiring,
  `TokenFlowMonitor` init, `_token_flow_loop()`, `_portfolio_watch_loop()`
- `clanker_tracker/notifier.py`: `notify_flow_alert()` method
- Config files updated with all new Arkham settings

**Scoring Impact**:
- Fund/VC holders (2+): +0.12 bonus (strongest holder signal)
- Fund/VC holders (1): +0.06 bonus
- Known entities (3+): +0.08 bonus
- Concentration >80% top 10: -0.10 penalty
- Exchange hot wallets (5+): -0.05 penalty
- Single holder majority: -0.15 penalty
- All unknown holders: -0.03 penalty
- Known builder deployer: +0.10 bonus
- Risky deployer: -0.10 penalty
- Dangerous/scam deployer: score = 0.0 (hard reject)
- Proxy contract: -0.08 penalty
- Serial deployer: -0.05 penalty

**New Loops**: 17 total (was 14)
- Token flow monitor (300s) — dump/accumulation detection
- Portfolio watch (600s) — wallet position surveillance
- Milestone tracker (300s) — ATH + multiplier notifications + dead token cleanup

---

### Iteration #8 — 2026-03-15 — Milestone Tracker (v1.0.0)

**Focus**: Post-alert token monitoring — detect price milestones, ATH, and clean up dead tokens.

#### New Feature: Milestone Tracker Loop (every 5m)
- Scans all alerted, non-dead tokens with AlertOutcome records
- Batch-fetches live DEX data from DexScreener (30 per call)
- Compares current MCap with alert-time FDV/MCap (prefers FDV as reference)
- **ATH Detection**: Tracks all-time-high MCap per token
  - Notifies when >10% above previous ATH
  - 1-hour cooldown between ATH notifications per token
- **Multiplier Milestones**: Detects whole-number multipliers (2x, 3x, 4x, ...)
  - Starts notifying from 2x by default (configurable min_multiplier_notify)
  - Only fires once per multiplier level (stored as last_milestone_x)
- **Dead Token Cleanup**:
  - Detects tokens with liquidity < $200 OR MCap < $500
  - Requires 24h continuous below threshold before marking dead
  - Recovery above thresholds resets the countdown
  - Sends 💀 death notification, permanently stops monitoring
  - Tokens older than 30 days auto-expire from tracking

#### New Telegram Alert Types
- 🏆 ATH notification — shows new ATH MCap, alert-time reference, gain %
- 🚀 Multiplier milestone — shows current multiplier, MCap, gain %
- 💀 Dead token — shows current vs alert MCap, days since alert

#### DB Changes (Migration 006)
- `alert_outcomes.ath_mcap` — all-time high MCap observed post-alert
- `alert_outcomes.last_milestone_x` — highest whole-number multiplier notified
- `alert_outcomes.milestone_notified_at` — timestamp of last milestone notification
- `tokens.is_dead` — boolean, token marked dead by milestone tracker
- `tokens.dead_since` — timestamp when token first dropped below thresholds

#### Config: `MilestoneTrackerConfig` (12 fields)
- scan_interval_seconds: 300
- dead_liq_threshold_usd: 200
- dead_mcap_threshold_usd: 500
- dead_confirmation_hours: 24
- max_token_age_days: 30
- batch_size: 30
- notify_ath: true
- notify_multiplier: true
- min_multiplier_notify: 2
- ath_cooldown_seconds: 3600

**New Loops**: 17 total (was 16)

---

### Iteration #9 — 2026-03-15 — Missed Gem Analysis & Gate-Pending Re-Scan (v1.1)

**Focus**: Data-driven analysis of why high-scoring tokens were not alerted + system improvements.

#### Production Data Analysis (336K tokens, 474 alerts, 26 gems)

**Key Findings**:

| Blocker | Tokens Blocked (score ≥ 0.45) | Avg Score | Max Score |
|---------|------|-----------|-----------|
| MCap gate ($50K) | 107 | 0.578 | 0.842 |
| Duplicate symbol | 68 | 0.537 | 0.882 |
| Score threshold | 471 | 0.408 | 0.593 |
| Liquidity gate | 1 | 0.536 | N/A |

**Root Cause**: The $50K MCap hard gate was the #1 missed gem blocker.
- COOK scored 0.7471 with MCap $48,547 — just $1,453 below the gate
- LANCER scored 0.8424 with MCap $14,891 — excellent token rejected
- 107 tokens scored above threshold but were permanently rejected with NO re-check

**Second Problem**: Duplicate symbol check blocked different contracts.
- ToruCeoAI scored 0.8824 but blocked because a DIFFERENT contract with same symbol was alerted
- 68 tokens with different contract addresses blocked by symbol-only matching

#### Changes Made

**1. Gate-Pending Re-Scan Loop (Loop 16, every 10m)**:
- Tokens that pass scoring but fail MCap/Liq gate → `gate_pending=True`
- New loop re-checks these tokens every 10 minutes
- If MCap/Liq grows above gate → alert fires
- Max 18 re-checks (3 hours) before giving up
- Prevents the permanent-rejection problem that caused 107 missed gems

**2. MCap Gate Lowered: $50K → $25K**:
- Gem tokens were alerted at MCap as low as $51K (MONTRA)
- Many gems start at $50-80K MCap — lowering catches them earlier
- Combined with gate-pending re-scan, tokens starting at $10-25K still get re-checked

**3. Duplicate Symbol Check Fixed: Contract-Address Aware**:
- Old: Same `symbol` string → blocked (even if different contract)
- New: Only blocks if same `contract_address` (true duplicate)
- Different contracts with same symbol are different projects → now allowed
- Prevents blocking 68+ legitimate tokens

#### DB Changes (Migration 007)
- `tokens.gate_pending` — boolean, token awaiting MCap/Liq gate re-check
- `tokens.gate_check_count` — integer, number of re-check attempts
- `tokens.last_gate_check` — timestamp of last re-check

#### Config: `GatePendingConfig` (4 fields)
- enabled: true
- recheck_interval_seconds: 600
- max_rechecks: 18
- batch_size: 30

#### Discovery Source Alert Rates (from DB analysis)
| Source | Total Scored | Alerted | Alert % |
|--------|-------------|---------|---------|
| wallet_conviction | 37 | 24 | 64.9% |
| community_takeover | 6 | 4 | 66.7% |
| binance_trending | 71 | 39 | 54.9% |
| profile | 591 | 289 | 48.9% |
| boost_top | 9 | 3 | 33.3% |
| binance_top_search | 107 | 27 | 25.2% |
| binance_social_hype | 48 | 12 | 25.0% |
| boost_latest | 69 | 16 | 23.2% |
| firehose | 260,088 | 37 | 0.01% |

**New Loops**: 18 total (was 17)

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
