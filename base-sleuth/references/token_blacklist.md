# Base Sleuth — Token Blacklist & Avoid Patterns

**AGENT INSTRUCTION**: Add tokens and patterns here that have proven to be rugs, scams, or consistently dead. Reference this before every trade.

---

## Blacklisted Contract Addresses

Tokens confirmed as rugs, scams, or honeypots. NEVER trade these or forks.

| Contract | Name | Reason | Date Added |
|----------|------|--------|------------|
| [ADD AS DISCOVERED] | | | |

---

## Blacklisted Deployer Wallets

Deployers known to create rugs on Base. Any token from these = auto-skip.

| Deployer Wallet | Known Rugs | Platform | Date Added |
|-----------------|------------|----------|------------|
| [ADD AS DISCOVERED] | | | |

---

## Blacklisted Clanker Requestor Addresses

Requestor addresses (msg_sender) that repeatedly launch dead tokens.

| Requestor Wallet | # Launches | # Dead | Notes | Date Added |
|------------------|------------|--------|-------|------------|
| [ADD AS DISCOVERED] | | | | |

---

## Pattern Blacklist — Base / Clanker Specific

### Naming Patterns to Avoid
- Exact copies of trending token names (copycat rugs)
- Tokens with "SAFE", "MOON", "1000x" in name
- Names that are just wallet address fragments
- Names less than 2 characters
- [ADD AS DISCOVERED]

### Bankr Bot Patterns to Avoid (if skip_bankr=false)
- Bankr launch with no social links AND no champagne tag
- Bankr launch with description that's just the bot template
- Bankr launch where devBuy > 50%
- [ADD AS DISCOVERED]

### Technical Patterns to Avoid
- `sniperTax > 0` in Clanker extensions — token taxes transactions
- `devBuy` percentage > 50% — dev bought massive share at launch
- No DexScreener data after 10+ minutes — dead on arrival
- Liquidity drops > 50% within 1 hour of launch
- Single wallet holds > 40% of supply
- [ADD AS DISCOVERED]

### Social Patterns to Avoid
- Twitter/X account created same day as token launch
- Telegram group with only bots, no real conversation
- Social links that 404 or redirect to unrelated pages
- Description is identical to another recent token
- [ADD AS DISCOVERED]

### DexScreener Patterns to Avoid
- Sell count > 3x buy count in first hour
- Liquidity < $500 after 5 minutes
- Only 1-2 holders with massive concentration
- Volume is 100% from a single wallet
- [ADD AS DISCOVERED]

---

## Scam Keywords List

Current keywords that trigger instant Stage 1 rejection:

```
rug, scam, honeypot, honey pot, ponzi, fake, drain
```

**Add new keywords as discovered:**
```
[ADD HERE — e.g., "guaranteed profit", "free airdrop", etc.]
```

---

## Known Rug Deployers on Base

Track Base-chain deployer wallets that create and abandon tokens.

| Deployer | # Rugs | Method | First Seen | Notes |
|----------|--------|--------|------------|-------|
| [ADD AS DISCOVERED] | | | | |

---

## Post-Rug Analysis

When a trade results in a rug or total loss, document it here to learn.

<!--
### [TOKEN_NAME] ($SYMBOL) — Rugged YYYY-MM-DD

**Contract**: 0x...
**Entry**: $XX at price $X.XXXX
**Loss**: -XX%
**Launch Platform**: bankr / clawnch / direct
**Was Champagne**: Yes/No
**Pipeline Score**: X.XX

**What happened**: [Description — how did it rug?]

**Red flags I missed**:
1. [Flag 1]
2. [Flag 2]

**What I'll check next time**:
1. [New check]
2. [New check]

**Pattern to add to blacklist**: [If applicable]
**Deployer to blacklist**: [If applicable]
-->
