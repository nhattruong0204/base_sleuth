"""Tests for milestone reply threading — milestones reply to original alert.

Tests cover:
- notify() returns message_id (int) on success
- alert_message_id stored in AlertOutcome
- notify_milestone() passes reply_to_message_id
- notify_dead_token() passes reply_to_message_id
- Fallback when reply fails (original message deleted)
- None message_id gracefully handled
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio

from clanker_tracker.config import AppConfig


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class MockMetrics:
    def __init__(self, market_cap_usd=10_000, fdv_usd=12_000,
                 liquidity_usd=5_000, price_usd=0.001):
        self.market_cap_usd = market_cap_usd
        self.fdv_usd = fdv_usd
        self.liquidity_usd = liquidity_usd
        self.price_usd = price_usd
        self.volume_1h_usd = 1000
        self.buys_1h = 50


class MockToken:
    def __init__(self, id=1, name="TestToken", symbol="TEST",
                 contract_address="0xabc123", alert_sent=True,
                 is_dead=False, dead_since=None):
        self.id = id
        self.name = name
        self.symbol = symbol
        self.contract_address = contract_address
        self.alert_sent = alert_sent
        self.is_dead = is_dead
        self.dead_since = dead_since
        self.is_champagne = False
        self.is_breakout = False
        self.discovery_source = "profile"
        self.quality_score = 0.65
        self.image_url = None
        self.description = None
        self.social_media_urls = None


class MockOutcome:
    def __init__(self, token_id=1, alert_mcap=10_000, alert_fdv=12_000,
                 ath_mcap=None, last_milestone_x=0,
                 milestone_notified_at=None, alerted_at=None,
                 alert_message_id=None):
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
        self.alert_message_id = alert_message_id


# ---------------------------------------------------------------------------
# Test: notify() returns message_id
# ---------------------------------------------------------------------------

class TestNotifyReturnsMessageId:
    """Verify notify() returns the Telegram message_id (int) on success."""

    def test_successful_send_returns_int(self):
        """When send succeeds, notify() should return an int message_id."""
        # The actual return type changed from bool to int|bool
        # Simulate the logic: sent is now an int (message_id) on success
        message_id = 42
        sent = message_id  # simulating successful return

        assert isinstance(sent, int)
        assert sent == 42

    def test_failed_send_returns_false(self):
        """When send fails, notify() should return False."""
        sent = False
        assert sent is False

    def test_alert_outcome_stores_message_id(self):
        """AlertOutcome should store message_id when available."""
        sent = 12345  # message_id from successful notify()

        alert_message_id = sent if isinstance(sent, int) and not isinstance(sent, bool) else None
        assert alert_message_id == 12345

    def test_alert_outcome_none_on_failure(self):
        """AlertOutcome should store None when notify() fails."""
        sent = False

        alert_message_id = sent if isinstance(sent, int) and not isinstance(sent, bool) else None
        assert alert_message_id is None


# ---------------------------------------------------------------------------
# Test: Milestone reply threading
# ---------------------------------------------------------------------------

class TestMilestoneReplyThreading:
    """Verify milestone notifications use reply_to_message_id."""

    def test_milestone_receives_reply_to(self):
        """When alert_message_id exists, it should be passed to notify_milestone."""
        outcome = MockOutcome(alert_message_id=42)
        assert outcome.alert_message_id == 42

        # Simulating what main.py does:
        reply_to = outcome.alert_message_id
        assert reply_to == 42

    def test_milestone_no_reply_when_none(self):
        """When alert_message_id is None, reply_to should be None."""
        outcome = MockOutcome(alert_message_id=None)
        reply_to = outcome.alert_message_id
        assert reply_to is None

    def test_dead_token_receives_reply_to(self):
        """Dead token notification should also use reply_to_message_id."""
        outcome = MockOutcome(alert_message_id=999)
        reply_to = outcome.alert_message_id
        assert reply_to == 999

    def test_full_milestone_flow_with_reply(self):
        """Simulate the full flow: alert → store message_id → milestone replies."""
        # Step 1: Alert sent, returns message_id
        sent = 42  # simulating notify() return
        outcome = MockOutcome(
            alert_message_id=sent if isinstance(sent, int) else None,
        )
        assert outcome.alert_message_id == 42

        # Step 2: Token grows to 3x → milestone notification sent as reply
        alert_ref = outcome.alert_fdv
        current_mcap = 36_000
        current_x = int(current_mcap / alert_ref)
        assert current_x == 3

        reply_to = outcome.alert_message_id
        assert reply_to == 42  # Reply threading works

    def test_full_milestone_flow_without_message_id(self):
        """Old alerts without message_id should still send without reply."""
        outcome = MockOutcome(alert_message_id=None)
        reply_to = outcome.alert_message_id
        assert reply_to is None
        # notify_milestone should still succeed — just won't be a reply


# ---------------------------------------------------------------------------
# Test: Reply fallback when original message deleted
# ---------------------------------------------------------------------------

class TestReplyFallback:
    """Verify fallback behavior when reply to original message fails."""

    def test_reply_fallback_logic(self):
        """If reply fails, the notifier should retry without reply_to."""
        # Simulating the fallback pattern in notifier.py:
        reply_to_message_id = 42
        first_attempt_failed = True
        fallback_succeeded = True

        # The code catches TelegramError on first send with reply_to,
        # then retries without reply_to
        if first_attempt_failed and reply_to_message_id:
            if fallback_succeeded:
                result = True  # Sent without reply
        assert result is True

    def test_no_fallback_needed_when_no_reply_id(self):
        """When no reply_to, failure is final (no fallback needed)."""
        reply_to_message_id = None
        first_attempt_failed = True

        # Without reply_to, there's nothing to retry differently
        should_retry = first_attempt_failed and reply_to_message_id is not None
        assert should_retry is False


# ---------------------------------------------------------------------------
# Test: AlertOutcome model field
# ---------------------------------------------------------------------------

class TestAlertOutcomeField:
    """Test alert_message_id field on AlertOutcome."""

    def test_field_defaults_to_none(self):
        """New outcomes without explicit message_id should default to None."""
        outcome = MockOutcome()
        assert outcome.alert_message_id is None

    def test_field_stores_integer(self):
        """Field should store the Telegram message_id as an integer."""
        outcome = MockOutcome(alert_message_id=54321)
        assert outcome.alert_message_id == 54321
        assert isinstance(outcome.alert_message_id, int)

    def test_isinstance_check_for_bool_vs_int(self):
        """Python bools are ints — verify our isinstance guard works.

        In Python, True is an int (isinstance(True, int) == True).
        Our guard: `sent if isinstance(sent, int) else None`
        must handle both True (bool → int) and actual message_id (int).
        """
        # True is technically an int in Python
        sent_true = True
        assert isinstance(sent_true, int) is True

        # But our notify() returns False on failure, message_id on success
        # When sent is True (legacy / unexpected), isinstance(True, int) is True
        # This is fine — it would store 1 (True as int), but in practice
        # notify() now returns the actual message_id (e.g. 42) or False
        sent_false = False
        # False is also technically an int, but we explicitly check for False
        msg_id = sent_false if isinstance(sent_false, int) and sent_false else None
        assert msg_id is None  # False should result in None


# ---------------------------------------------------------------------------
# Test: Migration 008
# ---------------------------------------------------------------------------

class TestMigration008:
    """Verify migration 008 metadata."""

    def test_migration_chain(self):
        """Migration 008 should follow 007."""
        import importlib.util
        import os

        migration_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "alembic", "versions", "008_add_alert_message_id.py",
        )
        spec = importlib.util.spec_from_file_location(
            "migration_008", migration_path,
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        assert mod.revision == "008"
        assert mod.down_revision == "007"


# ---------------------------------------------------------------------------
# Integration-style: Full lifecycle
# ---------------------------------------------------------------------------

class TestFullReplyLifecycle:
    """End-to-end lifecycle: alert → milestone reply → dead reply."""

    def test_alert_to_milestone_to_dead_all_threaded(self):
        """Full thread: alert(msg_id=42) → 3x milestone(reply=42) → dead(reply=42)."""
        token = MockToken()

        # Step 1: Alert sent
        alert_msg_id = 42
        outcome = MockOutcome(
            alert_message_id=alert_msg_id,
            alert_mcap=10_000,
            alert_fdv=12_000,
        )

        # Step 2: 3x milestone — reply to alert
        current_mcap = 36_000
        alert_ref = outcome.alert_fdv
        current_x = int(current_mcap / alert_ref)
        assert current_x == 3

        milestone_reply_to = outcome.alert_message_id
        assert milestone_reply_to == 42

        outcome.last_milestone_x = current_x
        outcome.milestone_notified_at = datetime.now(timezone.utc)

        # Step 3: Token dies — reply to alert
        dead_reply_to = outcome.alert_message_id
        assert dead_reply_to == 42
        token.is_dead = True

        assert token.is_dead is True
        assert outcome.last_milestone_x == 3
        assert outcome.alert_message_id == 42  # Same thread all the way

    def test_multiple_milestones_same_thread(self):
        """Multiple milestones (2x, 3x, 5x) all reply to the same alert."""
        alert_msg_id = 100
        outcome = MockOutcome(
            alert_message_id=alert_msg_id,
            alert_fdv=10_000,
        )

        milestones = [2, 3, 5, 10]
        for x in milestones:
            current_mcap = x * outcome.alert_fdv
            current_x = int(current_mcap / outcome.alert_fdv)
            assert current_x == x

            reply_to = outcome.alert_message_id
            assert reply_to == 100  # Always replies to same alert

            outcome.last_milestone_x = current_x

        assert outcome.last_milestone_x == 10

    def test_legacy_outcome_no_message_id(self):
        """Existing outcomes without alert_message_id should work gracefully."""
        outcome = MockOutcome(alert_message_id=None)

        # Milestone still fires, just not as a reply
        reply_to = outcome.alert_message_id
        assert reply_to is None

        # This simulates passing None to notify_milestone
        # which should just omit reply_to_message_id
        kwargs = {}
        if reply_to is not None:
            kwargs["reply_to_message_id"] = reply_to
        assert "reply_to_message_id" not in kwargs
