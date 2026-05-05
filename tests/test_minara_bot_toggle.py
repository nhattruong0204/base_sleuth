"""Tests for Telegram Minara runtime controls."""

from __future__ import annotations

from types import SimpleNamespace

from clanker_tracker.bot_commands import (
    _build_minara_text,
    _minara_ready,
    _toggle_minara,
)
from clanker_tracker.config import AppConfig, MinaraConfig


def tracker_with_minara(**kwargs):
    return SimpleNamespace(cfg=AppConfig(minara=MinaraConfig(**kwargs)))


def test_minara_ready_api_key():
    assert _minara_ready(tracker_with_minara(auth_method="api_key", api_key="sk-test")) is True
    assert _minara_ready(tracker_with_minara(auth_method="api_key", api_key=None)) is False


def test_minara_ready_x402():
    tracker = tracker_with_minara(
        auth_method="x402",
        x402_evm_private_key="0x" + "1" * 64,
    )

    assert _minara_ready(tracker) is True
    assert _minara_ready(tracker_with_minara(auth_method="x402")) is False


def test_toggle_enables_when_credentials_present():
    tracker = tracker_with_minara(enabled=False, auth_method="api_key", api_key="sk-test")

    text = _toggle_minara(tracker)

    assert tracker.cfg.minara.enabled is True
    assert "Status: ✅ ON" in text


def test_toggle_disables_when_enabled():
    tracker = tracker_with_minara(enabled=True, auth_method="api_key", api_key="sk-test")

    text = _toggle_minara(tracker)

    assert tracker.cfg.minara.enabled is False
    assert "Status: ❌ OFF" in text


def test_toggle_refuses_missing_credentials():
    tracker = tracker_with_minara(enabled=False, auth_method="api_key", api_key=None)

    text = _toggle_minara(tracker)

    assert tracker.cfg.minara.enabled is False
    assert "Cannot enable Minara yet" in text
    assert "MINARA_API_KEY" in text


def test_minara_text_documents_new_token_scope():
    tracker = tracker_with_minara(enabled=True, auth_method="api_key", api_key="sk-test")

    text = _build_minara_text(tracker)

    assert "New Token Alert only" in text
    assert "runtime-only" in text
