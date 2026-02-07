"""4-stage quality scoring pipeline for newly discovered tokens.

Stages
------
1. Instant reject  — no pool, scam keywords, warning flags, low mcap
2. Early metrics   — volume, holders, buy/sell ratio (needs DexScreener data)
3. Smart money     — known profitable wallets buying early
4. Context quality — has traceable origin? real project idea?

Each stage returns a (pass: bool, score: float, reason: str) and the
pipeline produces a weighted final score in [0, 1].
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
    """Runs the 4-stage quality filter on a token."""

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg = config.filtering
        self.dex_cfg = config.dexscreener
        self._http = http
        self._smart_wallets: set[str] = set()
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

    async def evaluate(
        self, token: Token, session: AsyncSession,
    ) -> FilterResult:
        """Run all stages and compute a final weighted score."""
        result = FilterResult(token_id=token.id)

        # ---- Stage 1: Instant reject ----
        s1 = self._stage1_instant_reject(token)
        result.stage_results.append(s1)
        result.stage_reached = 1
        if not s1.passed:
            result.rejected = True
            result.rejection_reason = s1.reason
            result.final_score = 0.0
            await self._persist_score(token, result, session)
            return result

        # ---- Stage 2: Early metrics (DexScreener) ----
        metrics = await self._fetch_dex_metrics(token)
        s2 = self._stage2_early_metrics(token, metrics)
        result.stage_results.append(s2)
        result.stage_reached = 2

        # ---- Stage 3: Smart money ----
        s3 = self._stage3_smart_money(metrics)
        result.stage_results.append(s3)
        result.stage_reached = 3

        # ---- Stage 4: Context quality ----
        ctx = await session.get(TokenContext, token.id) if token.context is None else token.context
        s4 = self._stage4_context_quality(ctx)
        result.stage_results.append(s4)
        result.stage_reached = 4

        # ---- Weighted score ----
        result.final_score = self._compute_weighted_score(s2, s3, s4)

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
        )
        return result

    # ------------------------------------------------------------------
    # Stage implementations
    # ------------------------------------------------------------------

    def _stage1_instant_reject(self, token: Token) -> StageResult:
        """Hard-reject tokens that are obvious trash."""
        name_lower = (token.name or "").lower()
        symbol_lower = (token.symbol or "").lower()

        # No pool → can't trade
        if not token.pool_address:
            return StageResult(passed=False, score=0.0, reason="No pool address")

        # Scam keyword in name / symbol
        for kw in self.cfg.scam_keywords:
            if kw in name_lower or kw in symbol_lower:
                return StageResult(
                    passed=False, score=0.0, reason=f"Scam keyword: {kw}",
                )

        return StageResult(passed=True, score=1.0)

    def _stage2_early_metrics(
        self, token: Token, metrics: Optional[TokenMetrics],
    ) -> StageResult:
        """Score based on early trading activity from DexScreener."""
        if metrics is None:
            # No data yet — neutral score, don't reject
            return StageResult(passed=True, score=0.3, reason="No DEX data yet")

        score = 0.0
        reasons: list[str] = []

        # Market cap
        if metrics.market_cap_usd and metrics.market_cap_usd >= self.cfg.min_mcap_usd:
            score += 0.25
        else:
            reasons.append("low mcap")

        # Liquidity
        if metrics.liquidity_usd and metrics.liquidity_usd >= self.cfg.min_pool_liquidity_usd:
            score += 0.25
        else:
            reasons.append("low liquidity")

        # 5-min volume
        if metrics.volume_5m_usd and metrics.volume_5m_usd >= self.cfg.min_volume_5m_usd:
            score += 0.25
        else:
            reasons.append("low volume")

        # Buy/sell ratio
        buys = metrics.buys_5m or 0
        sells = metrics.sells_5m or 0
        total = buys + sells
        if total > 0:
            ratio = buys / total
            if ratio >= self.cfg.min_buy_sell_ratio:
                score += 0.25
            else:
                reasons.append(f"buy ratio {ratio:.2f}")
        else:
            reasons.append("no trades")

        # Holder count bonus
        if metrics.holder_count and metrics.holder_count >= self.cfg.min_holders:
            score = min(score + 0.1, 1.0)

        return StageResult(
            passed=True,
            score=round(score, 3),
            reason="; ".join(reasons) if reasons else "metrics ok",
        )

    def _stage3_smart_money(self, metrics: Optional[TokenMetrics]) -> StageResult:
        """Score based on smart money wallet presence."""
        if not self._smart_wallets:
            return StageResult(passed=True, score=0.0, reason="No smart wallet list")

        sm_count = metrics.smart_money_holders if metrics else 0
        if sm_count and sm_count > 0:
            # Scale: 1 wallet = 0.5, 2 = 0.75, 3+ = 1.0
            score = min(0.25 + 0.25 * sm_count, 1.0)
            return StageResult(
                passed=True,
                score=round(score, 3),
                reason=f"{sm_count} smart wallets",
            )
        return StageResult(passed=True, score=0.0, reason="No smart money detected")

    def _stage4_context_quality(self, ctx: Optional[TokenContext]) -> StageResult:
        """Score based on whether we could trace the token's origin."""
        if ctx is None or ctx.resolution_strategy == "unresolved":
            if self.cfg.require_origin_url:
                return StageResult(
                    passed=False, score=0.0, reason="Origin required but unresolved",
                )
            return StageResult(passed=True, score=0.1, reason="No origin found")

        score = 0.3  # Base: we have *something*

        if ctx.origin_url:
            score += 0.3
        if ctx.origin_platform in ("x", "farcaster"):
            score += 0.1
        if ctx.origin_text:
            score += 0.15
        if ctx.project_idea:
            score += 0.15

        return StageResult(
            passed=True,
            score=round(min(score, 1.0), 3),
            reason=f"strategy={ctx.resolution_strategy}",
        )

    # ------------------------------------------------------------------
    # Score aggregation
    # ------------------------------------------------------------------

    def _compute_weighted_score(
        self,
        s2: StageResult,
        s3: StageResult,
        s4: StageResult,
    ) -> float:
        """Weighted combination of stages 2–4 (stage 1 is pass/fail gate)."""
        w_metrics = 1.0
        w_smart = self.cfg.smart_money_weight
        w_context = self.cfg.context_quality_weight

        total_weight = w_metrics + w_smart + w_context
        raw = (
            s2.score * w_metrics
            + s3.score * w_smart
            + s4.score * w_context
        ) / total_weight

        return round(raw, 4)

    # ------------------------------------------------------------------
    # DexScreener integration
    # ------------------------------------------------------------------

    async def _fetch_dex_metrics(self, token: Token) -> Optional[TokenMetrics]:
        """Fetch latest metrics from DexScreener API."""
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

        # Use the first (highest liquidity) pair
        pair = pairs[0]
        txns_5m = pair.get("txns", {}).get("m5", {})
        txns_1h = pair.get("txns", {}).get("h1", {})
        volume = pair.get("volume", {})

        return TokenMetrics(
            token_id=token.id,
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
