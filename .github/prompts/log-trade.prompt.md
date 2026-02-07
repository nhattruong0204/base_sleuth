---
description: "Log a paper trade in the trading journal and track performance"
mode: agent
tools: ["changes", "codebase", "fetch"]
---

# Task: Log a Paper Trade

You are the Base Sleuth agent logging a paper trade.

## Context — Read First

- [trading_journal.md](../base-sleuth/references/trading_journal.md) — Current trade log and portfolio balance
- [strategy_evolution.md](../base-sleuth/references/strategy_evolution.md) — Current strategy version
- [SKILL.md](../base-sleuth/SKILL.md) — Entry/exit criteria and position sizing rules

## Rules

1. Follow the trade template in `trading_journal.md` exactly
2. Calculate position size based on SKILL.md rules (max 5%, scale by conviction)
3. Set stop loss at -30%, TP1 at +50%, TP2 at +100%, TP3 at +300%
4. Update the Performance Summary table at the top of the journal
5. If this is trade #10, #20, etc. → also update `strategy_evolution.md` with a new iteration

## For Entry Trades

Fetch live data:
- DexScreener: `https://api.dexscreener.com/latest/dex/tokens/{contract_address}`
- Clanker: `https://www.clanker.world/api/tokens?address={contract_address}`

Fill in ALL fields: liquidity, mcap, volume, buys/sells, price change, social links, description.
Calculate a pipeline score based on the 5-stage criteria in SKILL.md.

## For Exit Trades

- Calculate P&L percentage and dollar amount
- Update Current Balance in Performance Summary
- Fill the OUTCOME section with what happened and learnings
- Note which stages correctly predicted the outcome

## Trade Details

{{{ input }}}
