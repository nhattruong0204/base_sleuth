# Changelog

All notable changes to **Base Sleuth** are documented here.

---

## [1.1.0] — 2026-03-15

### Added — Gate-Pending Re-Scan (Loop 16) & Missed Gem Fixes

Data-driven improvements based on analysis of 336K tokens, 474 alerts, and 26 gems.
107 high-scoring tokens (0.45-0.88) were permanently rejected because MCap was below
the $50K gate at scan time — many grew past it later. This release fixes that.

#### Gate-Pending Re-Scan Loop
- Tokens that pass scoring but fail hard MCap/Liq gate → marked `gate_pending=True`
- New loop re-checks these tokens every 10 minutes via DexScreener
- If MCap/Liq grows above gates → alert fires (with full notification + paper trading)
- After 18 failed re-checks (3 hours) → gives up
- Config: `GatePendingConfig` (enabled, recheck_interval_seconds, max_rechecks, batch_size)

#### MCap Gate Lowered: $50K → $25K
- Production data showed gems were alerted at MCap as low as $51K
- Combined with gate-pending re-scan, tokens starting at $10-25K still get monitored

#### Duplicate Symbol Check Fixed: Contract-Address Aware
- Old behavior: Same `symbol` string → blocked (even if completely different token)
- New behavior: Only blocks if same `contract_address` (true duplicate)
- 68 legitimate tokens with unique contracts were blocked by old logic

#### Database Migration (007)
- `tokens.gate_pending` — boolean, token awaiting MCap/Liq gate re-check
- `tokens.gate_check_count` — integer, number of re-check attempts
- `tokens.last_gate_check` — timestamp of last re-check
- Index on `gate_pending` for efficient queries

---

## [1.0.0] — 2026-03-15

### Added — Milestone Tracker (Loop 15)

New async loop that monitors all alerted tokens for price milestones, ATH, and dead token cleanup.

#### ATH Detection
- Tracks all-time-high MCap for every alerted token
- Sends 🏆 notification when MCap exceeds previous ATH by >10%
- 1-hour cooldown between ATH notifications per token

#### Multiplier Milestones (1x, 2x, 3x, 4x, ...)
- Compares current MCap with alert-time FDV (fallback to MCap)
- Sends 📈/🚀 notification on each new whole-number multiplier
- Skips 1x by default (configurable `min_multiplier_notify`)
- Only fires once per level — stores `last_milestone_x` in DB

#### Dead Token Cleanup
- Detects tokens with liq < $200 OR MCap < $500
- 24h confirmation window before marking dead (recovery resets countdown)
- Sends 💀 death notification with PnL comparison
- Dead tokens permanently excluded from future scans
- Tokens older than 30 days auto-expire from tracking

#### New Telegram Alert Types
- `notify_milestone()` — ATH + multiplier alerts with chart/swap buttons
- `notify_dead_token()` — death notification with alert vs current comparison

#### DB Migration 006
- `alert_outcomes`: `ath_mcap`, `last_milestone_x`, `milestone_notified_at`
- `tokens`: `is_dead`, `dead_since`

#### Config — `MilestoneTrackerConfig` (12 fields)
- `enabled`, `scan_interval_seconds` (300), `dead_liq_threshold_usd` (200), `dead_mcap_threshold_usd` (500)
- `dead_confirmation_hours` (24), `max_token_age_days` (30), `batch_size` (30)
- `notify_ath`, `notify_multiplier`, `min_multiplier_notify` (2), `ath_cooldown_seconds` (3600)

#### Files Changed
- `models.py` — New columns on `Token` + `AlertOutcome`
- `config.py` — `MilestoneTrackerConfig` class + `AppConfig` integration
- `main.py` — `_milestone_tracker_loop()` method, backoff key, loop registration (17 total)
- `notifier.py` — `notify_milestone()` + `notify_dead_token()` methods
- `bot_commands.py` — Added "Milestone Tracker" to `/status` loop health
- `alembic/versions/006_add_milestone_tracking.py` — Schema migration
- `config.yaml` / `config.example.yaml` — New `milestone_tracker` section
- `tests/test_milestone_tracker.py` — 28 unit tests

---

## [0.9.1] — 2026-03-03

### Fixed — Platform Name Impersonation Detection

