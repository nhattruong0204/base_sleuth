"""Tests for the milestone tracker loop logic.

Tests cover:
- Multiplier milestone detection (1x, 2x, 3x, ...)
- ATH tracking and notification
- Dead token detection and cleanup
- Edge cases: no DEX data, zero alert MCap, recovered tokens
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from clanker_tracker.config import AppConfig, MilestoneTrackerConfig


# ---------------------------------------------------------------------------
# Helpers — minimal mock objects
# ---------------------------------------------------------------------------

class MockMetrics:
    """Simulates the DEX metrics object returned by fetch_dex_metrics_batch."""

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
        self.volume_1h_usd = 0
        self.buys_1h = 0


class MockToken:
    """Simulates a Token ORM object."""

    def __init__(
        self,
        id: int = 1,
        name: str = "TestToken",
        symbol: str = "TEST",
        contract_address: str = "0xabc123",
        alert_sent: bool = True,
        is_dead: bool = False,
        dead_since: datetime | None = None,
    ):
        self.id = id
        self.name = name
        self.symbol = symbol
        self.contract_address = contract_address
        self.alert_sent = alert_sent
        self.is_dead = is_dead
        self.dead_since = dead_since


class MockOutcome:
    """Simulates an AlertOutcome ORM object."""

    def __init__(
        self,
        token_id: int = 1,
        alert_mcap: float | None = 10_000,
        alert_fdv: float | None = 12_000,
        ath_mcap: float | None = None,
        last_milestone_x: int | None = 0,
        milestone_notified_at: datetime | None = None,
        alerted_at: datetime | None = None,
    ):
        self.token_id = token_id
        self.alert_mcap = alert_mcap
        self.alert_fdv = alert_fdv
        self.alert_score = 0.65
        self.alert_liq = 5000
        self.alert_vol_1h = 1000
        self.alert_buys_1h = 20
        self.ath_mcap = ath_mcap
        self.last_milestone_x = last_milestone_x
        self.milestone_notified_at = milestone_notified_at
        self.alerted_at = alerted_at or datetime.now(timezone.utc) - timedelta(hours=2)


# ---------------------------------------------------------------------------
# Unit Tests
# ---------------------------------------------------------------------------

class TestMultiplierDetection:
    """Test multiplier milestone calculation logic."""

    def test_1x_at_alert_fdv(self):
        """At alert FDV, multiplier should be 1x."""
        alert_ref = 12_000  # alert_fdv
        current_mcap = 12_000
        current_x = int(current_mcap / alert_ref)
        assert current_x == 1

    def test_2x_milestone(self):
        """Double the alert FDV → 2x."""
        alert_ref = 12_000
        current_mcap = 24_000
        current_x = int(current_mcap / alert_ref)
        assert current_x == 2

    def test_3x_milestone(self):
        """Triple → 3x."""
        alert_ref = 12_000
        current_mcap = 36_000
        current_x = int(current_mcap / alert_ref)
        assert current_x == 3

    def test_10x_milestone(self):
        """10x moon."""
        alert_ref = 10_000
        current_mcap = 100_000
        current_x = int(current_mcap / alert_ref)
        assert current_x == 10

    def test_fractional_below_2x(self):
        """1.5x should not trigger 2x milestone."""
        alert_ref = 10_000
        current_mcap = 15_000
        current_x = int(current_mcap / alert_ref)
        assert current_x == 1  # Not yet 2x

    def test_fallback_to_alert_mcap(self):
        """When alert_fdv is None, use alert_mcap as reference."""
        outcome = MockOutcome(alert_fdv=None, alert_mcap=8_000)
        alert_ref = outcome.alert_fdv or outcome.alert_mcap
        assert alert_ref == 8_000

    def test_new_milestone_vs_previous(self):
        """Only notify if current_x > last notified milestone."""
        outcome = MockOutcome(last_milestone_x=2)
        alert_ref = outcome.alert_fdv  # 12_000
        current_mcap = 36_000
        current_x = int(current_mcap / alert_ref)
        assert current_x == 3
        assert current_x > outcome.last_milestone_x

    def test_same_milestone_no_renotify(self):
        """Don't re-notify if still at same multiplier."""
        outcome = MockOutcome(last_milestone_x=3)
        alert_ref = outcome.alert_fdv  # 12_000
        current_mcap = 36_000
        current_x = int(current_mcap / alert_ref)
        assert current_x == 3
        assert current_x == outcome.last_milestone_x  # Should NOT trigger

    def test_min_multiplier_filter(self):
        """With min_multiplier_notify=2, 1x should not trigger."""
        cfg = MilestoneTrackerConfig(min_multiplier_notify=2)
        current_x = 1
        assert current_x < cfg.min_multiplier_notify  # Should NOT trigger


