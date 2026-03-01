"""Multi-stage quality scoring pipeline for newly discovered tokens.

Designed for the reality of Clanker (Feb 2026):
- 38K+ tokens/day, 92% Bankr bot, 99%+ dead on arrival
- Only ~0.02% get the champagne curated tag
- Most tokens never appear on DexScreener at all

Pipeline
--------
Pre-filter : Skip Bankr (optional), require social links, scam keywords
Stage 1    : Instant reject — scam names, no data
Stage 2    : DEX metrics — liquidity, volume, buy/sell (DexScreener batch)
Stage 3    : Momentum — price surge, accelerating buys, volume velocity
Stage 4    : Smart money — known profitable wallets detected
Stage 5    : Context quality — origin traceback, social presence

Champagne tokens get a bonus and skip Stage 1.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AppConfig
from .models import Token, TokenContext, TokenMetrics

logger = structlog.get_logger(__name__)


@dataclass
class StageResult:
    """Outcome of a single filter stage."""
    passed: bool
    score: float  # 0.0 – 1.0
    reason: str = ""


@dataclass
class FilterResult:
    """Aggregated result across all stages."""
    token_id: int
    stage_results: list[StageResult] = field(default_factory=list)
    final_score: float = 0.0
    rejected: bool = False
    rejection_reason: str = ""
    stage_reached: int = 0
    metrics: Optional[TokenMetrics] = None  # DEX snapshot at eval time


class TokenFilter:
    """Runs the multi-stage quality filter on a token."""

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg = config.filtering
        self.dex_cfg = config.dexscreener
        self._http = http
        self._smart_wallets: set[str] = set()
        self._breakout_bonus: float = config.breakout.breakout_score_bonus
        self._load_smart_wallets()

    def _load_smart_wallets(self) -> None:
        path = Path(self.cfg.smart_money_wallet_file)
        if path.exists():
            self._smart_wallets = set()
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                # Support "address alias" format — take only the address
                addr = line.split()[0].lower()
                if addr.startswith("0x") and len(addr) == 42:
                    self._smart_wallets.add(addr)
            logger.info("smart_wallets.loaded", count=len(self._smart_wallets))
        else:
            logger.warning("smart_wallets.file_missing", path=str(path))

    # ------------------------------------------------------------------
    # Pre-filter — discard obvious trash before any API calls
    # ------------------------------------------------------------------

    def pre_filter(self, token: Token) -> tuple[bool, str]:
        """Fast pre-filter that avoids any external API calls.

        Returns (passed, reason).
        """
        # Optionally skip all Bankr launches
        if self.cfg.skip_bankr and token.is_bankr_launch:
            return False, "Bankr launch skipped"

        # Require social links
        if self.cfg.require_social_links:
            social = json.loads(token.social_media_urls) if token.social_media_urls else []
            if not social:
                return False, "No social links"

        return True, "pre-filter passed"

    # ------------------------------------------------------------------
    # Full evaluation pipeline
    # ------------------------------------------------------------------

    async def evaluate(
        self, token: Token, session: AsyncSession,
    ) -> FilterResult:
        """Run all stages and compute a final weighted score."""
        result = FilterResult(token_id=token.id)

        # ── Stage 1: Instant reject ──
        if self.cfg.champagne_auto_pass_stage1 and token.is_champagne:
            s1 = StageResult(passed=True, score=1.0, reason="champagne auto-pass")
        else:
            s1 = self._stage1_instant_reject(token)
        result.stage_results.append(s1)
        result.stage_reached = 1
        if not s1.passed:
            result.rejected = True
            result.rejection_reason = s1.reason
            result.final_score = 0.0
            await self._persist_score(token, result, session)
            return result

        # ── Stage 2: DEX metrics (DexScreener) ──
        metrics = await self._fetch_dex_metrics(token)
        if metrics:
            # Persist the metrics snapshot
            metrics.token_id = token.id
            session.add(metrics)
        result.metrics = metrics  # Expose to caller for alert messages

        s2 = self._stage2_dex_metrics(token, metrics)
        result.stage_results.append(s2)
        result.stage_reached = 2

        # ── Stage 3: Momentum detection ──
        s3 = self._stage3_momentum(token, metrics)
        result.stage_results.append(s3)
        result.stage_reached = 3

        # ── Stage 4: Smart money ──
        s4 = await self._stage4_smart_money(token, metrics, session)
        result.stage_results.append(s4)
        result.stage_reached = 4

        # ── Stage 5: Context quality ──
        # Always use explicit async get — never lazy-load relationships
        # in async sessions (triggers MissingGreenlet error with asyncpg)
        ctx = await session.get(TokenContext, token.id)
        s5 = self._stage5_context_quality(token, ctx)
        result.stage_results.append(s5)
        result.stage_reached = 5

        # ── Weighted score ──
        result.final_score = self._compute_weighted_score(
            token, s2, s3, s4, s5,
        )

        if result.final_score < self.cfg.score_threshold:
            result.rejected = True
            result.rejection_reason = (
                f"Score {result.final_score:.2f} below threshold "
                f"{self.cfg.score_threshold}"
            )

        await self._persist_score(token, result, session)
        logger.info(
            "filter.result",
            token=token.symbol,
            score=round(result.final_score, 3),
            rejected=result.rejected,
            champagne=token.is_champagne,
            breakout=getattr(token, "is_breakout", False),
            platform=token.launch_platform,
        )
        return result

    # ------------------------------------------------------------------
    # Stage 1: Instant reject
    # ------------------------------------------------------------------

    def _stage1_instant_reject(self, token: Token) -> StageResult:
        """Hard-reject tokens that are obvious trash."""
        name_lower = (token.name or "").lower()
        symbol_lower = (token.symbol or "").lower()

        # Scam keyword in name / symbol
        for kw in self.cfg.scam_keywords:
            if kw in name_lower or kw in symbol_lower:
                return StageResult(
                    passed=False, score=0.0, reason=f"Scam keyword: {kw}",
                )

        return StageResult(passed=True, score=1.0)

    # ------------------------------------------------------------------
    # Stage 2: DEX metrics
    # ------------------------------------------------------------------

    def _stage2_dex_metrics(
        self, token: Token, metrics: Optional[TokenMetrics],
    ) -> StageResult:
        """Score based on early trading activity from DexScreener.

        Includes bot-buy detection: tokens with hundreds/thousands of tiny
        buys (avg < $100) are bot-sprayed and get penalised.
        """
        if metrics is None:
            return StageResult(passed=True, score=0.1, reason="No DEX data yet")

        score = 0.0
        reasons: list[str] = []

        liq = metrics.liquidity_usd or 0
        mcap = metrics.market_cap_usd or 0
        vol_1h = metrics.volume_1h_usd or 0
        buys = metrics.buys_1h or 0
        sells = metrics.sells_1h or 0
        total_txns = buys + sells

        # ── Bot-buy spray detection ──
        # Real tokens: 20-100 buys/1h with avg $200+.
        # Bot-sprayed: 300-2000+ buys/1h with avg <$100.
        avg_buy = vol_1h / buys if buys > 0 else 0
        is_bot_spray = (
            buys >= self.cfg.bot_buy_threshold
            and avg_buy < self.cfg.bot_avg_buy_max_usd
        )
        if is_bot_spray:
            reasons.append(
                f"BOT SPRAY {buys} buys avg ${avg_buy:.0f}"
            )
            return StageResult(
                passed=True,
                score=round(0.05, 3),
                reason="; ".join(reasons),
            )

        # ── Wash-trading detection ──
        # Tokens with vol_1h > 2x liquidity AND 200+ buys are likely
        # wash-traded. 73% of dead alerts showed this pattern.
        is_wash_trading = False
        if (
            liq > 0
            and buys >= self.cfg.wash_min_buys
            and vol_1h / liq > self.cfg.wash_vol_liq_ratio
        ):
            is_wash_trading = True
            reasons.append(
                f"WASH vol/liq {vol_1h / liq:.1f}x + {buys} buys"
            )

        # ── Liquidity (primary signal) ──
        if liq >= self.cfg.min_pool_liquidity_usd:
            # Scale: $1K=0.15, $5K=0.20, $10K+=0.30
            if liq >= 10_000:
                score += 0.30
            elif liq >= 5_000:
                score += 0.20
            else:
                score += 0.15
        else:
            reasons.append(f"liq ${liq:,.0f}")

        # ── Market cap ──
        if mcap >= self.cfg.min_mcap_usd:
            score += 0.15
        else:
            reasons.append("low mcap")

        # ── Volume quality (1h) ──
        if vol_1h >= self.cfg.min_volume_1h_usd:
            score += 0.20
            # Bonus for strong volume relative to liquidity (demand pressure)
            if liq > 0 and vol_1h / liq > 0.5:
                score += 0.10
                reasons.append(f"vol/liq {vol_1h / liq:.1f}x")
        else:
            reasons.append(f"vol1h ${vol_1h:,.0f}")

        # ── Buy/sell health ──
        if total_txns > 0:
            ratio = buys / total_txns
            if ratio >= self.cfg.min_buy_sell_ratio:
                score += 0.10
            else:
                reasons.append(f"b/s {ratio:.2f}")
        else:
            reasons.append("no txns")

        # ── Holder bonus ──
        if metrics.holder_count and metrics.holder_count >= self.cfg.min_holders:
            score = min(score + 0.10, 1.0)

        # ── Average buy size quality bonus ──
        # Healthy buys: avg > $200 indicates real traders, not bots
        if buys > 5 and avg_buy >= 200:
            score = min(score + 0.05, 1.0)
            reasons.append(f"avg buy ${avg_buy:.0f}")

        # ── Wash-trading penalty ──
        # Apply after all scoring if wash trading detected
        if is_wash_trading:
            score *= self.cfg.wash_score_multiplier

        return StageResult(
            passed=True,
            score=round(score, 3),
            reason="; ".join(reasons) if reasons else "metrics ok",
        )

    # ------------------------------------------------------------------
    # Stage 3: Momentum detection (the gem signal!)
    # ------------------------------------------------------------------

    def _stage3_momentum(
        self, token: Token, metrics: Optional[TokenMetrics],
    ) -> StageResult:
        """Detect momentum signals that indicate early gem potential.

        Key signals:
        - Volume velocity (5m volume relative to 1h)
        - Active buying pressure (buy count in 1h) — capped, not infinite
        - Volume-to-liquidity ratio (demand pressure proxy)
        - Organic activity signal (genuine buys without DexScreener profile)
        """
        if metrics is None:
            return StageResult(passed=True, score=0.0, reason="No metrics for momentum")

        score = 0.0
        signals: list[str] = []

        vol_5m = metrics.volume_5m_usd or 0
        vol_1h = metrics.volume_1h_usd or 0
        buys_1h = metrics.buys_1h or 0
        liq = metrics.liquidity_usd or 0

        # Volume velocity: high 5m vol relative to 1h = accelerating
        if vol_5m >= self.cfg.min_volume_5m_usd:
            score += 0.20
            if vol_1h > 0 and vol_5m / vol_1h > 0.3:
                score += 0.10
                signals.append(f"vol accel {vol_5m / vol_1h:.0%}")

        # Active buying pressure — capped to avoid rewarding bot spray
        if buys_1h >= self.cfg.min_buys_1h:
            # Diminishing returns: 5-30 buys = good, 30-100 = great, >100 = no extra
            if buys_1h <= 100:
                score += 0.20
            else:
                score += 0.15  # Slightly less for very high counts
            signals.append(f"{buys_1h} buys/1h")

        # Volume-to-liquidity ratio as demand pressure proxy
        if liq > 0:
            vol_liq_ratio = vol_1h / liq
            if vol_liq_ratio > 1.0:
                score += 0.25
                signals.append(f"vol/liq {vol_liq_ratio:.1f}x")
            elif vol_liq_ratio > 0.3:
                score += 0.15
                signals.append(f"vol/liq {vol_liq_ratio:.1f}x")

        # ── Organic gem signal ──
        # Tokens with genuine trading activity but no DexScreener paid
        # profile are community-driven — potential organic gems.
        # (We detect this as vol > $5K, buys > 20, but token doesn't
        # have a DexScreener profile. Boost captured at weighted score level.)
        avg_buy = vol_1h / buys_1h if buys_1h > 0 else 0
        if (
            buys_1h >= 15
            and vol_1h >= 3_000
            and avg_buy >= 100
            and liq >= 3_000
        ):
            score += 0.10
            signals.append("organic activity")

        return StageResult(
            passed=True,
            score=round(min(score, 1.0), 3),
            reason="; ".join(signals) if signals else "no momentum signals",
        )

    # ------------------------------------------------------------------
    # Stage 4: Smart money
    # ------------------------------------------------------------------

    async def _stage4_smart_money(
        self, token: Token, metrics: Optional[TokenMetrics], session: AsyncSession,
    ) -> StageResult:
        """Score based on smart money wallet presence.

        Checks two sources:
        1. WalletSwap table — tracked wallets that bought this token
        2. In-memory smart wallet set from data/smart_money_wallets.txt

        Higher tier wallets (Tier 1/2) contribute more to the score.
        """
        if not self._smart_wallets:
            return StageResult(passed=True, score=0.0, reason="No smart wallet list")

        sm_count = 0
        tier1_count = 0
        tier2_count = 0

        # Check WalletSwap records for this token
        if token.contract_address:
            from sqlalchemy import select, func
            from .models import WalletSwap, SmartWallet

            token_addr = token.contract_address.lower()

            # Count distinct tracked wallets that bought this token
            swap_stmt = (
                select(WalletSwap.wallet_address)
                .where(WalletSwap.token_address == token_addr)
                .where(WalletSwap.action == "buy")
                .distinct()
            )
            swap_rows = (await session.execute(swap_stmt)).scalars().all()
            smart_buyers = {
                addr for addr in swap_rows if addr in self._smart_wallets
            }
            sm_count = len(smart_buyers)

            # Get tier breakdown for scoring
            if smart_buyers:
                tier_stmt = (
                    select(SmartWallet.tier, func.count(SmartWallet.id))
                    .where(SmartWallet.address.in_(smart_buyers))
                    .group_by(SmartWallet.tier)
                )
                tier_rows = (await session.execute(tier_stmt)).all()
                for tier, count in tier_rows:
                    if tier == 1:
                        tier1_count = count
                    elif tier == 2:
                        tier2_count = count

        # Also check old-style smart_money_holders from metrics
        if metrics and metrics.smart_money_holders:
            sm_count = max(sm_count, metrics.smart_money_holders)

        if sm_count > 0:
            # Tiered scoring: Tier 1 wallets worth more
            base_score = min(0.20 + 0.20 * sm_count, 0.80)
            tier_bonus = tier1_count * 0.10 + tier2_count * 0.05
            score = min(base_score + tier_bonus, 1.0)

            parts = [f"{sm_count} smart wallets"]
            if tier1_count:
                parts.append(f"{tier1_count} T1")
            if tier2_count:
                parts.append(f"{tier2_count} T2")

            return StageResult(
                passed=True,
                score=round(score, 3),
                reason=", ".join(parts),
            )

        return StageResult(passed=True, score=0.0, reason="No smart money detected")

    # ------------------------------------------------------------------
    # Stage 5: Context quality
    # ------------------------------------------------------------------

    def _stage5_context_quality(
        self, token: Token, ctx: Optional[TokenContext],
    ) -> StageResult:
        """Score based on origin traceability and social presence."""
        score = 0.0

        # Social links from Clanker API
        social = json.loads(token.social_media_urls) if token.social_media_urls else []
        has_twitter = any(
            s.get("name", "").lower() in ("x", "twitter") for s in social
        )
        has_website = any(s.get("name", "").lower() == "website" for s in social)

        if has_twitter:
            score += 0.25
        if has_website:
            score += 0.15
        if len(social) >= 2:
            score += 0.10

        # Origin resolution context
        if ctx is None or ctx.resolution_strategy == "unresolved":
            if self.cfg.require_origin_url:
                return StageResult(
                    passed=False, score=0.0, reason="Origin required but unresolved",
                )
        else:
            if ctx.origin_url:
                score += 0.20
            if ctx.origin_platform in ("x", "farcaster"):
                score += 0.10
            if ctx.origin_text:
                score += 0.10
            if ctx.project_idea:
                score += 0.10

        return StageResult(
            passed=True,
            score=round(min(score, 1.0), 3),
            reason=f"twitter={has_twitter} website={has_website}",
        )

    # ------------------------------------------------------------------
    # Score aggregation
    # ------------------------------------------------------------------

    def _compute_weighted_score(
        self,
        token: Token,
        s2: StageResult,
        s3: StageResult,
        s4: StageResult,
        s5: StageResult,
    ) -> float:
        """Weighted combination of stages 2–5 (stage 1 is pass/fail gate).

        Weight philosophy (v3 — data-driven Feb 2026):
        - Metrics (2.0): Liquidity + volume are the primary survival signals
        - Momentum (1.5): Buying pressure confirms interest
        - Smart money (2.0): Whale wallets (when data available)
        - Context (0.3): Social links are baseline, NOT differentiators

        Source-aware penalties:
        - Firehose tokens get penalised (84% trash rate historically)
        - Boost/trending tokens are pre-validated by DexScreener
        """
        w_metrics = self.cfg.weight_metrics
        w_momentum = self.cfg.weight_momentum
        w_smart = self.cfg.weight_smart_money
        w_context = self.cfg.weight_context

        total_weight = w_metrics + w_momentum + w_smart + w_context
        raw = (
            s2.score * w_metrics
            + s3.score * w_momentum
            + s4.score * w_smart
            + s5.score * w_context
        ) / total_weight

        # ── Source-aware adjustment ──
        source = getattr(token, "discovery_source", "") or ""
        if source == "firehose":
            # Firehose has 0% winners historically — heavy penalty
            raw *= self.cfg.firehose_score_penalty
        elif source in ("community_takeover",):
            # Community takeovers have 0% trash — slight bonus
            raw += 0.05
        elif source.startswith("boost_top"):
            # boost_top has 33% win rate — best source
            raw += 0.03

        # Champagne bonus — curated tokens get a flat boost
        if token.is_champagne:
            raw += self.cfg.weight_champagne_bonus

        # Breakout bonus — tokens detected via DexScreener trending
        if getattr(token, "is_breakout", False):
            raw += self._breakout_bonus

        return round(min(raw, 1.0), 4)

    # ------------------------------------------------------------------
    # DexScreener integration (single token)
    # ------------------------------------------------------------------

    async def _fetch_dex_metrics(self, token: Token) -> Optional[TokenMetrics]:
        """Fetch latest metrics from DexScreener API for a single token."""
        if not token.contract_address:
            return None

        url = f"{self.dex_cfg.base_url}/tokens/{token.contract_address}"
        try:
            resp = await self._http.get(url, timeout=10)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.debug("dex.fetch.failed", token=token.symbol, error=str(exc))
            return None

        data = resp.json()
        pairs = data.get("pairs") or []
        if not pairs:
            return None

        pair = pairs[0]
        return self._parse_dex_pair(pair)

    # ------------------------------------------------------------------
    # DexScreener batch lookup (up to 30 tokens at once)
    # ------------------------------------------------------------------

    async def fetch_dex_metrics_batch(
        self, tokens: list[Token],
    ) -> dict[str, TokenMetrics]:
        """Batch-fetch DEX metrics for multiple tokens.

        DexScreener supports comma-separated addresses (max 30).
        Returns a dict mapping lowercase contract_address → TokenMetrics.
        """
        result: dict[str, TokenMetrics] = {}
        batch_size = self.dex_cfg.batch_size

        for i in range(0, len(tokens), batch_size):
            batch = tokens[i : i + batch_size]
            addrs = ",".join(t.contract_address for t in batch)
            try:
                resp = await self._http.get(
                    f"{self.dex_cfg.base_url}/tokens/{addrs}",
                    timeout=15,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                logger.warning(
                    "dex.batch.failed", batch_size=len(batch), error=str(exc),
                )
                continue

            data = resp.json()
            seen: set[str] = set()
            for pair in data.get("pairs") or []:
                addr = pair.get("baseToken", {}).get("address", "").lower()
                if addr and addr not in seen:
                    seen.add(addr)
                    metrics = self._parse_dex_pair(pair)
                    result[addr] = metrics

        logger.info(
            "dex.batch.done",
            requested=len(tokens),
            got_data=len(result),
        )
        return result

    @staticmethod
    def _parse_dex_pair(pair: dict) -> TokenMetrics:
        """Parse a single DexScreener pair response into TokenMetrics."""
        txns_5m = pair.get("txns", {}).get("m5", {})
        txns_1h = pair.get("txns", {}).get("h1", {})
        volume = pair.get("volume", {})

        return TokenMetrics(
            price_usd=_float(pair.get("priceUsd")),
            market_cap_usd=_float(pair.get("marketCap")),
            fdv_usd=_float(pair.get("fdv")),
            liquidity_usd=_float((pair.get("liquidity") or {}).get("usd")),
            volume_5m_usd=_float(volume.get("m5")),
            volume_1h_usd=_float(volume.get("h1")),
            volume_24h_usd=_float(volume.get("h24")),
            buys_5m=txns_5m.get("buys"),
            sells_5m=txns_5m.get("sells"),
            buys_1h=txns_1h.get("buys"),
            sells_1h=txns_1h.get("sells"),
        )

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @staticmethod
    async def _persist_score(
        token: Token, result: FilterResult, session: AsyncSession,
    ) -> None:
        token.quality_score = result.final_score
        token.filter_stage_reached = result.stage_reached
        token.rejection_reason = result.rejection_reason or None
        await session.flush()


def _float(val) -> Optional[float]:
    """Safe float conversion."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
