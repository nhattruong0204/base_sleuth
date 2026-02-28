# Changelog

All notable changes to **Base Sleuth** are documented here.

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