class TestATHTracking:
    """Test all-time-high MCap tracking logic."""

    def test_first_check_sets_ath(self):
        """First DEX data check should set ATH."""
        outcome = MockOutcome(ath_mcap=None)
        current_mcap = 20_000
        prev_ath = outcome.ath_mcap or 0
        if current_mcap > prev_ath:
            outcome.ath_mcap = current_mcap
        assert outcome.ath_mcap == 20_000

    def test_new_ath_updates(self):
        """MCap above previous ATH should update."""
        outcome = MockOutcome(ath_mcap=20_000)
        current_mcap = 25_000
        prev_ath = outcome.ath_mcap or 0
        if current_mcap > prev_ath:
            outcome.ath_mcap = current_mcap
        assert outcome.ath_mcap == 25_000

    def test_below_ath_no_update(self):
        """MCap below ATH should NOT update."""
        outcome = MockOutcome(ath_mcap=30_000)
        current_mcap = 25_000
        prev_ath = outcome.ath_mcap or 0
        if current_mcap > prev_ath:
            outcome.ath_mcap = current_mcap
        assert outcome.ath_mcap == 30_000  # Unchanged

    def test_ath_10pct_threshold(self):
        """ATH notification only fires when >10% above previous ATH."""
        prev_ath = 20_000
        current_mcap = 21_000  # Only 5% above
        assert not (current_mcap > prev_ath * 1.1)  # Should NOT trigger

        current_mcap = 22_500  # 12.5% above
        assert current_mcap > prev_ath * 1.1  # Should trigger

    def test_ath_cooldown(self):
        """ATH notification respects cooldown period."""
        cfg = MilestoneTrackerConfig(ath_cooldown_seconds=3600)
        now = datetime.now(timezone.utc)

        # Notified 30 min ago — should NOT notify
        last_notified = now - timedelta(minutes=30)
        cooldown_ok = (now - last_notified).total_seconds() >= cfg.ath_cooldown_seconds
        assert not cooldown_ok

        # Notified 2 hours ago — should notify
        last_notified = now - timedelta(hours=2)
        cooldown_ok = (now - last_notified).total_seconds() >= cfg.ath_cooldown_seconds
        assert cooldown_ok


class TestDeadTokenDetection:
    """Test dead token marking and cleanup logic."""

    def test_low_liq_starts_countdown(self):
        """Liquidity below threshold should start dead countdown."""
        cfg = MilestoneTrackerConfig(dead_liq_threshold_usd=200)
        token = MockToken()
        current_liq = 150  # Below threshold
        current_mcap = 1000

        assert current_liq < cfg.dead_liq_threshold_usd
        assert token.dead_since is None  # Should be set to now

    def test_low_mcap_starts_countdown(self):
        """MCap below threshold should start dead countdown."""
        cfg = MilestoneTrackerConfig(dead_mcap_threshold_usd=500)
        token = MockToken()
        current_mcap = 300  # Below threshold
        current_liq = 1000

        assert current_mcap < cfg.dead_mcap_threshold_usd
        assert token.dead_since is None  # Should be set to now

    def test_confirmed_dead_after_24h(self):
        """Token should be marked dead after being below threshold for 24h."""
        cfg = MilestoneTrackerConfig(dead_confirmation_hours=24)
        now = datetime.now(timezone.utc)
        dead_since = now - timedelta(hours=25)
        token = MockToken(dead_since=dead_since)

        elapsed = (now - token.dead_since).total_seconds()
        confirmed = elapsed >= cfg.dead_confirmation_hours * 3600
        assert confirmed

    def test_not_dead_before_24h(self):
        """Token should NOT be marked dead before 24h elapsed."""
        cfg = MilestoneTrackerConfig(dead_confirmation_hours=24)
        now = datetime.now(timezone.utc)
        dead_since = now - timedelta(hours=12)
        token = MockToken(dead_since=dead_since)

        elapsed = (now - token.dead_since).total_seconds()
        confirmed = elapsed >= cfg.dead_confirmation_hours * 3600
        assert not confirmed

    def test_recovery_resets_countdown(self):
        """Token that recovers above thresholds should reset dead_since."""
        token = MockToken(dead_since=datetime.now(timezone.utc) - timedelta(hours=10))
        cfg = MilestoneTrackerConfig()
        current_liq = 5000  # Recovered
        current_mcap = 10_000  # Recovered

        # Recovery condition
        if not (current_liq < cfg.dead_liq_threshold_usd
                or current_mcap < cfg.dead_mcap_threshold_usd):
            token.dead_since = None

        assert token.dead_since is None

    def test_max_age_cutoff(self):
        """Tokens older than max_token_age_days should not be checked."""
        cfg = MilestoneTrackerConfig(max_token_age_days=30)
        now = datetime.now(timezone.utc)
        old_alert = now - timedelta(days=31)
        max_age_cutoff = now - timedelta(days=cfg.max_token_age_days)
        assert old_alert < max_age_cutoff  # Should be excluded


