"""Live gem scanner — run once to find the best tokens from last N hours."""

import asyncio
import httpx
from datetime import datetime, timezone, timedelta

SCAM_KEYWORDS = ["rug", "scam", "honeypot", "honey pot", "ponzi", "fake", "drain"]
BANKR_KEYWORDS = ["bankrbot", "bankr bot", "bankr terminal", "bankr.bot"]


def detect_platform(desc: str) -> str:
    d = (desc or "").lower()
    if any(k in d for k in BANKR_KEYWORDS):
        return "bankr"
    if "clawnch" in d or "4claw" in d:
        return "clawnch"
    if "farcaster" in d or "warpcast" in d:
        return "farcaster"
    if desc and desc.strip():
        return "direct"
    return "unknown"


def has_scam(name: str, symbol: str) -> bool:
    combined = ((name or "") + " " + (symbol or "")).lower()
    return any(k in combined for k in SCAM_KEYWORDS)


async def scan(hours: int = 2):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
    now_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    print(f"=== BASE SLEUTH LIVE SCAN — {now_str} ===")
    print(f"Looking for tokens launched after {cutoff.strftime('%H:%M UTC')} (last {hours}h)")
    print()

    async with httpx.AsyncClient(timeout=25) as http:
        # ── Step 1: Champagne tokens ──
        print("🍾 STEP 1: Fetching champagne tokens...")
        r = await http.get(
            "https://www.clanker.world/api/tokens",
            params={"champagne": "true", "sort": "desc"},
        )
        champ_all = r.json().get("data", [])
        champ_recent = []
        for t in champ_all:
            created = t.get("created_at") or t.get("createdAt") or ""
            if created:
                try:
                    ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                    if ts >= cutoff:
                        champ_recent.append(t)
                except Exception:
                    pass
        print(f"   Found {len(champ_recent)} champagne tokens in last {hours}h (out of {len(champ_all)} on page)")

        # ── Step 2: Latest non-Bankr tokens ──
        print()
        print("🔍 STEP 2: Fetching latest firehose tokens (filtering Bankr)...")
        candidates = list(champ_recent)
        seen_addrs = {t.get("contract_address", "").lower() for t in candidates}

        cursor = None
        pages_fetched = 0
        non_bankr_count = 0
        total_fetched = 0
        bankr_skipped = 0
        stop_paging = False

        while pages_fetched < 20 and non_bankr_count < 40 and not stop_paging:
            params = {"sort": "desc"}
            if cursor:
                params["cursor"] = cursor
            r2 = await http.get("https://www.clanker.world/api/tokens", params=params)
            page_data = r2.json()
            tokens = page_data.get("data", [])
            cursor = page_data.get("cursor")
            pages_fetched += 1
            total_fetched += len(tokens)

            if not tokens:
                break

            for t in tokens:
                created = t.get("created_at") or t.get("createdAt") or ""
                if created:
                    try:
                        ts = datetime.fromisoformat(created.replace("Z", "+00:00"))
                        if ts < cutoff:
                            stop_paging = True
                            break
                    except Exception:
                        pass

                addr = (t.get("contract_address") or "").lower()
                if addr in seen_addrs:
                    continue
                seen_addrs.add(addr)

                desc = t.get("description", "")
                platform = detect_platform(desc)
                name = t.get("name", "")
                symbol = t.get("symbol", "")

                if platform == "bankr":
                    bankr_skipped += 1
                    continue
                if has_scam(name, symbol):
                    continue
                if len(name or "") < 2:
                    continue

                non_bankr_count += 1
                t["_platform"] = platform
                t["_is_champagne"] = False
                candidates.append(t)

        for t in champ_recent:
            t["_platform"] = detect_platform(t.get("description", ""))
            t["_is_champagne"] = True

        print(f"   Scanned {total_fetched} tokens across {min(pages_fetched, 20)} pages")
        print(f"   {non_bankr_count} non-Bankr candidates + {len(champ_recent)} champagne")
        print(f"   {bankr_skipped} Bankr tokens skipped")
        print(f"   Total candidates for DexScreener: {len(candidates)}")

        # ── Step 3: Batch DexScreener lookup ──
        print()
        print("📊 STEP 3: Batch DexScreener lookup...")
        addr_to_token = {}
        for t in candidates:
            addr = t.get("contract_address", "")
            if addr:
                addr_to_token[addr] = t

        addrs = list(addr_to_token.keys())
        dex_results = {}
        for i in range(0, len(addrs), 30):
            batch = addrs[i : i + 30]
            batch_str = ",".join(batch)
            try:
                dr = await http.get(
                    f"https://api.dexscreener.com/latest/dex/tokens/{batch_str}"
                )
                pairs = dr.json().get("pairs") or []
                for p in pairs:
                    base_addr = p.get("baseToken", {}).get("address", "").lower()
                    existing_liq = (
                        dex_results.get(base_addr, {})
                        .get("liquidity", {})
                        .get("usd", 0)
                        or 0
                    )
                    new_liq = (p.get("liquidity") or {}).get("usd", 0) or 0
                    if base_addr not in dex_results or new_liq > existing_liq:
                        dex_results[base_addr] = p
            except Exception as e:
                print(f"   DexScreener batch error: {e}")
            await asyncio.sleep(0.3)

        print(f"   {len(dex_results)} tokens have DEX data (out of {len(addrs)} checked)")

        # ── Step 4: Score each candidate ──
        print()
        print("🏆 STEP 4: Scoring candidates...")
        scored = []
        for addr, t in addr_to_token.items():
            pair = dex_results.get(addr.lower())
            if not pair:
                continue

            liq = (pair.get("liquidity") or {}).get("usd") or 0
            vol24 = (pair.get("volume") or {}).get("h24") or 0
            mcap = pair.get("marketCap") or pair.get("fdv") or 0
            buys_1h = (pair.get("txns") or {}).get("h1", {}).get("buys", 0)
            sells_1h = (pair.get("txns") or {}).get("h1", {}).get("sells", 0)
            buys_24h = (pair.get("txns") or {}).get("h24", {}).get("buys", 0)
            sells_24h = (pair.get("txns") or {}).get("h24", {}).get("sells", 0)
            price_1h = (pair.get("priceChange") or {}).get("h1") or 0
            price_24h = (pair.get("priceChange") or {}).get("h24") or 0

            is_champ = t.get("_is_champagne", False)
            platform = t.get("_platform", "unknown")
            social_links = t.get("socialLinks") or []
            tags = t.get("tags") or {}
            verified = bool(tags.get("verified"))

            # S2: DEX Metrics (0–1, weight 0.30)
            s2 = 0.0
            if liq >= 500:
                s2 += min(liq / 50000, 1.0) * 0.4
            if mcap >= 1000:
                s2 += 0.2
            if vol24 >= 50:
                s2 += min(vol24 / 10000, 1.0) * 0.2
            total_txns = buys_1h + sells_1h
            if total_txns > 0:
                ratio = buys_1h / total_txns
                if ratio >= 0.2:
                    s2 += ratio * 0.2

            # S3: Momentum (0–1, weight 0.20)
            s3 = 0.0
            if buys_1h >= 5:
                s3 += min(buys_1h / 50, 1.0) * 0.4
            if isinstance(price_24h, (int, float)) and price_24h > 50:
                s3 += min(price_24h / 500, 1.0) * 0.3
            if liq > 0 and vol24 / max(liq, 1) > 0.5:
                s3 += min((vol24 / max(liq, 1)) / 5, 1.0) * 0.3

            # S4: Smart Money (0–1, weight 0.20) — no wallets tracked yet
            s4 = 0.0

            # S5: Context Quality (0–1, weight 0.15)
            s5 = 0.0
            if social_links:
                for sl in social_links:
                    ln = (sl.get("name") or "").lower() if isinstance(sl, dict) else ""
                    if "x" in ln or "twitter" in ln:
                        s5 += 0.3
                    elif "farcaster" in ln or "warpcast" in ln:
                        s5 += 0.3
                    elif ln:
                        s5 += 0.15
            desc = t.get("description") or ""
            if len(desc) > 20:
                s5 += 0.1
            s5 = min(s5, 1.0)

            # Weighted final score
            score = s2 * 0.30 + s3 * 0.20 + s4 * 0.20 + s5 * 0.15
            if is_champ:
                score += 0.15

            scored.append(
                {
                    "name": t.get("name", "?"),
                    "symbol": t.get("symbol", "?"),
                    "addr": addr,
                    "score": score,
                    "liq": liq,
                    "vol24": vol24,
                    "mcap": mcap,
                    "buys_1h": buys_1h,
                    "sells_1h": sells_1h,
                    "buys_24h": buys_24h,
                    "sells_24h": sells_24h,
                    "price_1h": price_1h,
                    "price_24h": price_24h,
                    "is_champ": is_champ,
                    "verified": verified,
                    "platform": platform,
                    "social_links": social_links,
                    "desc": desc[:80] if desc else "",
                    "s2": s2,
                    "s3": s3,
                    "s4": s4,
                    "s5": s5,
                }
            )

        scored.sort(key=lambda x: x["score"], reverse=True)

        # ── Step 5: Report ──
        print()
        print("=" * 70)
        print(
            f"  BASE SLEUTH SCAN RESULTS — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}"
        )
        print(
            f"  Scanned: {total_fetched} tokens | Candidates: {len(candidates)} | With DEX: {len(dex_results)}"
        )
        print("=" * 70)

        gems = [s for s in scored if s["score"] >= 0.45]
        watchlist = [s for s in scored if 0.25 <= s["score"] < 0.45]

        if gems:
            print(f"\n🔥 GEMS (score ≥ 0.45) — {len(gems)} found:\n")
            for g in gems:
                champ_badge = "🍾 Champagne" if g["is_champ"] else ""
                ver_badge = "✅ Verified" if g["verified"] else ""
                plat_badge = f"🔗 {g['platform'].title()}"
                badges = "  ".join(filter(None, [champ_badge, ver_badge, plat_badge]))
                print(f"  🚀 {g['name']} (${g['symbol']}) — Score: {g['score']:.0%}")
                print(f"     {badges}")
                print(
                    f"     💰 Liq: ${g['liq']:,.0f}  |  Vol 24h: ${g['vol24']:,.0f}  |  MCap: ${g['mcap']:,.0f}"
                )
                print(
                    f"     📈 Buys 1h: {g['buys_1h']}  Sells 1h: {g['sells_1h']}  |  Price 1h: {g['price_1h']}%  24h: {g['price_24h']}%"
                )
                print(
                    f"     📈 Buys 24h: {g['buys_24h']}  Sells 24h: {g['sells_24h']}"
                )
                print(
                    f"     🧮 S2={g['s2']:.2f} S3={g['s3']:.2f} S4={g['s4']:.2f} S5={g['s5']:.2f}"
                )
                if g["desc"]:
                    print(f"     📝 {g['desc']}")
                print(
                    f"     📊 https://dexscreener.com/base/{g['addr']}"
                )
                print(
                    f"     🔍 https://www.clanker.world/clanker/{g['addr']}"
                )
                print()
        else:
            print("\n⚠️  No gems found scoring ≥ 0.45 in the last 2 hours.")
            print("    (This is normal — <1% of tokens survive)")

        if watchlist:
            print(f"\n👀 WATCHLIST (score 0.25–0.44) — {len(watchlist)} tokens:\n")
            for w in watchlist[:10]:
                champ = "🍾" if w["is_champ"] else "  "
                print(
                    f"  {champ} {w['name'][:25]:25s} ${w['symbol']:8s} "
                    f"Score:{w['score']:.0%}  Liq:${w['liq']:>8,.0f}  "
                    f"Vol:${w['vol24']:>8,.0f}  Buys1h:{w['buys_1h']:>3}  "
                    f"[{w['platform']}]"
                )

        dead = len(candidates) - len(dex_results)
        print(f"\n💀 Dead on arrival (no DEX data): {dead}/{len(candidates)} candidates")
        print(f"🏦 Bankr tokens skipped: {bankr_skipped}")


if __name__ == "__main__":
    import sys
    hours = int(sys.argv[1]) if len(sys.argv) > 1 else 2
    asyncio.run(scan(hours=hours))
