---
description: "Add a new scoring stage to the 5-stage filter pipeline"
mode: agent
tools: ["changes", "codebase", "problems", "usages"]
---

# Task: Add a New Scoring Stage

You are adding a new scoring signal to the Base Sleuth filtering pipeline.

## Context — Read First

- [filters.py](../../clanker_tracker/filters.py) — Current 5-stage pipeline implementation
- [SKILL.md](../base-sleuth/SKILL.md) — Pipeline architecture and weight definitions
- [config.py](../../clanker_tracker/config.py) — FilteringConfig with thresholds and weights
- [config.example.yaml](../../config.example.yaml) — Human-readable config template
- [notifier.py](../../clanker_tracker/notifier.py) — Alert formatting (stage_names list)

## Current Pipeline

| Stage | Weight | What |
|-------|--------|------|
| Pre-filter | gate | Skip Bankr, scam keywords |
| S1 Reject | gate | Scam names, champagne auto-pass |
| S2 DEX Metrics | 0.30 | Liquidity, volume, holders |
| S3 Momentum | 0.20 | Price surge, buys/hr, vol velocity |
| S4 Smart Money | 0.20 | Tracked wallet matches (Arkham + Nansen) |
| S5 Context | 0.15 | Social links, origin resolution |
| Champagne Bonus | 0.15 | Flat additive for curated tokens |
| Breakout Bonus | 0.10 | Flat additive for DexScreener trending |

## Implementation Checklist

1. **`filters.py`**: Add `_stageN_your_name(self, token, metrics) -> StageResult` method
2. **`filters.py`**: Call it in `evaluate()` after the appropriate stage
3. **`config.py`**: Add thresholds to `FilteringConfig` + a `weight_your_name` field
4. **`config.example.yaml`**: Add the new fields with comments
5. **`notifier.py`**: Add the stage name to `stage_names` list in `_format_message()`
6. **`.github/base-sleuth/SKILL.md`**: Update the pipeline table
7. **`.github/base-sleuth/references/strategy_evolution.md`**: Note the new stage

## Rules

- Return a `StageResult(passed=bool, score=0.0-1.0, reason="...")`
- Keep stages independent — each should work even if others fail
- The `evaluate()` method handles weighting — stages just return raw scores
- Update `stage_names` list order in BOTH `filters.py` and `notifier.py`
- Use `logger.info()` not `logger.debug()` for diagnostics you need to see in production
- Write at least one unit test under `tests/` for the new stage

## New Stage Description

{{{ input }}}