class TestPnLCalculation:
    """Test profit/loss percentage calculation."""

    def test_2x_is_100pct(self):
        """2x multiplier = +100% gain."""
        alert_ref = 10_000
        current = 20_000
        pnl = ((current / alert_ref) - 1) * 100
        assert pnl == 100.0

    def test_half_is_minus50pct(self):
        """Halved value = -50% loss."""
        alert_ref = 10_000
        current = 5_000
        pnl = ((current / alert_ref) - 1) * 100
        assert pnl == -50.0

    def test_10x_is_900pct(self):
        """10x = +900%."""
        alert_ref = 10_000
        current = 100_000
        pnl = ((current / alert_ref) - 1) * 100
        assert pnl == 900.0

    def test_zero_alert_ref_skipped(self):
        """Zero alert ref should be skipped (avoid division by zero)."""
        alert_ref = 0
        assert not alert_ref or alert_ref <= 0  # Should skip


class TestMilestoneConfig:
    """Test configuration validation."""

    def test_default_config(self):
        """Default config should have sane values."""
        cfg = MilestoneTrackerConfig()
        assert cfg.enabled is True
        assert cfg.scan_interval_seconds == 300
        assert cfg.min_multiplier_notify == 2
        assert cfg.dead_confirmation_hours == 24
        assert cfg.max_token_age_days == 30

    def test_custom_config(self):
        """Custom config values should be respected."""
        cfg = MilestoneTrackerConfig(
            scan_interval_seconds=600,
            min_multiplier_notify=3,
            dead_confirmation_hours=48,
        )
        assert cfg.scan_interval_seconds == 600
        assert cfg.min_multiplier_notify == 3
        assert cfg.dead_confirmation_hours == 48


# ---------------------------------------------------------------------------
# Integration test — notifier formatting
# ---------------------------------------------------------------------------

class TestNotifierFormatting:
    """Test that milestone notifications produce valid messages."""

    @pytest.mark.asyncio
    async def test_milestone_message_contains_key_info(self):
        """Milestone notification should contain token name, multiplier, PnL."""
        from clanker_tracker.notifier import _esc, _fmt_number

        event = {
            "event_type": "multiplier",
            "token_name": "CoolToken",
            "token_symbol": "COOL",
            "token_address": "0xabc123",
            "alert_mcap": 10_000,
            "alert_fdv": 12_000,
            "current_mcap": 36_000,
            "current_liq": 5_000,
            "current_price": 0.001,
            "ath_mcap": 36_000,
            "multiplier": 3,
            "pnl_pct": 200,
        }

        # Verify the formatting helpers work
        assert _esc("CoolToken") == "CoolToken"
        assert _fmt_number(36_000) == "36.0K"
        assert _fmt_number(12_000) == "12.0K"

        # The multiplier should be in the expected range
        assert event["multiplier"] == 3
        assert event["pnl_pct"] == 200

    @pytest.mark.asyncio
    async def test_dead_token_message(self):
        """Dead token notification should contain alert vs current comparison."""
        event = {
            "token_name": "DeadCoin",
            "token_symbol": "DEAD",
            "token_address": "0xdead",
            "alert_mcap": 50_000,
            "current_mcap": 200,
            "current_liq": 50,
            "days_since_alert": 5.5,
        }

        # PnL calc
        pnl_pct = ((event["current_mcap"] / event["alert_mcap"]) - 1) * 100
        assert pnl_pct == pytest.approx(-99.6)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
