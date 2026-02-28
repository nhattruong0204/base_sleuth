"""Deep analysis: fetch all tokens from DB, compare with live DexScreener, find missed gems."""
import asyncio
import json
import statistics
import sys

sys.path.insert(0, "/opt/base-sleuth")

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

DB_URL = "postgresql+asyncpg://clanker:clanker@db:5432/clanker_tracker"
DEX_URL = "https://api.dexscreener.com/latest/dex"


async def main():
    engine = create_async_engine(DB_URL)
    async with engine.begin() as conn:
        total = (await conn.execute(text("SELECT count(*) FROM tokens"))).scalar()
        alerted = (await conn.execute(text("SELECT count(*) FROM tokens WHERE alert_sent = true"))).scalar()
        with_score = (await conn.execute(text("SELECT count(*) FROM tokens WHERE quality_score IS NOT NULL AND quality_score > 0"))).scalar()

        print(f"=== DATABASE OVERVIEW ===")
        print(f"Total tokens: {total:,}")
        print(f"With score > 0: {with_score:,}")
        print(f"Alert sent: {alerted}")

        # ALL alerted tokens
        rows = (await conn.execute(text("""
            SELECT t.id, t.symbol, t.name, t.contract_address, t.quality_score,
                   t.is_champagne, t.is_breakout, t.discovery_source,
                   t.social_media_urls, t.launch_platform, t.updated_at,
                   tm.fdv_usd, tm.market_cap_usd, tm.liquidity_usd,
                   tm.volume_1h_usd, tm.buys_1h, tm.sells_1h,
                   t.rejection_reason
            FROM tokens t
            LEFT JOIN token_metrics tm ON tm.token_id = t.id
            WHERE t.alert_sent = true
            ORDER BY t.quality_score DESC
        """))).fetchall()

        print(f"\n=== ALL {len(rows)} ALERTED TOKENS ===")
        alerted_tokens = []
        for r in rows:
            alerted_tokens.append({
                "id": r[0], "symbol": r[1], "name": r[2], "address": r[3],
                "score": float(r[4] or 0), "champagne": r[5], "breakout": r[6],
                "source": r[7], "social": r[8], "platform": r[9],
                "created_at": str(r[10]), "rec_fdv": float(r[11] or 0),
                "rec_mcap": float(r[12] or 0), "rec_liq": float(r[13] or 0),
                "vol_1h": float(r[14] or 0), "buys_1h": int(r[15] or 0),
                "sells_1h": int(r[16] or 0), "rejection": r[17]
            })

        # HIGH-SCORE non-alerted tokens (potential missed gems)
        missed = (await conn.execute(text("""
            SELECT t.id, t.symbol, t.name, t.contract_address, t.quality_score,
                   t.is_champagne, t.is_breakout, t.discovery_source,
                   t.social_media_urls, t.launch_platform, t.updated_at,
                   tm.fdv_usd, tm.market_cap_usd, tm.liquidity_usd,
                   tm.volume_1h_usd, tm.buys_1h, tm.sells_1h,
                   t.rejection_reason
            FROM tokens t
            LEFT JOIN token_metrics tm ON tm.token_id = t.id
            WHERE t.alert_sent = false AND t.quality_score >= 0.20
            ORDER BY t.quality_score DESC
            LIMIT 500
        """))).fetchall()

        missed_tokens = []
        for r in missed:
            missed_tokens.append({
                "id": r[0], "symbol": r[1], "name": r[2], "address": r[3],
                "score": float(r[4] or 0), "champagne": r[5], "breakout": r[6],
                "source": r[7], "social": r[8], "platform": r[9],
                "created_at": str(r[10]), "rec_fdv": float(r[11] or 0),
                "rec_mcap": float(r[12] or 0), "rec_liq": float(r[13] or 0),
                "vol_1h": float(r[14] or 0), "buys_1h": int(r[15] or 0),
                "sells_1h": int(r[16] or 0), "rejection": r[17]
            })

        # Score distribution
        dist = (await conn.execute(text("""
            SELECT
                CASE
                    WHEN quality_score >= 0.55 THEN '0.55+'
                    WHEN quality_score >= 0.45 THEN '0.45-0.55'
                    WHEN quality_score >= 0.35 THEN '0.35-0.45'
                    WHEN quality_score >= 0.25 THEN '0.25-0.35'
                    WHEN quality_score >= 0.15 THEN '0.15-0.25'
                    WHEN quality_score > 0 THEN '0.01-0.15'
                    ELSE '0.00'
                END as bucket,
                count(*)
            FROM tokens
            WHERE quality_score IS NOT NULL
            GROUP BY bucket
            ORDER BY bucket DESC
        """))).fetchall()

        print(f"\n=== SCORE DISTRIBUTION ===")
        for bucket, cnt in dist:
            print(f"  {bucket}: {cnt:,}")

        # Alert outcome stats
        outcomes = (await conn.execute(text("""
            SELECT outcome, count(*) FROM alert_outcomes
            WHERE outcome IS NOT NULL
            GROUP BY outcome
        """))).fetchall()
        print(f"\n=== ALERT OUTCOMES ===")
        for outcome, cnt in outcomes:
            print(f"  {outcome}: {cnt}")

    # Fetch live DexScreener data
    all_tokens = alerted_tokens + missed_tokens
    addresses = list(set(t["address"] for t in all_tokens if t["address"]))

    print(f"\n=== FETCHING LIVE DEXSCREENER DATA for {len(addresses)} addresses ===")

    live = {}
    async with httpx.AsyncClient() as http:
        for i in range(0, len(addresses), 30):
            batch = addresses[i : i + 30]
            try:
                resp = await http.get(
                    f"{DEX_URL}/tokens/{','.join(batch)}", timeout=15
                )
                resp.raise_for_status()
                data = resp.json()
                seen = set()
                for pair in data.get("pairs") or []:
                    addr = pair.get("baseToken", {}).get("address", "").lower()
                    if addr and addr not in seen:
                        seen.add(addr)
                        info = pair.get("info") or {}
                        live[addr] = {
                            "fdv": float(pair.get("fdv") or 0),
                            "mcap": float(pair.get("marketCap") or 0),
                            "liq": float(
                                (pair.get("liquidity") or {}).get("usd", 0)
                            ),
                            "vol24h": float(
                                pair.get("volume", {}).get("h24", 0) or 0
                            ),
                            "vol1h": float(
                                pair.get("volume", {}).get("h1", 0) or 0
                            ),
                            "buys1h": int(
                                pair.get("txns", {})
                                .get("h1", {})
                                .get("buys", 0)
                                or 0
                            ),
                            "sells1h": int(
                                pair.get("txns", {})
                                .get("h1", {})
                                .get("sells", 0)
                                or 0
                            ),
                            "priceChange1h": float(
                                pair.get("priceChange", {}).get("h1", 0) or 0
                            ),
                            "priceChange24h": float(
                                pair.get("priceChange", {}).get("h24", 0) or 0
                            ),
                            "dexPaid": info.get("header") is not None
                            or bool(info.get("socials")),
                            "boosted": bool(
                                pair.get("boosts", {}).get("active")
                                if pair.get("boosts")
                                else False
                            ),
                            "pairCreatedAt": pair.get("pairCreatedAt"),
                        }
            except Exception as e:
                print(f"  Batch error: {e}")
            await asyncio.sleep(0.5)

    print(f"Got live data for {len(live)} tokens")

    # ── CLASSIFY ALERTED TOKENS ──
    print(f"\n{'='*80}")
    print(f"=== ALERTED TOKEN DEEP ANALYSIS ({len(alerted_tokens)} tokens) ===")
    print(f"{'='*80}")

    trash = []
    ok = []
    winners = []

    for t in alerted_tokens:
        addr = t["address"].lower()
        lv = live.get(addr)
        t["live"] = lv

        if not lv or lv["fdv"] == 0:
            t["status"] = "DEAD"
            t["mult"] = 0
            trash.append(t)
        elif t["rec_fdv"] > 0:
            mult = lv["fdv"] / t["rec_fdv"]
            t["mult"] = mult
            if mult >= 1.5:
                t["status"] = "WINNER"
                winners.append(t)
            elif mult >= 0.5:
                t["status"] = "OK"
                ok.append(t)
            else:
                t["status"] = "TRASH"
                trash.append(t)
        else:
            t["mult"] = 0
            t["status"] = "NO_FDV"
            trash.append(t)

    print(f"\nWinner (>=1.5X): {len(winners)}")
    print(f"OK (0.5-1.5X):  {len(ok)}")
    print(f"Trash (<0.5X or dead): {len(trash)}")

    print(f"\n--- WINNERS ---")
    for t in sorted(winners, key=lambda x: x["mult"], reverse=True):
        lv = t["live"]
        print(
            f"  {t['mult']:.2f}X ${t['symbol'][:15]:15s} score={t['score']:.2f} "
            f"src={t['source']:10s} "
            f"rec_fdv=${t['rec_fdv']:>12,.0f} live_fdv=${lv['fdv']:>12,.0f} "
            f"liq=${lv['liq']:>8,.0f} "
            f"champ={t['champagne']} brk={t['breakout']} plat={t['platform']}"
        )

    print(f"\n--- TRASH / DEAD ALERTS ---")
    for t in sorted(trash, key=lambda x: x["score"], reverse=True):
        lv = t.get("live")
        live_fdv = lv["fdv"] if lv else 0
        live_liq = lv["liq"] if lv else 0
        social = json.loads(t["social"]) if t["social"] else []
        social_names = [s.get("name", "") for s in social]
        print(
            f"  {t['status']:5s} ${t['symbol'][:15]:15s} score={t['score']:.2f} "
            f"src={t['source']:10s} "
            f"rec_fdv=${t['rec_fdv']:>10,.0f} live_fdv=${live_fdv:>10,.0f} "
            f"live_liq=${live_liq:>8,.0f} "
            f"vol1h=${t['vol_1h']:>8,.0f} buys1h={t['buys_1h']:3d} "
            f"champ={t['champagne']} brk={t['breakout']} "
            f"soc={social_names} plat={t['platform']}"
        )

    # ── MISSED GEM ANALYSIS ──
    print(f"\n{'='*80}")
    print(f"=== MISSED GEM ANALYSIS ({len(missed_tokens)} scored >=0.20, not alerted) ===")
    print(f"{'='*80}")

    real_missed = []
    for t in missed_tokens:
        addr = t["address"].lower()
        lv = live.get(addr)
        if not lv or lv["fdv"] == 0:
            continue
        if t["rec_fdv"] > 0:
            mult = lv["fdv"] / t["rec_fdv"]
        else:
            mult = 0
        t["mult"] = mult
        t["live"] = lv
        if mult >= 1.5 and lv["liq"] >= 5000:
            real_missed.append(t)

    real_missed.sort(key=lambda x: x["mult"], reverse=True)
    print(f"\nREAL MISSED GEMS (>=1.5X, liq>=$5K): {len(real_missed)}")
    for t in real_missed[:50]:
        lv = t["live"]
        social = json.loads(t["social"]) if t["social"] else []
        social_names = [s.get("name", "") for s in social]
        print(
            f"  {t['mult']:.2f}X ${t['symbol'][:15]:15s} score={t['score']:.2f} "
            f"src={t['source']:10s} "
            f"rec_fdv=${t['rec_fdv']:>10,.0f} live_fdv=${lv['fdv']:>10,.0f} "
            f"live_liq=${lv['liq']:>8,.0f} "
            f"vol24h=${lv['vol24h']:>10,.0f} buys1h={lv['buys1h']:3d} "
            f"champ={t['champagne']} brk={t['breakout']} "
            f"soc={social_names} rej={t['rejection']}"
        )

    # ── PATTERN ANALYSIS ──
    print(f"\n{'='*80}")
    print(f"=== PATTERN ANALYSIS ===")
    print(f"{'='*80}")

    # Source breakdown
    print(f"\nSource breakdown for ALERTED:")
    for src in sorted(set(t["source"] for t in alerted_tokens if t["source"])):
        w = len([t for t in winners if t["source"] == src])
        o = len([t for t in ok if t["source"] == src])
        tr = len([t for t in trash if t["source"] == src])
        total_src = w + o + tr
        if total_src > 0:
            print(
                f"  {src:12s}: winner={w} ok={o} trash={tr} "
                f"(win%={w / total_src * 100:.0f}%, trash%={tr / total_src * 100:.0f}%)"
            )

    # Source for missed gems
    print(f"\nSource breakdown for MISSED GEMS:")
    for src in sorted(set(t["source"] for t in real_missed if t["source"])):
        cnt = len([t for t in real_missed if t["source"] == src])
        print(f"  {src:12s}: {cnt}")

    # Score ranges
    print(f"\nScore ranges for WINNERS:")
    for t in sorted(winners, key=lambda x: x["mult"], reverse=True):
        print(f"  {t['score']:.3f} - ${t['symbol']} ({t['mult']:.2f}X)")

    print(f"\nScore ranges for TRASH (top 20 by score):")
    for t in sorted(trash, key=lambda x: x["score"], reverse=True)[:20]:
        print(
            f"  {t['score']:.3f} - ${t['symbol']} [{t['status']}] "
            f"rec_fdv=${t['rec_fdv']:,.0f}"
        )

    # Threshold simulation
    print(f"\nThreshold simulation:")
    for thresh in [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70]:
        w = len([t for t in winners if t["score"] >= thresh])
        tr = len([t for t in trash if t["score"] >= thresh])
        o = len([t for t in ok if t["score"] >= thresh])
        total_t = w + o + tr
        if total_t > 0:
            print(
                f"  thresh={thresh:.2f}: alerts={total_t} winners={w} "
                f"trash={tr} win%={w / total_t * 100:.1f}% "
                f"trash%={tr / total_t * 100:.1f}%"
            )

    # DexPaid analysis for missed gems
    print(f"\nDexScreener paid/boosted for missed gems:")
    paid_m = [t for t in real_missed if t.get("live", {}).get("dexPaid")]
    unpaid_m = [t for t in real_missed if not t.get("live", {}).get("dexPaid")]
    print(f"  Paid DexScreener: {len(paid_m)}")
    print(f"  NOT paid DexScreener: {len(unpaid_m)}")
    if unpaid_m:
        print(f"  Unpaid missed gems (community/organic):")
        for t in unpaid_m[:20]:
            lv = t["live"]
            print(
                f"    {t['mult']:.2f}X ${t['symbol'][:15]:15s} "
                f"fdv=${lv['fdv']:>10,.0f} liq=${lv['liq']:>8,.0f} "
                f"vol24=${lv['vol24h']:>10,.0f}"
            )

    # DexPaid for alerted winners vs trash
    print(f"\nDexPaid status for ALERTED tokens:")
    w_paid = len([t for t in winners if t.get("live", {}).get("dexPaid")])
    w_unpaid = len(winners) - w_paid
    tr_paid = len([t for t in trash if t.get("live", {}).get("dexPaid")])
    tr_unpaid = len(trash) - tr_paid
    print(f"  Winners: paid={w_paid} unpaid={w_unpaid}")
    print(f"  Trash:   paid={tr_paid} unpaid={tr_unpaid}")

    # Platform breakdown
    print(f"\nPlatform for alerted tokens:")
    for plat in sorted(set(t["platform"] for t in alerted_tokens if t["platform"])):
        w = len([t for t in winners if t["platform"] == plat])
        tr = len([t for t in trash if t["platform"] == plat])
        total_p = len([t for t in alerted_tokens if t["platform"] == plat])
        print(f"  {plat:15s}: total={total_p} winners={w} trash={tr}")

    # FDV at alert time
    print(f"\nRec FDV at alert time:")
    w_fdvs = sorted([t["rec_fdv"] for t in winners if t["rec_fdv"] > 0])
    tr_fdvs = sorted([t["rec_fdv"] for t in trash if t["rec_fdv"] > 0])

    def show_stats(lst, label):
        if lst:
            print(
                f"  {label}: n={len(lst)} "
                f"median=${statistics.median(lst):,.0f} "
                f"p25=${lst[len(lst) // 4]:,.0f} "
                f"p75=${lst[3 * len(lst) // 4]:,.0f} "
                f"min=${lst[0]:,.0f} max=${lst[-1]:,.0f}"
            )

    show_stats(w_fdvs, "Winners")
    show_stats(tr_fdvs, "Trash  ")

    # MCap gate analysis
    print(f"\nMCap gate analysis (current gate: $50K):")
    for gate in [10000, 25000, 50000, 75000, 100000]:
        w = len([t for t in winners if t["rec_mcap"] >= gate])
        tr = len([t for t in trash if t["rec_mcap"] >= gate])
        total_g = w + tr
        if total_g > 0:
            print(
                f"  gate=${gate:>8,}: pass={total_g} win={w} "
                f"trash={tr} win%={w / total_g * 100:.0f}%"
            )

    # Liquidity at alert time vs outcome
    print(f"\nLiquidity at alert time:")
    show_stats(
        sorted([t["rec_liq"] for t in winners if t["rec_liq"] > 0]), "Winners"
    )
    show_stats(
        sorted([t["rec_liq"] for t in trash if t["rec_liq"] > 0]), "Trash  "
    )

    # Buy count at alert time
    print(f"\nBuys/1h at alert time:")
    show_stats(
        sorted([t["buys_1h"] for t in winners if t["buys_1h"] > 0]), "Winners"
    )
    show_stats(
        sorted([t["buys_1h"] for t in trash if t["buys_1h"] > 0]), "Trash  "
    )

    # Volume/1h at alert time
    print(f"\nVolume/1h at alert time:")
    show_stats(
        sorted([t["vol_1h"] for t in winners if t["vol_1h"] > 0]), "Winners"
    )
    show_stats(
        sorted([t["vol_1h"] for t in trash if t["vol_1h"] > 0]), "Trash  "
    )

    # WHY missed gems were rejected
    print(f"\nRejection reasons for MISSED GEMS:")
    reasons = {}
    for t in real_missed:
        r = t["rejection"] or "no rejection reason"
        reasons[r] = reasons.get(r, 0) + 1
    for r, cnt in sorted(reasons.items(), key=lambda x: -x[1]):
        print(f"  {cnt:3d}x - {r}")

    # Key metric differences: what separates winners from trash at alert time
    print(f"\n{'='*80}")
    print(f"=== KEY DISCRIMINATORS (winners vs trash at alert time) ===")
    print(f"{'='*80}")

    def safe_median(lst):
        return statistics.median(lst) if lst else 0

    print(f"\n{'Metric':<25s} {'Winners median':>15s} {'Trash median':>15s} {'Ratio':>8s}")
    print(f"{'-'*63}")

    w_liq = safe_median([t["rec_liq"] for t in winners if t["rec_liq"] > 0])
    t_liq = safe_median([t["rec_liq"] for t in trash if t["rec_liq"] > 0])
    print(f"{'Liquidity $':<25s} {w_liq:>15,.0f} {t_liq:>15,.0f} {w_liq / t_liq if t_liq else 0:>8.1f}x")

    w_v = safe_median([t["vol_1h"] for t in winners if t["vol_1h"] > 0])
    t_v = safe_median([t["vol_1h"] for t in trash if t["vol_1h"] > 0])
    print(f"{'Volume/1h $':<25s} {w_v:>15,.0f} {t_v:>15,.0f} {w_v / t_v if t_v else 0:>8.1f}x")

    w_b = safe_median([t["buys_1h"] for t in winners if t["buys_1h"] > 0])
    t_b = safe_median([t["buys_1h"] for t in trash if t["buys_1h"] > 0])
    print(f"{'Buys/1h':<25s} {w_b:>15,.0f} {t_b:>15,.0f} {w_b / t_b if t_b else 0:>8.1f}x")

    w_f = safe_median([t["rec_fdv"] for t in winners if t["rec_fdv"] > 0])
    t_f = safe_median([t["rec_fdv"] for t in trash if t["rec_fdv"] > 0])
    print(f"{'FDV $':<25s} {w_f:>15,.0f} {t_f:>15,.0f} {w_f / t_f if t_f else 0:>8.1f}x")

    w_s = safe_median([t["score"] for t in winners])
    t_s = safe_median([t["score"] for t in trash])
    print(f"{'Quality Score':<25s} {w_s:>15.3f} {t_s:>15.3f} {w_s / t_s if t_s else 0:>8.1f}x")

    await engine.dispose()


asyncio.run(main())
