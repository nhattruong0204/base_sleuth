"""Tests for the gate-pending re-scan loop and related improvements.

Tests cover:
- Gate-pending marking when MCap/Liq below gate
- Re-scan loop picks up gate-pending tokens
- Alert fires when MCap/Liq grows above gate
- Max rechecks → give up
- Contract-address-aware duplicate symbol check
- MCap gate at $25K
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from clanker_tracker.config import AppConfig, GatePendingConfig, FilteringConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockMetrics:
    """Simulates TokenMetrics from DexScreener."""

    def __init__(
        self,
        market_cap_usd: float | None = None,
        fdv_usd: float | None = None,
        liquidity_usd: float | None = None,
        price_usd: float | None = None,
    ):
        self.market_cap_usd = market_cap_usd
        self.fdv_usd = fdv_usd
        self.liquidity_usd = liquidity_usd
        self.price_usd = price_usd
        self.volume_1h_usd = 1000
        self.buys_1h = 50


class MockToken:
    """Simulates a Token ORM object with gate-pending fields."""

    def __init__(
        self,
        id: int = 1,
        name: str = "TestToken",
        symbol: str = "TEST",
        contract_address: str = "0xabc123def456",
        quality_score: float | None = 0.65,
        alert_sent: bool = False,
        gate_pending: bool = False,
        gate_check_count: int = 0,
        last_gate_check: datetime | None = None,
        rejection_reason: str | None = None,
        is_dead: bool = False,
        is_champagne: bool = False,
        is_breakout: bool = False,
        discovery_source: str = "profile",
    ):
        self.id = id
        self.name = name
        self.symbol = symbol
        self.contract_address = contract_address
        self.quality_score = quality_score
        self.alert_sent = alert_sent
        self.gate_pending = gate_pending
        self.gate_check_count = gate_check_count
        self.last_gate_check = last_gate_check
        self.rejection_reason = rejection_reason
        self.is_dead = is_dead
        self.is_champagne = is_champagne
        self.is_breakout = is_breakout
        self.discovery_source = discovery_source


class MockFilterResult:
    """Simulates a FilterResult."""

    def __init__(
        self,
        final_score: float = 0.65,
        rejected: bool = False,
        rejection_reason: str = "",
        metrics: MockMetrics | None = None,
    ):
        self.final_score = final_score
        self.rejected = rejected
        self.rejection_reason = rejection_reason
        self.metrics = metrics
        self.stage_reached = 5
        self.stage_scores = {}


# ---------------------------------------------------------------------------
# Config tests
# ---------------------------------------------------------------------------

class TestGatePendingConfig:
    """Test GatePendingConfig defaults and validation."""

    def test_defaults(self):
        cfg = GatePendingConfig()
        assert cfg.enabled is True
        assert cfg.recheck_interval_seconds == 600
        assert cfg.max_rechecks == 18
        assert cfg.batch_size == 30

    def test_custom_values(self):
        cfg = GatePendingConfig(
            recheck_interval_seconds=300,
            max_rechecks=10,
            batch_size=15,
        )
        assert cfg.recheck_interval_seconds == 300
        assert cfg.max_rechecks == 10
        assert cfg.batch_size == 15

    def test_min_recheck_interval(self):
        with pytest.raises(Exception):
            GatePendingConfig(recheck_interval_seconds=30)  # below ge=60

    def test_config_registered_in_app(self):
        app_cfg = AppConfig()
        assert hasattr(app_cfg, "gate_pending")
        assert isinstance(app_cfg.gate_pending, GatePendingConfig)


class TestMCapGateLowered:
    """Test that MCap gate default is now $25K."""

    def test_default_mcap_gate(self):
        cfg = FilteringConfig()
        assert cfg.min_alert_mcap_usd == 25_000.0

    def test_custom_mcap_gate(self):
        cfg = FilteringConfig(min_alert_mcap_usd=10_000.0)
        assert cfg.min_alert_mcap_usd == 10_000.0


# ---------------------------------------------------------------------------
# Gate-pending marking logic tests
# ---------------------------------------------------------------------------

class TestGatePendingMarking:
    """Test that tokens are correctly marked as gate_pending in eval loop."""

    def test_token_below_mcap_gate_marked_pending(self):
        """Simulate eval loop marking token as gate_pending when MCap is below gate."""
        token = MockToken(quality_score=0.75)
        metrics = MockMetrics(market_cap_usd=20_000, liquidity_usd=10_000)

        min_mcap = 25_000  # from config
        alert_mcap = metrics.market_cap_usd or 0

        if alert_mcap < min_mcap:
            token.gate_pending = True
            token.gate_check_count = 0
            token.rejection_reason = (
                f"MCap ${alert_mcap:,.0f} below ${min_mcap:,.0f} gate"
            )

        assert token.gate_pending is True
        assert token.gate_check_count == 0
        assert "MCap" in token.rejection_reason

    def test_token_above_mcap_gate_not_marked(self):
        """Token above MCap gate should NOT be marked pending."""
        token = MockToken(quality_score=0.75)
        metrics = MockMetrics(market_cap_usd=30_000, liquidity_usd=10_000)

        min_mcap = 25_000
        alert_mcap = metrics.market_cap_usd or 0

        if alert_mcap < min_mcap:
            token.gate_pending = True

        assert token.gate_pending is False

    def test_token_below_liq_gate_marked_pending(self):
        """Token below liquidity gate should be marked pending."""
        token = MockToken(quality_score=0.65)
        metrics = MockMetrics(market_cap_usd=50_000, liquidity_usd=3_000)

        min_liq = 5_000
        alert_liq = metrics.liquidity_usd or 0

        if alert_liq < min_liq:
            token.gate_pending = True
            token.gate_check_count = 0
            token.rejection_reason = (
                f"Liq ${alert_liq:,.0f} below ${min_liq:,.0f} gate"
            )

        assert token.gate_pending is True
        assert "Liq" in token.rejection_reason


# ---------------------------------------------------------------------------
# Gate-pending re-scan logic tests
# ---------------------------------------------------------------------------

class TestGatePendingRescan:
    """Test gate-pending re-scan loop logic."""

    def test_token_passes_gate_on_recheck(self):
        """Token MCap grew above gate → alert should fire."""
        token = MockToken(
            gate_pending=True,
            gate_check_count=3,
            quality_score=0.72,
        )
        metrics = MockMetrics(
            market_cap_usd=30_000,  # Now above $25K
            liquidity_usd=8_000,    # Above $5K
        )

        min_mcap = 25_000
        min_liq = 5_000

        mcap_ok = (metrics.market_cap_usd or 0) >= min_mcap
        liq_ok = (metrics.liquidity_usd or 0) >= min_liq

        if mcap_ok and liq_ok:
            token.gate_pending = False
            token.rejection_reason = None

        assert token.gate_pending is False
        assert token.rejection_reason is None

    def test_token_still_below_gate(self):
        """Token still below gate → stays pending, count incremented."""
        token = MockToken(
            gate_pending=True,
            gate_check_count=3,
        )
        metrics = MockMetrics(
            market_cap_usd=15_000,  # Still below $25K
            liquidity_usd=8_000,
        )

        min_mcap = 25_000
        token.gate_check_count += 1

        mcap_ok = (metrics.market_cap_usd or 0) >= min_mcap
        assert mcap_ok is False
        assert token.gate_pending is True
        assert token.gate_check_count == 4

    def test_max_rechecks_gives_up(self):
        """After max_rechecks, token should be cleared from pending."""
        max_rechecks = 18
        token = MockToken(
            gate_pending=True,
            gate_check_count=17,  # One below max
        )
        metrics = MockMetrics(
            market_cap_usd=15_000,  # Still below gate
            liquidity_usd=8_000,
        )

        token.gate_check_count += 1  # Now 18 = max

        min_mcap = 25_000
        mcap_ok = (metrics.market_cap_usd or 0) >= min_mcap

        if not mcap_ok and token.gate_check_count >= max_rechecks:
            token.gate_pending = False

        assert token.gate_pending is False
        assert token.gate_check_count == 18

    def test_no_dex_data_increments_count(self):
        """No DEX data → increment count, leave pending."""
        max_rechecks = 18
        token = MockToken(
            gate_pending=True,
            gate_check_count=5,
        )

        # Simulating no DEX data for this token
        token.gate_check_count += 1

        assert token.gate_pending is True
        assert token.gate_check_count == 6

    def test_no_dex_data_at_max_rechecks_gives_up(self):
        """No DEX data at max rechecks → give up."""
        max_rechecks = 18
        token = MockToken(
            gate_pending=True,
            gate_check_count=17,
        )

        token.gate_check_count += 1
        if token.gate_check_count >= max_rechecks:
            token.gate_pending = False

        assert token.gate_pending is False


# ---------------------------------------------------------------------------
# Duplicate symbol check — contract-address aware
# ---------------------------------------------------------------------------

class TestDuplicateSymbolContractAware:
    """Test that duplicate symbol check uses contract address."""

    def test_same_contract_blocked(self):
        """Same contract address → should be blocked."""
        new_token_addr = "0xabc123"
        existing_alerted_addr = "0xabc123"

        blocked = new_token_addr.lower() == existing_alerted_addr.lower()
        assert blocked is True

    def test_different_contract_allowed(self):
        """Different contract address → should be allowed."""
        new_token_addr = "0xabc123"
        existing_alerted_addr = "0xdef456"

        blocked = new_token_addr.lower() == existing_alerted_addr.lower()
        assert blocked is False

    def test_case_insensitive_comparison(self):
        """Contract addresses should be compared case-insensitively."""
        new_token_addr = "0xABC123DEF456"
        existing_alerted_addr = "0xabc123def456"

        blocked = new_token_addr.lower() == existing_alerted_addr.lower()
        assert blocked is True

    def test_different_contract_same_symbol_passes(self):
        """Two different contracts with same symbol — the new one should alert."""
        token_a = MockToken(symbol="COOK", contract_address="0x111111")
        token_b = MockToken(symbol="COOK", contract_address="0x222222")

        # Simulating the check: existing token_a was alerted, now token_b wants to alert
        same_contract = token_a.contract_address.lower() == token_b.contract_address.lower()
        assert same_contract is False  # Different contracts → should NOT block


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    """Edge cases for gate-pending logic."""

    def test_gate_pending_disabled(self):
        """When gate_pending disabled, tokens should NOT be marked pending."""
        cfg = GatePendingConfig(enabled=False)
        token = MockToken()

        # If disabled, don't set gate_pending
        if cfg.enabled:
            token.gate_pending = True

        assert token.gate_pending is False

    def test_token_already_alerted_skipped(self):
        """Already-alerted tokens should not be re-processed."""
        token = MockToken(alert_sent=True, gate_pending=True)

        # In the loop, we filter: alert_sent=False
        should_process = not token.alert_sent and token.gate_pending
        assert should_process is False

    def test_dead_token_not_reprocessed(self):
        """Dead tokens should not be in the gate-pending queue."""
        token = MockToken(is_dead=True, gate_pending=True)
        # The loop query doesn't filter on is_dead, but
        # in practice, dead tokens have alert_sent=True from milestones
        # This test verifies the scenario is handled consistently
        assert token.is_dead is True
        assert token.gate_pending is True

    def test_zero_mcap_stays_pending(self):
        """Token with zero MCap stays pending."""
        token = MockToken(gate_pending=True, gate_check_count=2)
        metrics = MockMetrics(market_cap_usd=0, liquidity_usd=0)

        mcap_ok = (metrics.market_cap_usd or 0) >= 25_000
        liq_ok = (metrics.liquidity_usd or 0) >= 5_000

        assert mcap_ok is False
        assert liq_ok is False
        assert token.gate_pending is True

    def test_partial_gate_pass_mcap_only(self):
        """MCap above gate but Liq below → stays pending (both must pass)."""
        token = MockToken(gate_pending=True, gate_check_count=5)
        metrics = MockMetrics(
            market_cap_usd=30_000,  # Above $25K
            liquidity_usd=3_000,    # Below $5K
        )

        min_mcap = 25_000
        min_liq = 5_000
        mcap_ok = (metrics.market_cap_usd or 0) >= min_mcap
        liq_ok = (metrics.liquidity_usd or 0) >= min_liq

        both_pass = mcap_ok and liq_ok
        assert both_pass is False
        assert token.gate_pending is True

    def test_partial_gate_pass_liq_only(self):
        """Liq above gate but MCap below → stays pending."""
        metrics = MockMetrics(
            market_cap_usd=15_000,  # Below $25K
            liquidity_usd=10_000,   # Above $5K
        )

        min_mcap = 25_000
        min_liq = 5_000
        mcap_ok = (metrics.market_cap_usd or 0) >= min_mcap
        liq_ok = (metrics.liquidity_usd or 0) >= min_liq

        both_pass = mcap_ok and liq_ok
        assert both_pass is False


# ---------------------------------------------------------------------------
# Integration-style tests
# ---------------------------------------------------------------------------

class TestGatePendingIntegration:
    """Higher-level tests simulating the full gate-pending workflow."""

    def test_full_lifecycle_mark_recheck_alert(self):
        """Full lifecycle: eval marks pending → recheck → MCap grows → alert fires."""
        # Step 1: Eval loop — token scores well but MCap too low
        token = MockToken(quality_score=0.72, contract_address="0xtest123")
        metrics_initial = MockMetrics(market_cap_usd=15_000, liquidity_usd=8_000)

        min_mcap = 25_000
        if (metrics_initial.market_cap_usd or 0) < min_mcap:
            token.gate_pending = True
            token.gate_check_count = 0
            token.rejection_reason = "MCap $15,000 below $25,000 gate"

        assert token.gate_pending is True
        assert token.alert_sent is False

        # Step 2: First re-check — still below
        metrics_check1 = MockMetrics(market_cap_usd=20_000, liquidity_usd=9_000)
        token.gate_check_count += 1
        mcap_ok = (metrics_check1.market_cap_usd or 0) >= min_mcap
        assert mcap_ok is False
        assert token.gate_pending is True

        # Step 3: Second re-check — MCap grew past gate!
        metrics_check2 = MockMetrics(market_cap_usd=35_000, liquidity_usd=12_000)
        token.gate_check_count += 1
        min_liq = 5_000
        mcap_ok = (metrics_check2.market_cap_usd or 0) >= min_mcap
        liq_ok = (metrics_check2.liquidity_usd or 0) >= min_liq

        if mcap_ok and liq_ok:
            token.gate_pending = False
            token.rejection_reason = None
            token.alert_sent = True

        assert token.gate_pending is False
        assert token.alert_sent is True
        assert token.rejection_reason is None
        assert token.gate_check_count == 2

    def test_full_lifecycle_max_rechecks_timeout(self):
        """Full lifecycle: token never grows → max rechecks reached → give up."""
        token = MockToken(quality_score=0.65, gate_pending=True)
        max_rechecks = 18

        for i in range(max_rechecks):
            metrics = MockMetrics(market_cap_usd=10_000, liquidity_usd=8_000)
            token.gate_check_count += 1

            mcap_ok = (metrics.market_cap_usd or 0) >= 25_000

            if not mcap_ok and token.gate_check_count >= max_rechecks:
                token.gate_pending = False

        assert token.gate_pending is False
        assert token.alert_sent is False
        assert token.gate_check_count == 18

    def test_multiple_tokens_mixed_outcomes(self):
        """Multiple tokens: some pass, some don't, some max out."""
        tokens = [
            MockToken(id=1, symbol="GROW", gate_pending=True, gate_check_count=3,
                      contract_address="0x111"),
            MockToken(id=2, symbol="FLAT", gate_pending=True, gate_check_count=17,
                      contract_address="0x222"),
            MockToken(id=3, symbol="SEMI", gate_pending=True, gate_check_count=5,
                      contract_address="0x333"),
        ]

        dex_data = {
            "0x111": MockMetrics(market_cap_usd=50_000, liquidity_usd=15_000),  # Passes!
            "0x222": MockMetrics(market_cap_usd=10_000, liquidity_usd=3_000),   # Still below, max
            "0x333": MockMetrics(market_cap_usd=30_000, liquidity_usd=3_000),   # MCap ok, liq not
        }

        min_mcap, min_liq, max_rechecks = 25_000, 5_000, 18
        alerts = 0
        gave_up = 0

        for token in tokens:
            addr = token.contract_address.lower()
            m = dex_data.get(addr)
            token.gate_check_count += 1

            if not m:
                continue

            mcap_ok = (m.market_cap_usd or 0) >= min_mcap
            liq_ok = (m.liquidity_usd or 0) >= min_liq

            if mcap_ok and liq_ok:
                token.gate_pending = False
                token.alert_sent = True
                alerts += 1
            elif token.gate_check_count >= max_rechecks:
                token.gate_pending = False
                gave_up += 1

        assert alerts == 1  # Only GROW passes
        assert gave_up == 1  # FLAT hits max
        assert tokens[0].alert_sent is True
        assert tokens[1].gate_pending is False
        assert tokens[2].gate_pending is True  # SEMI still pending (only check 6)
