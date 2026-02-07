---
description: "Scan DexScreener for breakout tokens — delayed movers on Base"
mode: agent
tools: ["codebase", "fetch"]
---

# Task: Scan for Breakout Tokens (Delayed Movers)

You are running a breakout scan — finding Base tokens that launched days ago but are NOW gaining momentum. These are tokens the firehose missed.

## Context — Read First

- [SKILL.md](../../base-sleuth/SKILL.md) — Loop 3 breakout scanner architecture
- [clanker_intel.md](../../base-sleuth/references/clanker_intel.md) — DexScreener breakout endpoints
- [strategy_evolution.md](../../base-sleuth/references/strategy_evolution.md) — Iteration #1 breakout rationale
- [token_blacklist.md](../../base-sleuth/references/token_blacklist.md) — Patterns to avoid

## Scan Steps

### 1. Fetch DexScreener Trending Signals
Query all three discovery endpoints for Base tokens:
```
GET https://api.dexscreener.com/token-boosts/top/v1      → most active boosts
GET https://api.dexscreener.com/token-boosts/latest/v1   → recently boosted
GET https://api.dexscreener.com/token-profiles/latest/v1 → custom profiles
```
Filter: `chainId == "base"` only.

### 2. Batch Fetch Full Metrics
For all Base tokens found, get detailed pair data:
```
GET https://api.dexscreener.com/tokens/v1/base/{addr1},{addr2},...
```
Up to 30 addresses per request.

### 3. Apply Breakout Quality Filters
Minimum thresholds:
- Liquidity ≥ $10,000
- Volume 24h ≥ $5,000
- At least ONE: buys_1h ≥ 20 OR price_change_1h ≥ 50%
- Age ≤ 14 days (skip ancient tokens)

### 4. Score Through 5-Stage Pipeline
Same pipeline as regular scan, but with:
- Breakout bonus: +0.10 flat additive
- Alert threshold: ≥ 0.45

### 5. Deep Dive on Winners
For any gem (≥0.45) or strong watchlist (≥0.35), show:
- Full metrics breakdown (liq, vol, txns, price changes)
- Buy/sell pressure analysis
- Vol/Liq ratio
- Age analysis (when was it launched?)
- DexScreener + Clanker links
- Signal strength assessment

### 6. Report
Format results as:
- 📈 BREAKOUT GEMS: Score ≥ 0.45
- 👀 BREAKOUT WATCHLIST: Score 0.25–0.44
- Summary stats: how many Base tokens on DexScreener, how many qualified
