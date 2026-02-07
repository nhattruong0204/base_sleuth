---
description: "Review strategy performance and evolve scoring weights"
mode: agent
tools: ["changes", "codebase", "fetch"]
---

# Task: Strategy Review & Evolution

You are the Base Sleuth agent reviewing trading performance and evolving the strategy.

## Context — Read First

- [trading_journal.md](../base-sleuth/references/trading_journal.md) — All trade logs
- [strategy_evolution.md](../base-sleuth/references/strategy_evolution.md) — Current strategy + history
- [SKILL.md](../base-sleuth/SKILL.md) — Full strategy definition
- [token_blacklist.md](../base-sleuth/references/token_blacklist.md) — Current avoid patterns
- [config.py](../clanker_tracker/config.py) — FilteringConfig with weights and thresholds

## Review Steps

### 1. Calculate Performance Metrics
From the trading journal, compute:
- Win rate (target: >35%)
- Average win % vs average loss %
- Profit factor (gross wins / gross losses)
- Net P&L
- Largest win and largest loss
- Average hold time
- Champagne vs non-champagne win rates

### 2. Stage Accuracy Analysis
For each scoring stage, check:
- Did high S2 (DEX metrics) scores correlate with wins?
- Did S3 (momentum) predict short-term gains?
- Did S4 (smart money) matches lead to winners?
- Was champagne bonus justified?

### 3. Propose Adjustments
Based on data:
- Should any weights be increased/decreased?
- Should any thresholds be tightened/loosened?
- Are there new patterns to add to the blacklist?
- Are there new smart money wallets to track?

### 4. Update Files
- Add a new iteration to `strategy_evolution.md`
- Update weights in `config.py` and `config.example.yaml` if changed
- Update `SKILL.md` pipeline table if architecture changed
- Add new patterns to `token_blacklist.md` if discovered

## Review Focus

{{{ input }}}
