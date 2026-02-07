# Base Sleuth — Smart Money Wallet Watchlist

Track profitable wallets on Base chain. When these wallets buy a new token early, it's a strong bullish signal.

**AGENT INSTRUCTION**: When you see these wallets enter a new token early, it's a buy signal (Stage 4). Update this list as you identify new smart money via on-chain analysis. Wallets in this file should also be added to `data/smart_money_wallets.txt` for machine-readable access.

---

## Tier 1 — Highest Confidence

These wallets have demonstrated consistent ability to find Base chain winners early.

| Wallet | Alias | Win Rate (Est) | Avg Gain | Tracked Since | Notes |
|--------|-------|----------------|----------|---------------|-------|
| [ADD AS DISCOVERED] | | | | | |

---

## Tier 2 — Moderate Confidence

Wallets that show promise on Base but need more data points.

| Wallet | Alias | Trades Tracked | Win Rate | Tracked Since | Notes |
|--------|-------|----------------|----------|---------------|-------|
| [ADD AS DISCOVERED] | | | | | |

---

## Tier 3 — Watching

New wallets being evaluated for Base chain activity.

| Wallet | Alias | Discovery Source | Tracked Since | Notes |
|--------|-------|-----------------|---------------|-------|
| [ADD AS DISCOVERED] | | | | |

---

## How to Identify Smart Money on Base

### 1. Reverse-Engineer Winners
When a Clanker token does 10x+:
- Check DexScreener for earliest buyers
- Look at BaseScan holder list — who bought in first 5 minutes?
- Cross-reference with known wallets
- If a wallet shows up in 3+ winners, add to Tier 3

### 2. Champagne Token Early Buyers
Champagne tokens are curated gems:
- Track who buys champagne tokens within first 10 minutes
- These wallets may have insider knowledge or good analysis tools
- Pattern: same wallet appears in multiple champagne tokens = smart money

### 3. Clanker Requestor Patterns
Some requestor addresses (`msg_sender` in Clanker API) repeatedly launch successful tokens:
- Track which requestor addresses have champagne-tagged tokens
- These addresses may be teams with track records

### 4. On-Chain Analysis via BaseScan
- Look for wallets with consistent >40% win rate on Base tokens
- Wallets that size up on winners (position size correlates with outcome)
- Wallets that exit before major dumps (pre-rug detection)

### 5. DexScreener Trader Tab
- Check top traders for winning tokens
- Look for repeat performers across multiple tokens
- Note: many are MEV bots — focus on wallets with human-like patterns

---

## Wallet Tracking Protocol

For each tracked wallet, maintain a log:

<!--
## [ALIAS] — 0xFIRST4...LAST4

**Full Address**: 0x...
**Tier**: 1 / 2 / 3
**Added**: YYYY-MM-DD
**Reason Added**: [Why this wallet caught attention]
**Discovery Source**: [reverse-engineering winner / champagne buyer / CT mention / etc.]

### Base Chain Activity Log
| Date | Token | Symbol | Entry Price | Exit Price | P&L | Hold Time |
|------|-------|--------|-------------|------------|-----|-----------|
| | | | | | | |

### Performance Summary
- Trades Tracked: X
- Win Rate: XX%
- Avg Gain on Wins: +XX%
- Avg Loss on Losses: -XX%
- Biggest Win: +XX% ($TOKEN)
- Typical Entry Size: $X,XXX
- Typical Hold Period: Xh–Xh

### Behavior Patterns
- Buys within X minutes of launch
- Prefers [champagne / direct / any] tokens
- Position size range: $X – $X
- Exit pattern: [scales out / dumps all at once]
- [Other patterns observed]
-->

---

## Wallets to Avoid (Smart Dumpers)

Wallets known to front-run retail, MEV bots, or pump-and-dump operators.

| Wallet | Alias | Reason | Platform | Date Added |
|--------|-------|--------|----------|------------|
| [ADD AS DISCOVERED] | | | | |

---

## Sync with Machine-Readable File

**IMPORTANT**: When adding a wallet to Tier 1 or Tier 2 here, also add the address to:
```
data/smart_money_wallets.txt
```
This file is loaded by the scoring pipeline (Stage 4: Smart Money) for automated matching.

Format: one lowercase address per line (no comments, no labels).
