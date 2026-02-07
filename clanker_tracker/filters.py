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
            self._smart_wallets = {
                line.strip().lower()
                for line in path.read_text().splitlines()
                if line.strip() and not line.startswith("#")
            }
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

        s2 = self._stage2_dex_metrics(token, metrics)
        result.stage_results.append(s2)
        result.stage_reached = 2

        # ── Stage 3: Momentum detection ──
        s3 = self._stage3_momentum(token, metrics)
        result.stage_results.append(s3)
        result.stage_reached = 3

        # ── Stage 4: Smart money ──
        s4 = self._stage4_smart_money(metrics)
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
        """Score based on early trading activity from DexScreener."""
        if metrics is None:
            return StageResult(passed=True, score=0.1, reason="No DEX data yet")

        score = 0.0
        reasons: list[str] = []

        # Liquidity
        if metrics.liquidity_usd and metrics.liquidity_usd >= self.cfg.min_pool_liquidity_usd:
            score += 0.30
        else:
            reasons.append(f"liq ${metrics.liquidity_usd or 0:,.0f}")

        # Market cap
        if metrics.market_cap_usd and metrics.market_cap_usd >= self.cfg.min_mcap_usd:
            score += 0.20
        else:
            reasons.append("low mcap")

        # 1h volume
        if metrics.volume_1h_usd and metrics.volume_1h_usd >= self.cfg.min_volume_1h_usd:
            score += 0.25
        else:
            reasons.append(f"vol1h ${metrics.volume_1h_usd or 0:,.0f}")

        # Buy/sell ratio (healthy markets have balanced or buy-heavy activity)
        buys = metrics.buys_1h or 0
        sells = metrics.sells_1h or 0
        total = buys + sells
        if total > 0:
            ratio = buys / total
            if ratio >= self.cfg.min_buy_sell_ratio:
                score += 0.15
            else:
                reasons.append(f"b/s ratio {ratio:.2f}")
        else:
            reasons.append("no txns")

        # Holder bonus
        if metrics.holder_count and metrics.holder_count >= self.cfg.min_holders:
            score = min(score + 0.10, 1.0)

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
        - Active buying pressure (buy count in 1h)
        - Volume-to-liquidity ratio (demand pressure proxy)
        """
        if metrics is None:
            return StageResult(passed=True, score=0.0, reason="No metrics for momentum")

        score = 0.0
        signals: list[str] = []

        # Volume velocity: high 5m vol relative to 1h = accelerating
        vol_5m = metrics.volume_5m_usd or 0
        vol_1h = metrics.volume_1h_usd or 0
        if vol_5m >= self.cfg.min_volume_5m_usd:
            score += 0.25
            if vol_1h > 0 and vol_5m / vol_1h > 0.3:
                score += 0.15
                signals.append(f"vol accel {vol_5m / vol_1h:.0%}")

        # Active buying pressure
        buys_1h = metrics.buys_1h or 0
        if buys_1h >= self.cfg.min_buys_1h:
            score += 0.20
            signals.append(f"{buys_1h} buys/1h")

        # Volume-to-liquidity ratio as demand pressure proxy
        if metrics.liquidity_usd and metrics.liquidity_usd > 0:
            vol_liq_ratio = vol_1h / metrics.liquidity_usd
            if vol_liq_ratio > 1.0:
                score += 0.25
                signals.append(f"vol/liq {vol_liq_ratio:.1f}x")
            elif vol_liq_ratio > 0.3:
                score += 0.15
                signals.append(f"vol/liq {vol_liq_ratio:.1f}x")

        return StageResult(
            passed=True,
            score=round(min(score, 1.0), 3),
            reason="; ".join(signals) if signals else "no momentum signals",
        )

    # ------------------------------------------------------------------
    # Stage 4: Smart money
    # ------------------------------------------------------------------

    def _stage4_smart_money(self, metrics: Optional[TokenMetrics]) -> StageResult:
        """Score based on smart money wallet presence."""
        if not self._smart_wallets:
            return StageResult(passed=True, score=0.0, reason="No smart wallet list")

        sm_count = metrics.smart_money_holders if metrics else 0
        if sm_count and sm_count > 0:
            score = min(0.25 + 0.25 * sm_count, 1.0)
            return StageResult(
                passed=True,
                score=round(score, 3),
                reason=f"{sm_count} smart wallets",
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
        """Weighted combination of stages 2–5 (stage 1 is pass/fail gate)."""
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
