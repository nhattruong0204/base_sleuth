"""Tests for Minara pre-alert thesis parsing and gating."""

from __future__ import annotations

from clanker_tracker.config import AppConfig, MinaraConfig
from clanker_tracker.minara_client import MinaraClient


class DummyHTTP:
    pass


def build_client(**kwargs) -> MinaraClient:
    cfg = AppConfig(minara=MinaraConfig(**kwargs))
    return MinaraClient(cfg, DummyHTTP())


def test_parse_strict_json_response():
    client = build_client()
    decision = client.parse_content(
        '{"decision":"BUY","confidence":82,"thesis":"Liquidity and momentum are strong.",'
        '"risks":["Low float","Fast reversal"],"time_horizon":"1-6h",'
        '"invalidation":"Liquidity drops below $5k"}'
    )

    assert decision.decision == "BUY"
    assert decision.confidence == 82
    assert decision.thesis == "Liquidity and momentum are strong."
    assert decision.risks == ["Low float", "Fast reversal"]
    assert decision.time_horizon == "1-6h"
    assert decision.invalidation == "Liquidity drops below $5k"


def test_parse_json_embedded_in_text():
    client = build_client()
    decision = client.parse_content(
        'Here is the result:\n{"decision":"WATCH","confidence":61,"thesis":"Wait for confirmation."}'
    )

    assert decision.decision == "WATCH"
    assert decision.confidence == 61
    assert decision.thesis == "Wait for confirmation."


def test_parse_plain_text_fallback():
    client = build_client(max_thesis_chars=120)
    decision = client.parse_content("No structured JSON, but this token needs caution.")

    assert decision.decision == "UNKNOWN"
    assert decision.confidence is None
    assert decision.thesis == "No structured JSON, but this token needs caution."


def test_gate_allows_configured_decisions_with_confidence():
    client = build_client(gate_alerts=True, min_confidence=60)
    decision = client.parse_content(
        '{"decision":"WATCH","confidence":60,"thesis":"Acceptable watchlist setup."}'
    )

    assert client.allows_alert(decision) is True


def test_gate_blocks_pass_decision():
    client = build_client(gate_alerts=True)
    decision = client.parse_content(
        '{"decision":"PASS","confidence":91,"thesis":"Likely wash trading."}'
    )

    assert client.allows_alert(decision) is False


def test_gate_respects_fail_open_for_missing_decision():
    assert build_client(gate_alerts=True, fail_open=True).allows_alert(None) is True
    assert build_client(gate_alerts=True, fail_open=False).allows_alert(None) is False


def test_enabled_api_key_requires_api_key():
    assert build_client(enabled=True, auth_method="api_key", api_key="sk-test").enabled is True
    assert build_client(enabled=True, auth_method="api_key", api_key=None).enabled is False


def test_enabled_x402_requires_evm_private_key():
    assert build_client(
        enabled=True,
        auth_method="x402",
        x402_evm_private_key="0x" + "1" * 64,
    ).enabled is True
    assert build_client(enabled=True, auth_method="x402", x402_evm_private_key=None).enabled is False


def test_x402_chat_url_base_fast_and_expert():
    fast = build_client(auth_method="x402", x402_chain="base", mode="fast")
    expert = build_client(auth_method="x402", x402_chain="base", mode="expert")

    assert fast._x402_chat_url() == "https://x402.minara.ai/x402/chat"
    assert expert._x402_chat_url() == "https://x402.minara.ai/x402/chat/expert"


def test_x402_chat_url_polygon():
    client = build_client(auth_method="x402", x402_chain="polygon", mode="fast")

    assert client._x402_chat_url() == "https://x402.minara.ai/x402/polygon/chat"