Scam tokens named "CLAWNCH", "CLANKER", "UNISWAP" etc. were passing the scoring pipeline (51% score) by wash-trading DEX metrics to look legitimate — zero smart money, zero context, but high fake volume/momentum.

#### Impersonation Reject Gate (Stage 1)
- New: `impersonation_names` config list (19 platform/ecosystem names)
- Exact match on token name or symbol → instant reject with reason `Impersonation: clawnch`
- Covers: clanker, clawnch, bankr, uniswap, dexscreener, coinbase, ethereum, solana, opensea, metamask, basescan, etherscan, aave, compound, aerodrome, warpcast, farcaster, base chain, basechain

#### Duplicate Symbol Cooldown Extended
- `duplicate_symbol_cooldown_seconds`: 7,200 (2h) → **86,400 (24h)**
- Prevents same scam symbol from re-alerting every 2 hours

#### Files Changed
- `config.py` — Added `impersonation_names` field, raised cooldown default to 24h
- `filters.py` — Stage 1 `_stage1_instant_reject()` now checks impersonation list
- `config.yaml` / `config.example.yaml` — New impersonation list + cooldown update
- `token_blacklist.md` — Documented impersonation pattern

---

## [0.9.0] — 2026-03-03

### Added — Arkham Deep Intelligence Suite (4 new features)

Expanded Arkham Intel integration from 4 to **8 capabilities** — institutional-grade on-chain intelligence for token scoring and post-alert monitoring.

#### Token Holder Intelligence (THI)
- `analyze_holder_quality()` — composite method: fetch top holders → batch identify → score quality
- Uses `GET /token/holders/{chain}/{address}` + `POST /intelligence/address_enriched/batch`
- Classifies holders: fund/VC, exchange, whale, smart money, scam-tagged
- Returns `holder_score` (0-1), `fund_vc_count`, `concentration_top10`, `risk_flags`
- Scoring: Fund/VC (2+) +0.12, Known entities (3+) +0.08, Concentration >80% -0.10, Exchange (5+) -0.05

#### Deployer Profiling
- `profile_deployer()` — composite method: contract intel → deployer identity → risk assessment
- Uses `GET /intelligence/contract/{chain}/{address}` + batch address enrichment
- Returns `risk_level` (safe/unknown/risky/dangerous), `deployer_score` (-1 to +1)
- Scoring: Known builder +0.10, Scam deployer → score=0 (hard reject), Proxy -0.08, Serial deployer -0.05

#### Token Flow Monitor (new loop — every 300s)
- `TokenFlowMonitor` class monitors tokens with open paper positions
- Uses `GET /token/top_flow/{chain}/{address}` (heavy endpoint, 1 req/sec)
- Detects: dump warnings (known entity outflows ≥$10K), accumulation signals (known entity inflows ≥$10K)
- Sends Telegram alerts with entity names, USD amounts, Arkham Explorer links

#### Portfolio Watch (new loop — every 600s)
- Monitors top 30 tracked wallet portfolios via `GET /balances/address/{address}`
- Detects new Base token positions above $500 threshold
- Cross-references with tracked tokens → conviction alerts
- Tracks seen positions to only alert on NEW entries

#### Scoring Pipeline Integration
- `filters.py`: Arkham holder quality + deployer profiling called in `evaluate()` after Binance enrichment
- `_compute_weighted_score()` extended with 11 new additive/penalty adjustments
- `set_arkham_client()` injection method on `TokenFilter`

#### New Alert Type
- `notify_flow_alert()` — dump warning / accumulation signal formatting
- Shows token name, flow direction, total USD, top 5 movers with Arkham identities
- Links to DexScreener + Arkham Explorer

#### Config — ~60 new fields in `ArkhamConfig`
- THI: `holder_intel_enabled`, `holder_known_entity_bonus`, `holder_fund_vc_bonus`, `holder_concentration_penalty`, `holder_max_top10_pct`, `holder_exchange_risk_penalty`
- Deployer: `deployer_profiling_enabled`, `deployer_known_builder_bonus`, `deployer_scam_reject`, `deployer_proxy_penalty`
- Flow: `flow_monitoring_enabled`, `flow_poll_interval_seconds`, `flow_min_usd`, `flow_alert_threshold_usd`
- Portfolio: `portfolio_watch_enabled`, `portfolio_poll_interval_seconds`, `portfolio_min_position_usd`

