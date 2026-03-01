---
description: "Log a paper trade in the trading journal"
mode: agent
tools: ["changes", "codebase", "fetch"]
---

# Task: Log a Paper Trade

You are the Base Sleuth agent logging a paper trade with live market data.

## Context — Read First

- [trading_journal.md](../base-sleuth/references/trading_journal.md) — Current trade log and portfolio balance
- [strategy_evolution.md](../base-sleuth/references/strategy_evolution.md) — Current strategy version
- [SKILL.md](../base-sleuth/SKILL.md) — Entry/exit criteria and position sizing rules

## Position Sizing Rules

| Score | Size | Rationale |
|-------|------|-----------|
| 0.75+ | 5% ($500) | Strong gem — all signals aligned |
| 0.60–0.74 | 3-4% ($300-400) | Good signal |
| 0.45–0.59 | 2% ($200) | Speculative watchlist entry |
| Below 0.45 | SKIP | Not enough conviction |

## For Entry Trades

1. Fetch live data from DexScreener:
   ```
   GET https://api.dexscreener.com/latest/dex/tokens/{contract_address}
   ```
2. Record ALL fields: liquidity, mcap, fdv, volume, buys/sells, price changes, social links
3. Calculate pipeline score per SKILL.md 5-stage criteria
4. Set stops: SL at -30%, TP1 at +50%, TP2 at +100%, TP3 at +300%
5. Update the Performance Summary table at the top of the journal

## For Exit Trades

1. Fetch current price from DexScreener
2. Calculate P&L percentage and dollar amount
3. Update Current Balance in Performance Summary
4. Fill OUTCOME section: what happened, which stages predicted correctly
5. Note learnings for strategy evolution

## Milestone Reviews

Every 10th trade → also update `.github/base-sleuth/references/strategy_evolution.md` with a new iteration.

## Trade Details

{{{ input }}}
