---
description: "Scan for hidden gems right now — live Clanker + DexScreener data"
mode: agent
tools: ["codebase", "fetch"]
---

# Task: Scan for Hidden Gems Right Now

You are running a live gem scan on Base chain via Clanker API + DexScreener.

## Context — Read First

- [SKILL.md](../base-sleuth/SKILL.md) — Scoring criteria and entry thresholds
- [clanker_intel.md](../base-sleuth/references/clanker_intel.md) — API details and quirks
- [token_blacklist.md](../base-sleuth/references/token_blacklist.md) — Patterns to avoid
- [wallet_watchlist.md](../base-sleuth/references/wallet_watchlist.md) — Smart money to look for

## Scan Steps

### 1. Champagne Tokens (Highest Priority)
```
GET https://www.clanker.world/api/tokens?champagne=true&sort=desc
```
Only 0.02% of tokens — but 58% have real liquidity.

### 2. Latest Non-Bankr Tokens
```
GET https://www.clanker.world/api/tokens?sort=desc
```
Skip tokens where description contains "bankrbot" or "bankr terminal".

### 3. DexScreener Breakout Tokens
```
GET https://api.dexscreener.com/token-boosts/top/v1
GET https://api.dexscreener.com/token-boosts/latest/v1
GET https://api.dexscreener.com/token-profiles/latest/v1
```
Filter to `chainId=base`, then fetch full metrics. Apply: liq ≥ $10K, vol24h ≥ $5K, momentum required.

### 4. Get DexScreener Metrics for Candidates
```
GET https://api.dexscreener.com/latest/dex/tokens/{contract_address}
```
Skip tokens with no pairs (dead on arrival).

### 5. Score Each Candidate (5-Stage Pipeline)

- S1: Scam keywords, name validation
- S2: Liquidity ≥ $500, volume, buy/sell ratio, holder count
- S3: Momentum — buys/hr ≥ 5, price surge, vol/liq ratio
- S4: Smart money wallets from watchlist
- S5: Social links, description, origin quality
- Champagne bonus: +15% | Breakout bonus: +10%

### 6. Report Results

For each token scoring ≥ 0.45:
```
🚀 TOKEN ($SYMBOL) — Score: XX%
🍾 Champagne: Yes/No | Platform: direct/bankr/clawnch
💰 MCap $XXK · FDV $XXK · Liq $XXK
📊 Vol 24h: $XXK | Buys/hr: XX | Price 1h: +XX%
🔗 DexScreener | Uniswap | Clanker Page
CA: 0x...
```

{{{ input }}}
