---
description: "Scan DexScreener for breakout tokens — delayed movers on Base"
mode: agent
tools: ["codebase", "fetch"]
---

# Task: Scan for Breakout Tokens (Delayed Movers)

Find Base tokens that launched days ago but are NOW gaining momentum — the firehose missed them but DexScreener trending caught them.

## Context — Read First

- [SKILL.md](../base-sleuth/SKILL.md) — Loop 3 breakout scanner architecture
- [clanker_intel.md](../base-sleuth/references/clanker_intel.md) — DexScreener endpoints
- [strategy_evolution.md](../base-sleuth/references/strategy_evolution.md) — Breakout rationale
- [token_blacklist.md](../base-sleuth/references/token_blacklist.md) — Patterns to avoid

## Scan Steps

### 1. Fetch DexScreener Trending Signals
```
GET https://api.dexscreener.com/token-boosts/top/v1      → most active boosts
GET https://api.dexscreener.com/token-boosts/latest/v1   → recently boosted
GET https://api.dexscreener.com/token-profiles/latest/v1 → custom profiles (branding effort)
```
Filter: `chainId == "base"` only.

### 2. Batch Fetch Full Metrics
```
GET https://api.dexscreener.com/tokens/v1/base/{addr1},{addr2},...
```
Max 30 addresses per request.

### 3. Apply Breakout Filters
- Liquidity ≥ $10,000
- Volume 24h ≥ $5,000
- Momentum: buys_1h ≥ 20 OR price_change_1h ≥ 50%
- Age ≤ 14 days (skip ancient tokens)

### 4. Score Through Pipeline
Same 5-stage pipeline as regular scan, plus:
- Breakout bonus: +0.10 flat additive (DexScreener trending = marketing effort)
- Alert threshold: ≥ 0.45

### 5. Report

For each result, show full metrics:
- MCap, FDV, Liquidity, Volume, Price changes
- Buy/sell pressure analysis, Vol/Liq ratio
- Age (when launched), momentum trajectory
- DexScreener + Clanker + Uniswap links

Categorize:
- 📈 **BREAKOUT GEMS**: Score ≥ 0.45
- 👀 **BREAKOUT WATCHLIST**: Score 0.25–0.44
- Summary: total Base tokens on DexScreener, how many qualified

{{{ input }}}
