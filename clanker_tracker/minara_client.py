"""Minara Agent API client for pre-alert token thesis.

Docs: https://minara.ai/docs/ecosystem/agent-api/api-reference/api-reference-api-key
Pay-as-you-go docs: https://minara.ai/docs/ecosystem/agent-api/getting-started-by-x402
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Optional

import httpx
import structlog

from .config import AppConfig, MinaraConfig
from .filters import FilterResult
from .models import Token, TokenContext

logger = structlog.get_logger(__name__)


@dataclass
class MinaraDecision:
    """Parsed Minara analysis for a token alert."""

    decision: str = "UNKNOWN"
    confidence: Optional[int] = None
    thesis: str = ""
    risks: list[str] = field(default_factory=list)
    time_horizon: str = ""
    invalidation: str = ""
    raw_content: str = ""


class MinaraClient:
    """Small wrapper around Minara's Developer Chat endpoint."""

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg: MinaraConfig = config.minara
        self._http = http

    @property
    def enabled(self) -> bool:
        if not self.cfg.enabled:
            return False
        if self.cfg.auth_method == "api_key":
            return bool(self.cfg.api_key)
        if self.cfg.auth_method == "x402":
            return bool(self.cfg.x402_evm_private_key)
        return False

    async def analyze_alert(
        self,
        token: Token,
        ctx: Optional[TokenContext],
        result: FilterResult,
        *,
        dex: dict | None = None,
        nansen_buys: list[dict] | None = None,
    ) -> MinaraDecision | None:
        """Ask Minara for a compact BUY/WATCH/PASS thesis."""
        if not self.enabled:
            return None

        try:
            prompt = self._build_prompt(
                token, ctx, result, dex=dex, nansen_buys=nansen_buys,
            )
            if self.cfg.auth_method == "x402":
                content = await self._chat_with_x402(prompt)
            else:
                content = await self._chat_with_api_key(prompt)

            decision = self.parse_content(content)
            logger.info(
                "minara.analysis",
                token=token.symbol,
                decision=decision.decision,
                confidence=decision.confidence,
                auth_method=self.cfg.auth_method,
            )
            return decision
        except Exception as exc:
            logger.warning(
                "minara.analysis_failed",
                token=token.symbol,
                auth_method=self.cfg.auth_method,
                error=str(exc),
            )
            return None

    async def _chat_with_api_key(self, prompt: str) -> str:
        payload = {
            "mode": self.cfg.mode,
            "stream": False,
            "message": {
                "role": "user",
                "content": prompt,
            },
        }
        headers = {
            "Authorization": f"Bearer {self.cfg.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.cfg.base_url.rstrip('/')}/v1/developer/chat"
        resp = await self._http.post(
            url,
            json=payload,
            headers=headers,
            timeout=self.cfg.timeout_seconds,
        )
        resp.raise_for_status()
        return (resp.json() or {}).get("content") or ""

    async def _chat_with_x402(self, prompt: str) -> str:
        """Call Minara x402 chat endpoint.

        Imports are intentionally lazy so API-key deployments do not need
        x402/eth-account installed.
        """
        if not self.cfg.x402_evm_private_key:
            raise RuntimeError("missing EVM_PRIVATE_KEY for Minara x402 payments")

        try:
            from eth_account import Account
            from x402 import x402Client
            from x402.http.clients import x402HttpxClient
            from x402.mechanisms.evm import EthAccountSigner
            from x402.mechanisms.evm.exact.register import register_exact_evm_client
        except ImportError as exc:
            raise RuntimeError(
                'missing x402 dependencies; install with pip install "x402[httpx]" eth_account'
            ) from exc

        client = x402Client()
        account = Account.from_key(self.cfg.x402_evm_private_key)
        register_exact_evm_client(client, EthAccountSigner(account))

        async with x402HttpxClient(client) as http:
            resp = await http.post(
                self._x402_chat_url(),
                json={"userQuery": prompt},
                timeout=self.cfg.timeout_seconds,
            )
            await resp.aread()
            resp.raise_for_status()
            return (resp.json() or {}).get("content") or ""

    def _x402_chat_url(self) -> str:
        base = self.cfg.x402_base_url.rstrip("/")
        expert_suffix = "/expert" if self.cfg.mode == "expert" else ""
        if self.cfg.x402_chain == "base":
            return f"{base}/x402/chat{expert_suffix}"
        return f"{base}/x402/{self.cfg.x402_chain}/chat{expert_suffix}"

    def allows_alert(self, decision: MinaraDecision | None) -> bool:
        """Return whether a Minara decision passes configured alert gates."""
        if not self.cfg.gate_alerts:
            return True
        if decision is None:
            return self.cfg.fail_open

        allowed = {d.upper() for d in self.cfg.allowed_decisions}
        if decision.decision.upper() not in allowed:
            return False
        if decision.confidence is None:
            return True
        return decision.confidence >= self.cfg.min_confidence

    def parse_content(self, content: str) -> MinaraDecision:
        """Parse strict JSON when possible, falling back to raw text."""
        raw = (content or "").strip()
        data = self._extract_json(raw)
        if data is None:
            return MinaraDecision(raw_content=raw, thesis=self._truncate(raw))

        risks = data.get("risks") or []
        if isinstance(risks, str):
            risks = [risks]
        if not isinstance(risks, list):
            risks = []

        confidence = data.get("confidence")
        try:
            confidence = int(confidence) if confidence is not None else None
        except (TypeError, ValueError):
            confidence = None

        return MinaraDecision(
            decision=str(data.get("decision") or "UNKNOWN").upper(),
            confidence=confidence,
            thesis=self._truncate(str(data.get("thesis") or "")),
            risks=[str(r) for r in risks[:3]],
            time_horizon=str(data.get("time_horizon") or ""),
            invalidation=str(data.get("invalidation") or ""),
            raw_content=raw,
        )

    @staticmethod
    def _extract_json(content: str) -> dict | None:
        if not content:
            return None
        try:
            parsed = json.loads(content)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            pass

        match = re.search(r"\{.*\}", content, flags=re.DOTALL)
        if not match:
            return None
        try:
            parsed = json.loads(match.group(0))
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None

    def _truncate(self, text: str) -> str:
        text = " ".join((text or "").split())
        if len(text) <= self.cfg.max_thesis_chars:
            return text
        return text[: self.cfg.max_thesis_chars - 3].rstrip() + "..."

    def _build_prompt(
        self,
        token: Token,
        ctx: Optional[TokenContext],
        result: FilterResult,
        *,
        dex: dict | None = None,
        nansen_buys: list[dict] | None = None,
    ) -> str:
        stage_lines = []
        stage_names = [
            "Reject Gate", "DEX Metrics", "Momentum",
            "Smart Money", "Context",
        ]
        for i, sr in enumerate(result.stage_results):
            label = stage_names[i] if i < len(stage_names) else f"Stage {i + 1}"
            stage_lines.append(
                f"- {label}: passed={sr.passed}, score={sr.score:.2f}, reason={sr.reason}"
            )

        dex_line = "not available"
        if dex:
            dex_line = (
                f"mcap={dex.get('market_cap')}, fdv={dex.get('fdv')}, "
                f"liq={dex.get('liquidity')}, price={dex.get('price')}"
            )

        nansen_line = "none"
        if nansen_buys:
            total_usd = sum(b.get("usd_value") or 0 for b in nansen_buys)
            nansen_line = f"{len(nansen_buys)} wallet buys, total_usd={total_usd:.2f}"

        origin = getattr(ctx, "origin_url", None) or "unresolved"
        project_idea = getattr(ctx, "project_idea", None) or ""
        address = token.contract_address

        return (
            "You are Minara reviewing a Base-chain microcap token alert before it is sent "
            "to a Telegram alpha channel. Use your available Minara finance skills, "
            "including real-time market data, on-chain context, wallet/entity intelligence, "
            "social/sentiment awareness, and deep-research style cross-checks when available. "
            "Do not execute trades. Return JSON only with keys: "
            "decision (BUY, WATCH, or PASS), confidence (0-100), thesis "
            "(max 4 concise sentences), risks (array, max 3), time_horizon, "
            "and invalidation. Be skeptical of low liquidity, bot activity, "
            "wash trading, weak context, and scam/impersonation patterns.\n\n"
            f"Token: {token.name} (${token.symbol})\n"
            f"Address: {address}\n"
            f"Discovery source: {getattr(token, 'discovery_source', None)}\n"
            f"Champagne: {getattr(token, 'is_champagne', False)}\n"
            f"Breakout: {getattr(token, 'is_breakout', False)}\n"
            f"Launch platform: {getattr(token, 'launch_platform', None)}\n"
            f"Bot score: {result.final_score:.3f}\n"
            f"Live DEX: {dex_line}\n"
            f"Nansen smart money: {nansen_line}\n"
            f"Origin URL: {origin}\n"
            f"Project idea: {project_idea[:500]}\n\n"
            "Stage breakdown:\n"
            + "\n".join(stage_lines)
        )
