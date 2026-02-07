"""
Multi-stage token quality filter.

Stage 1: Instant reject   — structural red flags (no liquidity, rug patterns, warnings)
Stage 2: Early metrics     — volume, holders, buy/sell ratio (async, checked over time)
Stage 3: Smart money       — known profitable wallets buying early
Stage 4: Context quality   — has real project origin? community engagement?

Each stage produces a partial score; final score is weighted combination.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import structlog

from .config import FilteringConfig
from .models import Token, TokenContext, TokenMetrics

logger = structlog.get_logger(__name__)


@dataclass
class FilterResult:
    """Outcome of the filtering pipeline for one token."""

    contract_address: str
    symbol: str

    # Stage results
    stage1_pass: bool = False
    stage1_reason: str = ""

    stage2_score: float = 0.0  # 0-100
    stage3_score: float = 0.0  # 0-100
    stage4_score: float = 0.0  # 0-100

    final_score: float = 0.0   # 0-100

    # Flags
    is_gem: bool = False
    rejection_reasons: list[str] = field(default_factory=list)


class TokenFilter:
    """Evaluates tokens through the multi-stage quality pipeline."""

    def __init__(self, config: FilteringConfig):
        self.config = config
        self._smart_money_wallets: set[str] = set()
        self._load_smart_money_wallets()

    def _load_smart_money_wallets(self):
        """Load known profitable wallet addresses from file."""
        path = Path(self.config.smart_money_wallets_file)
        if path.exists():
            with open(path) as f:
                self._smart_money_wallets = {
                    line.strip().lower()
                    for line in f
                    if line.strip() and not line.startswith("#")
                }
            logger.info("smart_money_loaded", count=len(self._smart_money_wallets))
        else:
            logger.warning("smart_money_file_missing", path=str(path))

    def stage1_instant_filter(self, token: Token) -> FilterResult:
        """
        Stage 1: Instant reject based on structural data.
        Called immediately when token is discovered.
        """
        result = FilterResult(
            contract_address=token.contract_address,
            symbol=token.symbol,
        )

        # Check for warning flags from Clanker
        if token.warnings:
            try:
                warnings = json.loads(token.warnings) if isinstance(token.warnings, str) else token.warnings
                if warnings:
                    result.rejection_reasons.append(f"clanker_warnings: {warnings}")
            except (json.JSONDecodeError, TypeError):
                pass

        # Minimum starting market cap
        if token.starting_mcap_eth is not None and token.starting_mcap_eth < self.config.min_starting_mcap_eth:
            result.rejection_reasons.append(
                f"low_mcap: {token.starting_mcap_eth} ETH < {self.config.min_starting_mcap_eth}"
            )

        # No pool = no tradeable token
        if not token.pool_address:
            result.rejection_reasons.append("no_pool_address")

        # Common scam patterns in name/symbol
        scam_keywords = [
            "test", "airdrop", "free", "claim", "giveaway",
            "elon", "trump",  # Often used in scam tokens
        ]
        name_lower = (token.name or "").lower()
        symbol_lower = (token.symbol or "").lower()
        for kw in scam_keywords:
            if kw in name_lower or kw in symbol_lower:
                result.rejection_reasons.append(f"scam_keyword: {kw}")
                break

        # Verdict
        result.stage1_pass = len(result.rejection_reasons) == 0

        if not result.stage1_pass:
            logger.debug(
                "stage1_rejected",
                symbol=token.symbol,
                reasons=result.rejection_reasons,
            )

        return result

    def stage2_early_metrics(
        self,
        result: FilterResult,
        metrics: Optional[TokenMetrics],
    ) -> FilterResult:
        """
        Stage 2: Score based on early trading metrics.
        Called after 5-30 min of trading data is available.
        """
        if not metrics:
            result.stage2_score = 0.0
            return result

        score = 0.0

        # Unique buyers
        if metrics.unique_buyers and metrics.unique_buyers >= self.config.min_unique_buyers_5min:
            score += 25.0
            # Bonus for high buyer count
            if metrics.unique_buyers >= self.config.min_unique_buyers_5min * 3:
                score += 10.0

        # Buy/sell ratio
        if metrics.buy_count and metrics.sell_count:
            ratio = metrics.buy_count / max(metrics.sell_count, 1)
            if ratio >= self.config.min_buy_sell_ratio:
                score += 20.0
            elif ratio >= 1.0:
                score += 10.0

        # Top holder concentration
        if metrics.top_holder_pct is not None:
            if metrics.top_holder_pct < self.config.max_top_holder_pct:
                score += 20.0
            elif metrics.top_holder_pct < self.config.max_top_holder_pct * 1.5:
                score += 10.0
            else:
                result.rejection_reasons.append(
                    f"whale_concentration: {metrics.top_holder_pct:.1f}%"
                )

        # Volume check
        # (would need ETH price conversion, simplified here)
        if metrics.volume_24h_usd and metrics.volume_24h_usd > 1000:
            score += 15.0

        # Holder growth (if we have multiple snapshots)
        if metrics.unique_holders and metrics.unique_holders > 20:
            score += 10.0

        result.stage2_score = min(score, 100.0)
        return result

    def stage3_smart_money(
        self,
        result: FilterResult,
        early_buyer_addresses: list[str],
    ) -> FilterResult:
        """
        Stage 3: Check if known smart money wallets are buying.
        """
        if not self._smart_money_wallets or not early_buyer_addresses:
            result.stage3_score = 0.0
            return result

        matches = [
            addr for addr in early_buyer_addresses
            if addr.lower() in self._smart_money_wallets
        ]

        if len(matches) >= self.config.smart_money_threshold:
            result.stage3_score = 100.0
            logger.info(
                "smart_money_detected",
                symbol=result.symbol,
                wallet_count=len(matches),
            )
        elif len(matches) >= 1:
            result.stage3_score = 50.0
        else:
            result.stage3_score = 0.0

        return result

    def stage4_context_quality(
        self,
        result: FilterResult,
        context: Optional[TokenContext],
        token: Token,
    ) -> FilterResult:
        """
        Stage 4: Score based on how much origin context we have.
        Tokens with clear project origins score higher.
        """
        if not context:
            result.stage4_score = 0.0
            return result

        score = 0.0

        # Has traceable origin
        if context.x_tweet_url or context.farcaster_cast_url:
            score += 30.0

        # Has project description
        if context.project_idea and len(context.project_idea) > 20:
            score += 25.0

        # Has identifiable author
        if context.x_author_username or context.farcaster_author:
            score += 20.0

        # Has tweet/cast text (we know what the request was)
        if context.x_tweet_text or context.farcaster_text:
            score += 15.0

        # Bankr origin (legitimate launch path)
        if token.is_bankr_origin:
            score += 10.0

        result.stage4_score = min(score, 100.0)
        return result

    def compute_final_score(self, result: FilterResult) -> FilterResult:
        """
        Weighted combination of all stage scores.

        Weights:
        - Stage 2 (metrics): 40% — most objective signal
        - Stage 3 (smart money): 30% — strong conviction signal
        - Stage 4 (context): 30% — helps filter noise
        """
        if not result.stage1_pass:
            result.final_score = 0.0
            result.is_gem = False
            return result

        context_weight = self.config.context_weight
        metrics_weight = 0.4
        smart_money_weight = 1.0 - metrics_weight - context_weight

        result.final_score = (
            result.stage2_score * metrics_weight
            + result.stage3_score * smart_money_weight
            + result.stage4_score * context_weight
        )

        # Clamp to 0-100
        result.final_score = max(0.0, min(100.0, result.final_score))

        logger.info(
            "token_scored",
            symbol=result.symbol,
            s1_pass=result.stage1_pass,
            s2=f"{result.stage2_score:.0f}",
            s3=f"{result.stage3_score:.0f}",
            s4=f"{result.stage4_score:.0f}",
            final=f"{result.final_score:.0f}",
        )

        return result
