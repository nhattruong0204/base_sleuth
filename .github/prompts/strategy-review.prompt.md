---
description: "Review strategy performance and evolve scoring weights"
mode: agent
tools: ["changes", "codebase", "fetch"]
---

# Task: Strategy Review & Evolution

Review Base Sleuth's trading performance and propose data-driven adjustments.

## Context — Read First

- [trading_journal.md](../base-sleuth/references/trading_journal.md) — All trade logs with outcomes
- [strategy_evolution.md](../base-sleuth/references/strategy_evolution.md) — Strategy history + current version
- [SKILL.md](../base-sleuth/SKILL.md) — Full strategy definition and pipeline weights
- [token_blacklist.md](../base-sleuth/references/token_blacklist.md) — Current avoid patterns
- [config.py](../../clanker_tracker/config.py) — FilteringConfig with weights and thresholds

## Review Steps

### 1. Performance Metrics
From the trading journal, compute:
- Win rate (target: >35%)
- Average win % vs average loss %
- Profit factor (gross wins / gross losses)
- Net P&L and equity curve
- Largest win and largest loss
- Average hold time
- Champagne vs non-champagne win rates
- Breakout vs firehose win rates

### 2. Stage Accuracy Analysis
For each scoring stage, check correlation with outcomes:
- S2 (DEX Metrics): Did high liquidity/volume predict survival?
- S3 (Momentum): Did buys/hr and price surge predict short-term gains?
- S4 (Smart Money): Did wallet matches produce winners?
- S5 (Context): Did social/origin quality matter?
- Champagne bonus: Is the +15% justified?
- Breakout bonus: Is the +10% justified?

### 3. Propose Adjustments
Based on data — no gut feelings:
- Should any weights increase/decrease?
- Should thresholds tighten/loosen?
- New patterns for the blacklist?
- New smart money wallets discovered?
- Should alert threshold (currently 0.45) change?

### 4. Update Files
- Add a new iteration to `.github/base-sleuth/references/strategy_evolution.md`
- Update weights in `config.py` + `config.example.yaml` if changed
- Update `.github/base-sleuth/SKILL.md` pipeline table if architecture changed
- Add discovered patterns to `.github/base-sleuth/references/token_blacklist.md`

## Review Focus

{{{ input }}}