#### New Loops (16 total, was 14)
- Token Flow Monitor (300s) — dump/accumulation detection for held tokens
- Portfolio Watch (600s) — wallet position surveillance for top 30 wallets

---

## [0.8.0] — 2026-03-03

### Added — Binance Skills Hub Integration

Integrated 6 Binance Web3 Skills (public APIs, no key needed) for CEX-grade token intelligence.

#### New Module: `binance_client.py`
- `BinanceSkillsClient` — 6 API methods:
  1. `fetch_trending_tokens()` — Unified Token Rank (trending/top-search on Base chain 8453)
  2. `fetch_social_hype()` — Social buzz leaderboard with sentiment analysis
  3. `audit_token()` — Token security audit (honeypot/scam/rug detection)
  4. `fetch_token_dynamic()` — Rich market data with KOL/smart money/pro holder counts
  5. `search_tokens()` — Cross-chain token lookup by keyword/address
  6. `fetch_wallet_balance()` — On-chain wallet positions on Base
- `BinanceTrendingScanner` — Discovery loop class:
  - Fetches 3 data sources in parallel: trending, top search, social hype
  - Merges & dedupes, skips blue-chip addresses, ingests new tokens for scoring
  - Creates Token records with `discovery_source="binance_trending+social_hype"`

#### Binance Trending Loop (every 180s)
- Discovers Base tokens trending on Binance Web3
- Tokens enter eval pipeline for full scoring + Binance audit + enrichment

#### Scoring Enrichment (every eval)
- **Security Audit**: BLOCKED/HIGH risk → reject or heavy penalty; LOW risk → +0.05 bonus; Tax >10% → -0.10
- **Dynamic Data**: KOL holders → +0.05; Binance smart money → +0.08; Pro holders (3+) → +0.03
- `set_binance_client()` injection into `TokenFilter`

#### Config — `BinanceSkillsConfig` (26 fields)
- `enabled`, `audit_enabled`, `enrich_enabled`, trending params, score bonuses/penalties

#### Files Changed
- `clanker_tracker/binance_client.py` — New module (524 lines)
- `clanker_tracker/config.py` — `BinanceSkillsConfig` class
- `clanker_tracker/filters.py` — Binance audit + enrichment in `_compute_weighted_score()`
- `clanker_tracker/main.py` — `_binance_trending_loop()`, client initialization
- `config.yaml` / `config.example.yaml` — Full Binance Skills configuration

---

## [0.7.0] — 2026-02-15

### Added — Nansen Telethon Listener + Paper Trading + Multi-Wallet Conviction

#### Nansen Telethon Listener
- Persistent Telethon connection to @NansenBot Telegram
- Parses wallet buy/sell messages and auto-adds wallets to tracking
- Auto-ingests tokens bought by Nansen-tagged wallets

#### Paper Trading Engine
- Opens paper positions on alerted tokens
- Monitors SL/TP triggers: SL -30%, TP1 +50%, TP2 +100%, TP3 +300%, time stop 24h
- Tracks realized PnL, win rate, peak/trough prices
- New `paper_positions` DB table (migration 005)

#### Multi-Wallet Conviction
- Detects tokens bought by 2+ distinct tracked wallets within 6h
- Fires high-conviction alerts with wallet count + details
- Auto-ingests tokens not in DB if DexScreener shows liq ≥ $10K

#### Champagne Eval
- Re-evaluates champagne tokens that had no DEX data at discovery
- Lower threshold (0.30 vs 0.45) — champagne tokens are pre-vetted
- Retries up to 3h before giving up

#### New Loops (14 total, was 8)
- Nansen Telethon (persistent), Champagne Eval (600s), Paper Trading (300s), Multi-Wallet Conviction (300s), Wallet Watch (120s), Binance Trending (180s)

---

## [0.6.0] — 2026-02-10

### Added — Smart Wallet Tracking (Arkham Intel Integration)

Integrated Arkham Intel API to discover, track, and monitor profitable wallets on Base chain. System now generates wallet buy alerts and high-priority conviction signals.

