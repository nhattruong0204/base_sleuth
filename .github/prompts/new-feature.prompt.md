---
description: "Add a new feature to Base Sleuth — reads all skill docs first"
mode: agent
tools: ["changes", "codebase", "fetch", "findTestFiles", "githubRepo", "problems", "usages"]
---

# Task: Add a New Feature to Base Sleuth

You are an expert Python async developer working on Base Sleuth, a Clanker token discovery agent.

## Step 1 — Load Full Context

Read these files before writing any code:

- [SKILL.md](../base-sleuth/SKILL.md) — Strategy, architecture, scoring pipeline
- [clanker_intel.md](../base-sleuth/references/clanker_intel.md) — API behavior, response shapes
- [strategy_evolution.md](../base-sleuth/references/strategy_evolution.md) — Current thresholds and weights
- [config.example.yaml](../config.example.yaml) — All current config fields
- [filters.py](../clanker_tracker/filters.py) — Current scoring pipeline
- [main.py](../clanker_tracker/main.py) — Current orchestrator loops
- [models.py](../clanker_tracker/models.py) — Current DB schema

## Step 2 — Plan

Before coding, explain:
1. Which files need to change
2. Which stages/loops are affected
3. Whether new config fields are needed
4. Whether the SKILL.md or references need updating

## Step 3 — Implement

Follow these rules:
- All code must be **async/await** — the entire codebase is async
- Use `structlog` for logging, never `print()`
- New config fields → add to both `config.py` (Pydantic) AND `config.example.yaml`
- New scoring signals → add as a stage in `filters.py`
- New DB columns → update `models.py`
- Test that all modules still import cleanly after changes

## Step 4 — Update Skill Docs

After implementing, update:
- `base-sleuth/SKILL.md` if pipeline architecture changed
- `base-sleuth/references/strategy_evolution.md` if thresholds/weights changed
- `base-sleuth/references/clanker_intel.md` if new API behavior was discovered

## User's Request

{{{ input }}}
