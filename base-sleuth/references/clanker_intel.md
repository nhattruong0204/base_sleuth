# Base Sleuth — Clanker Ecosystem Intelligence

Live intelligence about the Clanker token factory, Base chain dynamics, and API behavior. This document captures operational knowledge that doesn't fit in strategy or blacklist files.

**AGENT INSTRUCTION**: Update this document whenever you discover new API behavior, ecosystem changes, or operational insights.

---

## Clanker API — Operational Notes

### Pagination (Critical)
- **Cursor-based ONLY** — the `page` parameter is completely ignored
- Cursor is base64-encoded JSON: `{"id":"2026-02-07T03:50:49"}`
- Each page returns exactly **10 tokens** regardless of `pageSize` parameter
- To jump to a date: craft a cursor with the target timestamp and base64-encode it
- `sort=desc` returns newest first (default behavior)

### Rate Limits
- No official rate limit documented
- Observed: can make ~60 requests/minute without issues
- No API key required for read endpoints
- API key needed for: deploy, get-by-address (authenticated)

### Response Shape
```json
{
  "data": [{ token objects }],
  "total": 431558,
  "cursor": "eyJpZCI6IjIwMjYtMDItMDdUMDM6NTA6NDkifQ==",
  "tokensDeployed": 431558
}
```

### Token Object — Key Fields
```json
{
  "id": 866694,
  "name": "TokenName",
  "symbol": "TKN",
  "contract_address": "0x...",
  "pool_address": "0x... (66 chars, bytes32 in v4)",
  "description": "Created by bankrbot | ...",
  "requestor_address": "0x...",
  "msg_sender": "0x...",
  "socialLinks": [{"name": "x", "link": "https://x.com/..."}],
  "metadata": {"socialMediaUrls": ["https://..."]},
  "tags": {"champagne": true, "verified": true},
  "extensions": {"devBuy": "0.5", "fees": "0", "sniperTax": "0"},
  "type": "clanker_v4",
  "factory_address": "0x..."
}
```

### Champagne Endpoint
- `GET /api/tokens?champagne=true` — returns only curated tokens
- As of 2026-02-07: only **96 tokens** have champagne tag
- These are paginated (10 per page, cursor-based)
- Mix of platforms: ~58% direct, ~39% farcaster/clawnch, ~3% bankr

---

## DexScreener API — Operational Notes

### Batch Lookup
- `GET /latest/dex/tokens/{addr1},{addr2},...` — up to 30 addresses
- Returns `{ pairs: [...] }` — may have multiple pairs per token
- Select best pair: highest liquidity
- If `pairs` is null/empty → token has no DEX presence (dead)

### Indexing Delay
- New Base tokens take **2–5 minutes** to appear on DexScreener
- Our recheck delay: 300s (5 min) — conservative but reliable
- Some tokens NEVER appear (no pool, no trades, truly dead)

### Key Metrics Location
```
pairs[0].liquidity.usd      — pool liquidity
pairs[0].volume.h24          — 24h volume
pairs[0].txns.h1.buys        — buy transactions last hour
pairs[0].txns.h1.sells       — sell transactions last hour
pairs[0].priceChange.h1      — 1h price change %
pairs[0].priceChange.h24     — 24h price change %
pairs[0].marketCap            — current market cap
pairs[0].fdv                  — fully diluted valuation
```

### Other Useful Endpoints
- `/token-boosts/top/v1` — tokens paying for DexScreener boost (marketing signal)
- `/token-profiles/latest/v1` — tokens with custom profiles (effort signal)

---

## Base Chain — Ecosystem Notes

### Token Volume Trends (as of 2026-02-07)
| Period | Tokens Deployed | Per Day |
|--------|----------------|---------|
| Last 1 day | ~38,000 | 38,000 |
| Last 7 days | ~167,000 | ~23,800 |
| Last 14 days | ~207,000 | ~14,800 |
| Last 28 days | ~225,000 | ~8,000 |
| All time | 431,558 | — |

**Observation**: Launch volume is ACCELERATING. 38K/day now vs 8K/day a month ago.

### Platform Distribution (Live Snapshot)
- Bankr: ~92% — Telegram bot, mass-deployed, mostly dead
- Clawnch (4Claw): ~6% — slightly higher quality, some social links
- Direct: ~1.5% — organic, highest potential
- Farcaster: ~0.5% — community-driven, decent quality
- Other/Unknown: <0.5%

---

## Discoveries & Anomalies

**Log unexpected findings here:**

### 2026-02-07 — Initial Analysis
- `pool_address` changed from 42-char (standard address) to 66-char (bytes32) in clanker_v4
- `socialLinks` format changed from flat URL array to `[{name, link}]` objects
- Some tokens have both `socialLinks` and `metadata.socialMediaUrls` — prefer `socialLinks`
- `requestor_address` can be null — fall back to `msg_sender`
- Champagne tokens are NOT just quality filter — some are Bankr launches (~3%)

---

## API Change Log

Track any API changes discovered during operation:

| Date | Change | Impact | Action Taken |
|------|--------|--------|-------------|
| 2026-02-07 | pool_address now 66 chars | DB column expanded to String(66) | Updated models.py |
| 2026-02-07 | socialLinks format changed | Context resolver updated | Updated context_resolver.py |
| [ADD AS DISCOVERED] | | | |