#### Arkham Intel Client (`arkham_client.py`)
- Fetches fomo-tagged wallets from `/intelligence/addresses/updates?tagId=fomo-user&chain=base`
- Analyzes wallet PnL via `/history/address/{addr}?chain=base` at 1d/7d/30d windows
- Monitors swaps via `/swaps?from={addrs}&chains=base` (heavy endpoint, 1 req/sec)
- Classifies wallets: Tier 1 (3/3 profitable), Tier 2 (2/3), Tier 3

#### New DB Tables (migration 004)
- `smart_wallets` — address, tier, PnL metrics, Arkham entity/label, active status
- `wallet_swaps` — buy/sell records with tx_hash dedup, conviction flag

#### Wallet Buy Alerts
- Telegram alert when tracked wallet buys any token on Base
- Shows wallet tier badge (🥇/🥈/🥉), PnL history, buy amount
- Inline buttons: DexScreener, Uniswap, Arkham Explorer

#### Conviction Alerts (🔥 highest priority)
- Fires when a tracked wallet buys a token already in our database
- Cross-references against tokens table for exact contract match
- Separate high-visibility format: 🔥 CONVICTION SIGNAL + 🚨 ALERTED badge

#### Stage 4 — Smart Money (now wired to DB)
- `_stage4_smart_money()` queries WalletSwap + SmartWallet tables
- Tiered scoring: T1 wallets contribute +0.10, T2 +0.05 per wallet
- Auto-updates `data/smart_money_wallets.txt` on each sync

#### New Loops (8 total, was 6)
- Wallet Sync (3600s) — periodic Arkham wallet refresh + performance analysis
- Wallet Monitor (60s) — swap polling + conviction detection

#### Bot Commands
- New `/wallets` command — tracked wallet summary, tiers, 24h swaps, convictions
- Added 👛 Wallets button to main dashboard keyboard

#### Config
- New `arkham:` section in config.yaml with all settings
- `ARKHAM_API_KEY` environment variable override

---

## [0.5.0] — 2026-02-09

### Changed — Data-Driven Filter Overhaul (anti-trash + organic gem detection)

Deep analysis of 47,045 tokens and 88 alerts revealed **63.6% trash rate** with 0 winners from firehose. Root causes: bot-buy spray undetected, context score inflating trash, no liquidity gate.

#### Bot Spray Detection (Stage 2)
- New: If `buys_1h > 250` AND `avg buy < $100` → score capped at 0.05 (detected 100-2000+ fake buys on trash tokens)
- New: Liquidity tiering — $1K→0.15, $5K→0.20, $10K→0.30 (was flat 0.25)
- New: Volume/liquidity demand pressure bonus (vol > 2× liq → +0.08)
- New: Average buy quality bonus (avg > $200 → +0.05, signals real traders)

#### Organic Activity Signal (Stage 3)
- New: Organic signal for unpaid-but-real tokens: buys≥15, vol≥$3K, avg_buy≥$100, liq≥$3K → +0.10
- Capped buy count rewards: diminishing returns above 100 buys (prevents bot spray inflation)

#### Source-Aware Scoring
- Firehose scores multiplied by 0.70 penalty (0 winners historically, 84% trash)
- `community_takeover` gets +0.05 bonus (0% trash rate historically)
- `boost_top` gets +0.03 bonus (33% win rate historically)

#### Raised Pre-Filter Bars
- `min_pool_liquidity_usd`: $1,000 → $3,000
- `min_volume_1h_usd`: $50 → $500
- `min_holders`: 3 → 5
- `min_buys_1h`: 5 → 10

