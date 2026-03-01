---
description: "Debug or fix an issue — loads codebase context and common pitfalls"
mode: agent
tools: ["changes", "codebase", "problems", "usages", "fetch"]
---

# Task: Debug / Fix an Issue

You are debugging Base Sleuth, a Python 3.12 async application running 24/7 in Docker.

## Context — Read First

- [clanker_intel.md](../base-sleuth/references/clanker_intel.md) — API quirks that often cause bugs
- [config.py](../../clanker_tracker/config.py) — All configuration with defaults
- [main.py](../../clanker_tracker/main.py) — 9-loop async orchestrator
- [notifier.py](../../clanker_tracker/notifier.py) — Telegram alert sender (4 alert types)
- [docker-compose.yml](../../docker-compose.yml) — Docker deployment + volume mounts

## Common Gotchas (Check These First)

### API & Data
1. **Clanker pagination**: `page` param is IGNORED — only cursor-based works. Cursor = base64 JSON.
2. **Always 10 per page**: `pageSize` param ignored. Always returns exactly 10 tokens.
3. **socialLinks format**: Array of `{name, link}` objects, NOT flat URL strings.
4. **pool_address length**: Can be 66 chars (bytes32) in clanker_v4, not just 42.
5. **Bankr detection**: Via description text, NOT deployer address.
6. **DexScreener batch limit**: Max 30 addresses per request.
7. **DexScreener indexing delay**: New tokens need 2-5 min before they appear.

### Code Patterns
8. **All code is async**: Every DB call, HTTP call, and loop must use `async/await`.
9. **structlog not print**: Never use `print()` — always `logger.info/warning/error/exception`.
10. **Pydantic None→list**: YAML `null` for list fields needs a `field_validator` to coerce to `[]`.
11. **httpx client lifecycle**: Long-lived `AsyncClient` instances may stale in Docker — prefer `async with httpx.AsyncClient() as c:` per request for critical paths.

### Deployment
12. **`docker compose restart` ≠ rebuild**: Volume-mounted source (`./clanker_tracker:/app/clanker_tracker:ro`) is picked up on restart, but `requirements.txt` and `data/` changes need `docker compose up -d --build bot`.
13. **`logger.debug()` is invisible**: Default log level is INFO. Use `logger.warning()` for diagnostics you need to see in production. Silent `except: pass` with debug logging = hours of confusion.
14. **Always check logs after deploy**: `docker compose logs bot --tail 20` — never assume restart = working.

## Debugging Steps

1. Read the error message or symptom carefully
2. Check if it matches a common gotcha above
3. Read the relevant source file(s) — check the full function, not just the error line
4. Check `get_errors` for type/lint issues
5. Reproduce locally if possible: `python -c "from clanker_tracker.main import Tracker"`
6. Fix with explanation, write a test to prevent regression
7. Verify the fix doesn't break imports

## VPS Access
```bash
ssh -i ~/.ssh/lightnode_vps root@38.54.15.53
cd /opt/base-sleuth
docker compose logs bot --tail 50         # recent logs
docker compose logs bot -f | grep error   # live error stream
docker compose exec bot python -c "..."   # test inside container
```

## The Issue

{{{ input }}}
