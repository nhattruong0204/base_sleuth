---
description: "Debug or fix an issue — loads full codebase context"
mode: agent
tools: ["changes", "codebase", "problems", "usages", "fetch"]
---

# Task: Debug / Fix an Issue

You are debugging Base Sleuth, a Python async application.

## Context — Read First

- [clanker_intel.md](../base-sleuth/references/clanker_intel.md) — API quirks that often cause bugs
- [config.py](../clanker_tracker/config.py) — All configuration with defaults

## Common Gotchas

1. **Clanker API pagination**: `page` is IGNORED. Only cursor-based pagination works. Cursor is base64-encoded JSON.
2. **Always 10 per page**: `pageSize` param is ignored. You always get exactly 10 tokens.
3. **socialLinks format**: Array of `{name, link}` objects, NOT flat URL strings.
4. **pool_address length**: Can be 66 chars (bytes32) in clanker_v4, not just 42.
5. **Bankr detection**: Via description text, NOT deployer address.
6. **DexScreener batch limit**: Max 30 addresses per request.
7. **DexScreener indexing delay**: New tokens need 2-5 min before they appear.
8. **Pydantic None→list**: YAML `null` for list fields needs a `field_validator` to coerce to `[]`.
9. **All code is async**: Every DB call, HTTP call, and loop must use `async/await`.
10. **structlog not print**: Never use `print()` — always `logger.info/debug/error/exception`.

## Debugging Steps

1. Read the error message carefully
2. Check if it's one of the common gotchas above
3. Read the relevant source file(s)
4. Check `get_errors` for any type/lint issues
5. Propose a fix with explanation
6. Verify the fix doesn't break imports

## The Issue

{{{ input }}}
