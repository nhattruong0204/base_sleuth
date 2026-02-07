---
description: "Scan for gems right now — fetch live Clanker + DexScreener data"
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

### 1. Fetch Champagne Tokens (Highest Priority)
```
GET https://www.clanker.world/api/tokens?champagne=true&sort=desc
```
These are the 0.02% curated tokens — 58% have real liquidity.

### 2. Fetch Latest Non-Bankr Tokens
```
GET https://www.clanker.world/api/tokens?sort=desc
```
Filter out tokens where description contains "bankrbot".

### 2.5. Scan DexScreener for Breakout Tokens (Delayed Movers)
```
GET https://api.dexscreener.com/token-boosts/top/v1
GET https://api.dexscreener.com/token-boosts/latest/v1
GET https://api.dexscreener.com/token-profiles/latest/v1
```
Filter to chainId=base, fetch metrics, apply quality thresholds (liq≥$10K, vol≥$5K, momentum).

### 3. Check DexScreener for Survivors
For each candidate, fetch:
```
GET https://api.dexscreener.com/latest/dex/tokens/{contract_address}
```
Skip tokens with no pairs (dead on arrival).

### 4. Score Each Candidate

Apply the 5-stage pipeline from SKILL.md:
- S1: Scam keywords, name check
- S2: Liquidity ≥$500, volume, buy/sell ratio
- S3: Momentum — buys/hr, price change %
- S4: Any smart money wallets from watchlist?
- S5: Social links, description quality
- Champagne bonus: +15%
- Breakout bonus: +10% (for DexScreener trending tokens)

### 5. Report

For each token scoring ≥ 0.45, report:
```
🚀 TOKEN_NAME ($SYMBOL) — Score: XX%
🍾 Champagne: Yes/No  |  Platform: direct/bankr/clawnch
💰 Liq: $XX,XXX  |  Vol 24h: $XX,XXX  |  MCap: $XX,XXX
📈 Buys/hr: XX  |  Price 1h: +XX%  |  Price 24h: +XX%
🔗 DexScreener: https://dexscreener.com/base/{addr}
🔗 Clanker: https://www.clanker.world/clanker/{addr}
```

{{{ input }}}