#### Rebalanced Scoring Weights
- `weight_metrics`: 1.0 → 2.0 (DEX metrics = primary signal)
- `weight_context`: 1.0 → 0.3 (social links don't predict survival)

#### Alert-Time Hard Gates
- New: `min_alert_liquidity_usd`: $5,000 (alongside existing $50K MCap gate)

#### New Config Fields
- `bot_buy_threshold`, `bot_avg_buy_max_usd`, `firehose_score_penalty`, `min_alert_liquidity_usd`

### Added — `/analysis` Command
- Live performance analysis of all alerted tokens with DexScreener batch fetch
- Shows: winners/OK/trash breakdown, total PnL simulation, per-token details
- Inline keyboard for 1d / 7d / 14d / 30d time ranges
- Message splitting for large result sets

---

## [0.4.0] — 2026-02-08

### Added — `/realpnl` Real-Time PnL Command
- New Telegram command: `/realpnl <days>` (e.g. `/realpnl 1`, `/realpnl 7`, `/realpnl 14`)
- Fetches **live MCap/FDV** from DexScreener for every alerted token in the timeframe
- Compares current FDV vs alert-time FDV to compute per-token return multiplier
- Simulates 1 SOL per trade with GMGN-style fee breakdown (2.5% buy + 2.5% sell)
- Shows: total invested, total returned, net profit, win rate, winner/loser counts
- Inline keyboard buttons for quick 1d / 7d / 14d switching
- Rugged token detection (zero DexScreener data = 💀)

---

## [0.3.0] — 2026-02-08

### Added — PID Feedback Loop (self-improving alerts)
- **6th async loop** (`_outcome_loop`) — re-checks alerted tokens at 1h / 6h / 24h
- **`AlertOutcome` model** + `alert_outcomes` DB table — stores alert-time snapshot (score, MCap, FDV, liquidity, vol, buys) and checkpoint metrics at each re-check
- **Outcome classification**: `gem` (MCap 2x+), `survivor` (still alive), `dead` (liquidity < $500)
- **PID auto-tune**: adjusts `score_threshold` based on rolling gem/dead ratio
  - Target: 25% gem rate
  - Dead rate > 60% → raises threshold aggressively
  - Good gem rate → can lower threshold slightly
  - Bounded between 0.40–0.75, step size 0.01
- **`PIDConfig`** in `config.py` — all PID parameters are configurable
- **Alembic migration** `003_add_alert_outcomes.py` — new table with indexes

### Added — Market Data in Alert Messages
- Alert messages now show **MCap, FDV, Liquidity, Vol 1h** at the time of alert
- `_fmt_number()` helper for compact display ($1.23M, $350K, etc.)
- `FilterResult` now carries the `metrics` snapshot for downstream use

### Changed — Tighter Alert Filters
- **Score threshold raised**: 0.45 → **0.55** (cuts ~60% of trash alerts)
- **Breakout bonus reduced**: 0.10 → **0.05** (prevents inflating borderline tokens)
- **Hard MCap gate**: tokens with MCap < **$50K** at alert time are blocked
- **Duplicate symbol cooldown**: same `$SYMBOL` won't alert within **2 hours** (blocks fred×3, SONAR×3 spam)

### Fixed
- **`_env_override` bug**: empty Docker env vars (blank `TELEGRAM_BOT_TOKEN`) no longer overwrite valid `config.yaml` values — was silently disabling Telegram bot

---

## [0.2.0] — 2026-02-07

### Added — Gainers Scanner & Backfill
- **Gainers scanner** — 5th loop using DexScreener search endpoint with rotating keywords + community takeover feed
- **Backfill on first run** — pages backward through Clanker API to catch tokens from last 12 hours
- **Blue-chip blacklist** — 13 known infrastructure tokens (WETH, cbBTC, USDC, etc.) are auto-skipped
- **`max_fdv_usd` cap** ($50M) on gainers — prevents mega-cap tokens from alerting
- **Time-since-launch** display in alert messages

### Fixed
- **cbBTC false positive** — $2B FDV token slipped through gainers scanner; hardened with address blacklist + FDV cap
- **`context_resolver.py` UniqueViolationError** — checks for existing context before insert

---

## [0.1.0] — 2026-02-06

### Added — Initial Release
- **4 async scanning loops**: firehose (30s), champagne (120s), breakout (180s), eval pipeline (10s)
- **5-stage scoring pipeline**: pre-filter → reject gate → DEX metrics → momentum → smart money → context
- **Bankr detection** — 92% of Clanker launches auto-skipped via description parsing
- **Champagne tag priority** — curated tokens get bonus and skip Stage 1
- **Breakout scanner** — DexScreener trending/boosted/profiles for delayed movers on Base
- **PostgreSQL persistence** — async via asyncpg + SQLAlchemy 2.0
- **Alembic migrations** — schema versioning for production
- **Docker Compose** — PostgreSQL + bot containers for 24/7 VPS deployment
- **Interactive Telegram bot** — commands (/status, /stats, /gems, /top, /scan, /config) + inline keyboard
- **Context resolver** — origin tracing (social URLs → page scrape → DDG search)
- **Smart money wallet tracking** — configurable wallet list for Stage 4 scoring
