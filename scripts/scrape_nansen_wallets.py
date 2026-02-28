#!/usr/bin/env python3
"""Scrape all messages from @NansenBot in Telegram and extract Base chain wallets.

Usage:
    1. Get your API credentials from https://my.telegram.org
    2. Run:
        python scripts/scrape_nansen_wallets.py \
            --api-id 38063415 \
            --api-hash b88a819a08869b9dda713bbe37207121

    3. First run will ask you to log in (phone number + code).
       A session file is saved so subsequent runs are instant.

    4. Output: data/nansen_wallets.md

Requirements:
    pip install telethon
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

NANSEN_BOT_ID = 1593218799  # @NansenBot real entity ID

# ── Regex patterns for parsing message text ──────────────────

# Wallet label from markdown: 🟢 [Label](url) or 🟢 Label
RE_LABEL = re.compile(r"🟢\s+\[?([^\]\(]+?)[\]\(]", re.UNICODE)
# Fallback label: 🟢 text bought
RE_LABEL_FALLBACK = re.compile(r"🟢\s+(.+?)\s+bought", re.UNICODE)

# Token symbol: bought 15.3M #JUNO   or   bought 294M #GITC
RE_TOKEN_SYMBOL = re.compile(r"bought\s+[\d,.]+[KMBkmb]?\s+#(\w+)", re.IGNORECASE)

# USD value: ($244.17)  or  ($1,406.42)
RE_USD_VALUE = re.compile(r"\(\$([\d,]+\.?\d*)\)")

# ETH value: 0.12 #ETH  or  for 0.2 #ETH
RE_ETH_VALUE = re.compile(r"([\d.]+)\s*#ETH", re.IGNORECASE)

# Token amount: bought 15.3M  or  bought 1.75B  or  bought 294M
RE_TOKEN_AMOUNT = re.compile(r"bought\s+([\d,.]+)([KMBkmb])?\s+#")

# Chain tag: #Base #Ethereum etc.
RE_CHAIN = re.compile(r"#(Base|Ethereum|Polygon|Arbitrum|Optimism|Solana)", re.IGNORECASE)

# Address from URL query param
RE_URL_ADDRESS = re.compile(r"address=(0x[0-9a-fA-F]{40})", re.IGNORECASE)
RE_URL_TOKEN = re.compile(r"tokenAddress=(0x[0-9a-fA-F]{40})", re.IGNORECASE)
RE_URL_TX = re.compile(r"/tx/(0x[0-9a-fA-F]{64})", re.IGNORECASE)
RE_URL_CHAIN = re.compile(r"chain=(\w+)", re.IGNORECASE)


def extract_urls_from_entities(msg) -> list[str]:
    """Extract all URLs from Telegram message entities."""
    urls: list[str] = []
    if msg.entities:
        for ent in msg.entities:
            if hasattr(ent, "url") and ent.url:
                urls.append(ent.url)
    return urls


def parse_message(text: str, urls: list[str]) -> dict | None:
    """Parse a NansenBot message using text + entity URLs.

    Returns dict with parsed fields, or None if not valid.
    """
    if not text or "Smart Alert" not in text:
        return None

    # ── Extract from entity URLs ────────────────────────
    wallet_address = ""
    token_address = ""
    tx_hash = ""
    chain_from_url = ""

    for url in urls:
        # Wallet from profiler URL
        if "profiler" in url and not wallet_address:
            m = RE_URL_ADDRESS.search(url)
            if m:
                wallet_address = m.group(1).lower()
            cm = RE_URL_CHAIN.search(url)
            if cm:
                chain_from_url = cm.group(1)

        # Token from token-god-mode URL
        if "token-god-mode" in url and not token_address:
            m = RE_URL_TOKEN.search(url)
            if m:
                token_address = m.group(1).lower()

        # Tx hash from tx URL
        if "/tx/" in url and not tx_hash:
            m = RE_URL_TX.search(url)
            if m:
                tx_hash = m.group(1).lower()

    # ── Extract from text ───────────────────────────────

    # Chain from hashtag (fallback)
    chain_match = RE_CHAIN.search(text)
    chain = chain_from_url or (chain_match.group(1) if chain_match else "")

    # Wallet label
    label_match = RE_LABEL.search(text)
    if not label_match:
        label_match = RE_LABEL_FALLBACK.search(text)
    wallet_label = label_match.group(1).strip() if label_match else ""
    # Clean emoji prefix from label
    wallet_label = wallet_label.lstrip("👤🟢 ").strip()

    # Token symbol
    sym_match = RE_TOKEN_SYMBOL.search(text)
    token_symbol = sym_match.group(1) if sym_match else ""

    # USD value
    usd_match = RE_USD_VALUE.search(text)
    usd_value = 0.0
    if usd_match:
        try:
            usd_value = float(usd_match.group(1).replace(",", ""))
        except ValueError:
            pass

    # ETH value
    eth_match = RE_ETH_VALUE.search(text)
    eth_value = 0.0
    if eth_match:
        try:
            eth_value = float(eth_match.group(1))
        except ValueError:
            pass

    # Token amount
    token_amount = 0.0
    amount_match = RE_TOKEN_AMOUNT.search(text)
    if amount_match:
        try:
            raw = float(amount_match.group(1).replace(",", ""))
            multiplier = {"k": 1e3, "m": 1e6, "b": 1e9}.get(
                (amount_match.group(2) or "").lower(), 1,
            )
            token_amount = raw * multiplier
        except ValueError:
            pass

    # Alert type (from header line)
    alert_type = ""
    if "Smart Money Large Token" in text:
        alert_type = "Smart Money Large Buy"
    elif "Memecoin Whale" in text:
        alert_type = "Memecoin Whale Buy"
    elif "Smart Money" in text:
        alert_type = "Smart Money"
    else:
        alert_type = "Smart Alert"

    if not wallet_address:
        return None

    return {
        "wallet_address": wallet_address,
        "wallet_label": wallet_label,
        "token_address": token_address,
        "token_symbol": token_symbol,
        "usd_value": usd_value,
        "eth_value": eth_value,
        "token_amount": token_amount,
        "tx_hash": tx_hash,
        "chain": chain,
        "alert_type": alert_type,
    }


async def scrape_nansen_messages(
    api_id: int,
    api_hash: str,
    session_name: str = "nansen_scraper",
    base_only: bool = False,
) -> list[dict]:
    """Connect to Telegram and fetch all messages from @NansenBot."""
    try:
        from telethon import TelegramClient
    except ImportError:
        print("ERROR: telethon not installed. Run: pip install telethon")
        sys.exit(1)

    session_path = str(Path(__file__).parent.parent / session_name)
    client = TelegramClient(session_path, api_id, api_hash)

    print("Connecting to Telegram...")
    await client.start()
    me = await client.get_me()
    print(f"Logged in as: {me.first_name}")

    # Get the real NansenBot by ID
    try:
        entity = await client.get_entity(NANSEN_BOT_ID)
        print(f"Found @{entity.username} (ID: {entity.id})")
    except Exception as e:
        print(f"ERROR: Cannot find NansenBot (ID {NANSEN_BOT_ID}): {e}")
        print("Make sure you have a chat with @NansenBot in your Telegram.")
        await client.disconnect()
        sys.exit(1)

    print(f"Fetching messages from @NansenBot...")
    signals: list[dict] = []
    msg_count = 0

    async for message in client.iter_messages(entity, limit=None):
        msg_count += 1
        if not message.text:
            continue

        # Extract URLs from message entities
        urls = extract_urls_from_entities(message)

        parsed = parse_message(message.text, urls)
        if not parsed:
            continue

        # Add message metadata
        parsed["message_id"] = message.id
        parsed["date"] = message.date.strftime("%Y-%m-%d %H:%M")

        if base_only and parsed["chain"].lower() != "base":
            continue

        signals.append(parsed)

        if msg_count % 100 == 0:
            print(f"  Processed {msg_count} messages, found {len(signals)} signals...")

    print(f"\nDone! Processed {msg_count} total messages.")
    print(f"Found {len(signals)} valid smart alerts.")

    await client.disconnect()
    return signals


def deduplicate_wallets(signals: list[dict]) -> list[dict]:
    """Deduplicate by wallet address, keeping stats."""
    wallet_map: dict[str, dict] = {}
    for sig in signals:
        addr = sig["wallet_address"]
        if addr not in wallet_map:
            wallet_map[addr] = {
                "address": addr,
                "label": sig["wallet_label"],
                "chain": sig["chain"],
                "first_seen": sig["date"],
                "last_seen": sig["date"],
                "buy_count": 1,
                "tokens": [sig["token_symbol"]] if sig["token_symbol"] else [],
                "total_usd": sig["usd_value"],
                "alert_types": {sig["alert_type"]} if sig["alert_type"] else set(),
            }
        else:
            entry = wallet_map[addr]
            entry["last_seen"] = sig["date"]
            entry["buy_count"] += 1
            entry["total_usd"] += sig["usd_value"]
            if sig["token_symbol"] and sig["token_symbol"] not in entry["tokens"]:
                entry["tokens"].append(sig["token_symbol"])
            if not entry["label"] and sig["wallet_label"]:
                entry["label"] = sig["wallet_label"]
            if sig["alert_type"]:
                entry["alert_types"].add(sig["alert_type"])

    # Convert sets to strings for output
    for w in wallet_map.values():
        w["alert_types"] = ", ".join(sorted(w["alert_types"]))

    return sorted(wallet_map.values(), key=lambda w: w["buy_count"], reverse=True)


def write_markdown(wallets: list[dict], signals: list[dict], output_path: Path, base_only: bool) -> None:
    """Write the wallet list to a markdown file."""
    chain_filter = "Base chain only" if base_only else "All chains"
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    lines: list[str] = []
    lines.append("# Nansen Smart Money Wallets — Extracted from @NansenBot")
    lines.append("")
    lines.append(f"> Auto-extracted on {now}")
    lines.append(f"> Source: @NansenBot (ID: {NANSEN_BOT_ID}) Telegram smart alerts")
    lines.append(f"> Filter: {chain_filter}")
    lines.append(f"> Total signals parsed: {len(signals)}")
    lines.append(f"> Unique wallets: {len(wallets)}")
    lines.append("")

    # ── Summary ──
    lines.append("## Summary")
    lines.append("")
    total_usd = sum(w["total_usd"] for w in wallets)
    lines.append(f"- **Unique wallets**: {len(wallets)}")
    lines.append(f"- **Total buy signals**: {len(signals)}")
    lines.append(f"- **Total USD volume**: ${total_usd:,.2f}")
    if signals:
        lines.append(f"- **Date range**: {signals[-1]['date']} → {signals[0]['date']}")

    # Chain breakdown
    chain_counts: dict[str, int] = {}
    for sig in signals:
        c = sig["chain"] or "Unknown"
        chain_counts[c] = chain_counts.get(c, 0) + 1
    if chain_counts:
        lines.append(f"- **Chains**: {', '.join(f'{c} ({n})' for c, n in sorted(chain_counts.items(), key=lambda x: -x[1]))}")
    lines.append("")

    # ── Wallet table ──
    lines.append("## Wallet List")
    lines.append("")
    lines.append("| # | Address | Nansen Label | Buys | Tokens | Total USD | First Seen | Last Seen |")
    lines.append("|---|---------|-------------|------|--------|-----------|------------|-----------|")

    for i, w in enumerate(wallets, 1):
        addr = w["address"]
        label = w["label"] or "Unknown"
        tokens_str = ", ".join(w["tokens"][:5])
        if len(w["tokens"]) > 5:
            tokens_str += f" +{len(w['tokens']) - 5} more"
        usd_str = f"${w['total_usd']:,.0f}" if w["total_usd"] > 0 else "-"
        lines.append(
            f"| {i} | `{addr}` | {label} | {w['buy_count']} | {tokens_str} | {usd_str} | {w['first_seen']} | {w['last_seen']} |"
        )

    lines.append("")
    lines.append("---")
    lines.append("")

    # ── Plain address list (copy-paste ready) ──
    lines.append("## Plain Address List")
    lines.append("")
    lines.append("```")
    for w in wallets:
        label = w["label"] or "nansen"
        lines.append(f"{w['address']} {label}")
    lines.append("```")
    lines.append("")

    # ── Recent signals (last 50) ──
    lines.append("## Recent Signals (last 50)")
    lines.append("")
    lines.append("| Date | Wallet | Label | Token | USD | ETH | Chain | Type |")
    lines.append("|------|--------|-------|-------|-----|-----|-------|------|")
    for sig in signals[:50]:
        addr_short = sig["wallet_address"][:8] + "…" + sig["wallet_address"][-4:]
        label = sig["wallet_label"] or "-"
        token = f"#{sig['token_symbol']}" if sig["token_symbol"] else "-"
        usd = f"${sig['usd_value']:,.0f}" if sig["usd_value"] > 0 else "-"
        eth = f"{sig['eth_value']:.2f}" if sig["eth_value"] > 0 else "-"
        chain = sig["chain"] or "-"
        atype = sig["alert_type"] or "-"
        lines.append(f"| {sig['date']} | `{addr_short}` | {label} | {token} | {usd} | {eth} | {chain} | {atype} |")

    lines.append("")
    lines.append("---")
    lines.append(f"*Generated by `scripts/scrape_nansen_wallets.py` on {now}*")

    output_path.write_text("\n".join(lines))
    print(f"\nWritten to: {output_path}")
    print(f"  {len(wallets)} unique wallets")
    print(f"  {len(signals)} buy signals")


def update_wallet_file(wallets: list[dict], wallet_file: Path) -> int:
    """Append new wallets to data/smart_money_wallets.txt. Returns count added."""
    if not wallet_file.exists():
        print(f"  Warning: {wallet_file} not found, skipping.")
        return 0

    existing = wallet_file.read_text()
    added = 0
    with open(wallet_file, "a") as f:
        for w in wallets:
            if w["address"] not in existing:
                label = w["label"] or "nansen"
                f.write(f"{w['address']} {label}\n")
                added += 1
    return added


async def main() -> None:
    parser = argparse.ArgumentParser(
        description="Scrape @NansenBot messages and extract smart money wallets",
    )
    parser.add_argument("--api-id", type=int, required=True, help="Telegram API ID")
    parser.add_argument("--api-hash", type=str, required=True, help="Telegram API hash")
    parser.add_argument("--session", default="nansen_scraper", help="Session name (default: nansen_scraper)")
    parser.add_argument("--output", default="data/nansen_wallets.md", help="Output markdown file")
    parser.add_argument("--base-only", action="store_true", help="Only extract Base chain wallets")
    parser.add_argument("--update-wallets", action="store_true", help="Also add to data/smart_money_wallets.txt")
    args = parser.parse_args()

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    signals = await scrape_nansen_messages(
        api_id=args.api_id,
        api_hash=args.api_hash,
        session_name=args.session,
        base_only=args.base_only,
    )

    if not signals:
        print("No valid signals found. Make sure you have messages from @NansenBot.")
        return

    wallets = deduplicate_wallets(signals)
    write_markdown(wallets, signals, output_path, args.base_only)

    if args.update_wallets:
        wallet_txt = Path("data/smart_money_wallets.txt")
        added = update_wallet_file(wallets, wallet_txt)
        if added:
            print(f"  Added {added} new wallets to {wallet_txt}")
        else:
            print(f"  No new wallets to add to {wallet_txt}")

    print(f"\n✅ Done! {len(wallets)} unique wallets from {len(signals)} signals.")


if __name__ == "__main__":
    asyncio.run(main())
