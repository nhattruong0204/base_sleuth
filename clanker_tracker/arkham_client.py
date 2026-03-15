"""Arkham Intel API client for smart wallet tracking and on-chain intelligence.

Integrates with Arkham Intel to:
1. Fetch Base EVM wallets tagged 'fomo-user'
2. Analyze wallet performance (PnL via historical USD flows)
3. Monitor wallet swaps for buy signals
4. Detect conviction signals (tracked wallet buys token in our DB)
5. Token Holder Intelligence — identify WHO holds a token (funds, VCs, whales)
6. Deployer Profiling — identify WHO deployed a token contract (scammer? builder?)
7. Token Flow Intelligence — monitor inflows/outflows for dump detection
8. Portfolio Watch — detect new positions from smart wallets

API docs: https://intel.arkm.com/api/docs
Rate limits: 20 req/sec standard, 1 req/sec for heavy endpoints (/swaps, /transfers, /token/top_flow)
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .config import AppConfig, ArkhamConfig
from .models import SmartWallet, Token, WalletSwap

logger = structlog.get_logger(__name__)


class ArkhamClient:
    """Fetches and manages smart wallet data from Arkham Intel."""

    def __init__(self, config: AppConfig, http: httpx.AsyncClient) -> None:
        self.cfg: ArkhamConfig = config.arkham
        self._http = http
        self._headers: dict[str, str] = {}
        if self.cfg.api_key:
            self._headers["API-Key"] = self.cfg.api_key

    @property
    def enabled(self) -> bool:
        return self.cfg.enabled and bool(self.cfg.api_key)

    # ------------------------------------------------------------------
    # Wallet discovery — fetch fomo-tagged wallets from Arkham
    # ------------------------------------------------------------------

    async def fetch_fomo_wallets(self) -> list[dict]:
        """Fetch Base wallets tagged 'fomo-user' from Arkham Intel.

        Uses /intelligence/addresses/updates with tagId filter.
        Falls back to manual wallet list if API unavailable.

        Returns list of dicts with keys: address, entity, label, metrics.
        """
        if not self.enabled:
            logger.warning("arkham.disabled", reason="no api_key or not enabled")
            return []

        wallets: list[dict] = []
        page_token: Optional[str] = None
        max_pages = 20  # Safety cap (100 wallets per page × 20 = 2000)

        for page in range(max_pages):
            try:
                params: dict = {
                    "tagId": self.cfg.tag_id,
                    "chain": "base",
                    "status": "all",
                    "orderBy": "balance",
                    "limit": 100,
                    "includeTags": "true",
                }
                if page_token:
                    params["pageToken"] = page_token

                resp = await self._http.get(
                    f"{self.cfg.base_url}/intelligence/addresses/updates",
                    headers=self._headers,
                    params=params,
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                items = data.get("items", [])
                for item in items:
                    addr = item.get("address", "").lower()
                    if not addr or not addr.startswith("0x"):
                        continue
                    wallets.append({
                        "address": addr,
                        "entity_id": item.get("entity_id"),
                        "entity_name": item.get("entity_name"),
                        "label": item.get("label"),
                        "balance_usd": (item.get("metrics") or {}).get("balance_usd"),
                        "volume_usd": (item.get("metrics") or {}).get("volume_usd"),
                    })

                # Pagination
                has_more = data.get("hasMore", False)
                page_token = data.get("pageToken")
                if not has_more or not page_token:
                    break

                if len(wallets) >= self.cfg.max_wallets:
                    break

                # Rate limiting between pages
                await asyncio.sleep(0.1)

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    logger.error("arkham.auth_failed", hint="Check API key")
                elif exc.response.status_code == 429:
                    logger.warning("arkham.rate_limited", page=page)
                    await asyncio.sleep(2)
                    continue
                else:
                    logger.error(
                        "arkham.fetch_wallets.http_error",
                        status=exc.response.status_code,
                        page=page,
                    )
                break
            except Exception:
                logger.exception("arkham.fetch_wallets.error", page=page)
                break

        logger.info("arkham.wallets_fetched", count=len(wallets), tag=self.cfg.tag_id)
        return wallets[:self.cfg.max_wallets]

    # ------------------------------------------------------------------
    # Performance analysis — PnL via historical USD snapshots
    # ------------------------------------------------------------------

    async def analyze_wallet_performance(self, address: str) -> dict:
        """Analyze wallet performance using historical USD flow data.

        Uses /history/address/{address}?chain=base for time-series
        USD balance snapshots.

        Returns dict with pnl_1d_pct, pnl_7d_pct, pnl_30d_pct.
        """
        result = {
            "pnl_1d_pct": None,
            "pnl_7d_pct": None,
            "pnl_30d_pct": None,
            "current_balance_usd": None,
        }

        if not self.enabled:
            return result

        try:
            resp = await self._http.get(
                f"{self.cfg.base_url}/history/address/{address}",
                headers=self._headers,
                params={"chain": "base"},
                timeout=20,
            )
            resp.raise_for_status()
            snapshots = resp.json()

            if not snapshots or not isinstance(snapshots, list):
                return result

            # Sort by time ascending
            snapshots.sort(key=lambda s: s.get("time", ""))

            now = datetime.now(timezone.utc)
            current_usd = snapshots[-1].get("usd", 0) if snapshots else 0
            result["current_balance_usd"] = current_usd

            # Find balance at T-1d, T-7d, T-30d
            for period_key, days in [
                ("pnl_1d_pct", 1),
                ("pnl_7d_pct", 7),
                ("pnl_30d_pct", 30),
            ]:
                target_time = now - timedelta(days=days)
                past_usd = self._find_closest_snapshot(snapshots, target_time)
                if past_usd is not None and past_usd > 0:
                    result[period_key] = round(
                        ((current_usd - past_usd) / past_usd) * 100, 2,
                    )

        except httpx.HTTPStatusError as exc:
            logger.debug(
                "arkham.history.http_error",
                address=address[:10],
                status=exc.response.status_code,
            )
        except Exception:
            logger.debug("arkham.history.error", address=address[:10])

        return result

    @staticmethod
    def _find_closest_snapshot(
        snapshots: list[dict], target_time: datetime,
    ) -> Optional[float]:
        """Find the USD balance closest to target_time."""
        best_usd = None
        best_diff = float("inf")

        for snap in snapshots:
            snap_time_str = snap.get("time", "")
            if not snap_time_str:
                continue
            try:
                snap_time = datetime.fromisoformat(
                    snap_time_str.replace("Z", "+00:00"),
                )
            except ValueError:
                continue
            diff = abs((snap_time - target_time).total_seconds())
            if diff < best_diff:
                best_diff = diff
                best_usd = snap.get("usd", 0)

        # Only return if we found something within 48h tolerance
        if best_diff < 48 * 3600:
            return best_usd
        return None

    # ------------------------------------------------------------------
    # Swap monitoring — detect wallet buys on Base
    # ------------------------------------------------------------------

    async def fetch_recent_swaps(
        self,
        wallet_addresses: list[str],
        time_last: str = "5m",
    ) -> list[dict]:
        """Fetch recent swaps from tracked wallets on Base.

        Uses /swaps endpoint with from={addresses}&chains=base.
        Heavy endpoint: 1 req/sec rate limit.

        Returns list of parsed swap dicts.
        """
        if not self.enabled or not wallet_addresses:
            return []

        swaps: list[dict] = []

        # Process in batches of 10 addresses (API may limit)
        batch_size = 10
        for i in range(0, len(wallet_addresses), batch_size):
            batch = wallet_addresses[i : i + batch_size]
            from_param = ",".join(batch)

            try:
                resp = await self._http.get(
                    f"{self.cfg.base_url}/swaps",
                    headers=self._headers,
                    params={
                        "from": from_param,
                        "chains": "base",
                        "timeLast": time_last,
                        "sortKey": "time",
                        "sortDir": "desc",
                        "limit": 50,
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                for swap in data.get("swaps", []):
                    parsed = self._parse_swap(swap)
                    if parsed:
                        swaps.append(parsed)

            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 429:
                    logger.warning("arkham.swaps.rate_limited")
                    await asyncio.sleep(2)
                else:
                    logger.debug(
                        "arkham.swaps.http_error",
                        status=exc.response.status_code,
                    )
            except Exception:
                logger.debug("arkham.swaps.error", batch=i)

            # Heavy endpoint rate limit
            await asyncio.sleep(self.cfg.heavy_endpoint_delay)

        logger.debug("arkham.swaps_fetched", count=len(swaps))
        return swaps

    @staticmethod
    def _parse_swap(swap: dict) -> Optional[dict]:
        """Parse an Arkham swap into a simplified dict."""
        sender = swap.get("sender", {})
        sender_addr = sender.get("address", "").lower()
        if not sender_addr:
            return None

        # Determine buy/sell direction
        # In a swap, token0 is sold and token1 is bought (from sender perspective)
        # But we need to check the flow direction
        token0_addr = (swap.get("token0") or "").lower()
        token1_addr = (swap.get("token1") or "").lower()
        token0_symbol = swap.get("token0Symbol") or ""
        token1_symbol = swap.get("token1Symbol") or ""
        token0_name = swap.get("token0Name") or ""
        token1_name = swap.get("token1Name") or ""

        usd_value = swap.get("historicalUSD") or 0

        # The bought token is token1 (received), sold token is token0 (sent)
        # If token0 is ETH/WETH/USDC (base currency), this is a BUY of token1
        stable_tokens = {"weth", "eth", "usdc", "usd coin", "wrapped ether", "dai"}
        is_buy_token1 = (
            token0_symbol.lower() in stable_tokens
            or token0_name.lower() in stable_tokens
        )

        if is_buy_token1:
            return {
                "wallet_address": sender_addr,
                "token_address": token1_addr,
                "token_symbol": token1_symbol,
                "token_name": token1_name,
                "action": "buy",
                "usd_value": abs(usd_value),
                "tx_hash": swap.get("id"),
                "block_timestamp": swap.get("blockTimestamp"),
            }
        else:
            # Selling token0 for token1 — could be a sell or a different buy
            return {
                "wallet_address": sender_addr,
                "token_address": token0_addr,
                "token_symbol": token0_symbol,
                "token_name": token0_name,
                "action": "sell",
                "usd_value": abs(usd_value),
                "tx_hash": swap.get("id"),
                "block_timestamp": swap.get("blockTimestamp"),
            }

    # ------------------------------------------------------------------
    # Token holder check — are smart wallets holding a specific token?
    # ------------------------------------------------------------------

    async def count_smart_holders(
        self,
        token_address: str,
        tracked_wallets: set[str],
    ) -> int:
        """Check how many tracked wallets hold a specific token.

        Uses /token/holders/{chain}/{address} to get top holders,
        then intersects with our tracked wallet set.
        """
        if not self.enabled or not tracked_wallets:
            return 0

        try:
            resp = await self._http.get(
                f"{self.cfg.base_url}/token/holders/base/{token_address}",
                headers=self._headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            # Holders are nested under chain key
            holders_by_chain = data.get("holders", {})
            count = 0

            for chain_holders in holders_by_chain.values():
                if not isinstance(chain_holders, list):
                    continue
                for holder in chain_holders:
                    addr_obj = holder.get("address", {})
                    addr = addr_obj.get("address", "").lower() if isinstance(addr_obj, dict) else ""
                    if addr in tracked_wallets:
                        count += 1

            return count

        except httpx.HTTPStatusError:
            return 0
        except Exception:
            return 0

    # ------------------------------------------------------------------
    # Token Holder Intelligence (THI) — identify WHO holds a token
    # ------------------------------------------------------------------

    async def fetch_token_top_holders(
        self,
        token_address: str,
        chain: str = "base",
    ) -> list[dict]:
        """Fetch top holders for a token on the specified chain.

        Uses GET /token/holders/{chain}/{address} with groupByEntity.
        Returns list of dicts with keys: address, balance_usd, share_pct, entity.
        """
        if not self.enabled:
            return []

        try:
            resp = await self._http.get(
                f"{self.cfg.base_url}/token/holders/{chain}/{token_address}",
                headers=self._headers,
                params={"groupByEntity": "true"},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            holders: list[dict] = []
            # Response has holders nested by chain
            holders_data = data.get("holders") or {}

            # Flatten all chain holders
            for chain_key, chain_holders in holders_data.items():
                if not isinstance(chain_holders, list):
                    continue
                for h in chain_holders:
                    addr_obj = h.get("address", {})
                    addr = (addr_obj.get("address", "") if isinstance(addr_obj, dict) else "").lower()
                    entity = addr_obj.get("arkhamEntity", {}) if isinstance(addr_obj, dict) else {}
                    entity_name = entity.get("name") if isinstance(entity, dict) else None
                    entity_type = entity.get("type") if isinstance(entity, dict) else None

                    holders.append({
                        "address": addr,
                        "balance_usd": h.get("balanceUSD") or 0,
                        "share_pct": h.get("share") or 0,
                        "entity_name": entity_name,
                        "entity_type": entity_type,
                        "label": addr_obj.get("arkhamLabel") if isinstance(addr_obj, dict) else None,
                        "tags": addr_obj.get("tags", []) if isinstance(addr_obj, dict) else [],
                    })

            # Sort by share descending
            holders.sort(key=lambda x: x.get("share_pct", 0), reverse=True)
            return holders

        except httpx.HTTPStatusError as exc:
            logger.debug(
                "arkham.token_holders.http_error",
                token=token_address[:12],
                status=exc.response.status_code,
            )
            return []
        except Exception:
            logger.debug("arkham.token_holders.error", token=token_address[:12])
            return []

    async def batch_identify_addresses(
        self,
        addresses: list[str],
        chain: str = "base",
    ) -> dict[str, dict]:
        """Batch identify up to 1000 addresses via Arkham enriched intelligence.

        Uses POST /intelligence/address_enriched/batch with chain filter.
        Returns dict of address -> intelligence data.
        """
        if not self.enabled or not addresses:
            return {}

        result: dict[str, dict] = {}

        # Process in batches of 1000 (API limit)
        for i in range(0, len(addresses), 1000):
            batch = addresses[i : i + 1000]
            try:
                resp = await self._http.post(
                    f"{self.cfg.base_url}/intelligence/address_enriched/batch",
                    headers={**self._headers, "Content-Type": "application/json"},
                    json={"addresses": batch},
                    params={
                        "chain": chain,
                        "includeTags": "true",
                        "includeClusters": "false",
                        "includeEntityPredictions": "false",
                    },
                    timeout=30,
                )
                resp.raise_for_status()
                data = resp.json()

                for addr_data in data if isinstance(data, list) else data.get("addresses", []):
                    addr = (addr_data.get("address") or "").lower()
                    if not addr:
                        continue
                    entity = addr_data.get("arkhamEntity", {})
                    result[addr] = {
                        "address": addr,
                        "entity_name": entity.get("name") if isinstance(entity, dict) else None,
                        "entity_type": entity.get("type") if isinstance(entity, dict) else None,
                        "label": addr_data.get("arkhamLabel"),
                        "is_contract": addr_data.get("isContract", False),
                        "tags": [
                            t.get("id") or t.get("name", "")
                            for t in (addr_data.get("tags") or [])
                            if isinstance(t, dict)
                        ],
                        "balance_usd": addr_data.get("balanceUSD"),
                    }

            except httpx.HTTPStatusError as exc:
                logger.debug(
                    "arkham.batch_identify.http_error",
                    batch_size=len(batch),
                    status=exc.response.status_code,
                )
            except Exception:
                logger.debug("arkham.batch_identify.error", batch_size=len(batch))

            if i + 1000 < len(addresses):
                await asyncio.sleep(0.1)  # Rate limit between batches

        return result

    async def fetch_contract_intel(
        self,
        token_address: str,
        chain: str = "base",
    ) -> Optional[dict]:
        """Get deployer info and contract metadata.

        Uses GET /intelligence/contract/{chain}/{address}.
        Returns dict with deployer address, proxy status, block info.
        """
        if not self.enabled:
            return None

        try:
            resp = await self._http.get(
                f"{self.cfg.base_url}/intelligence/contract/{chain}/{token_address}",
                headers=self._headers,
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            deployer = data.get("deployer", {})
            deployer_addr = (deployer.get("address") or "").lower() if isinstance(deployer, dict) else ""

            return {
                "deployer_address": deployer_addr,
                "deployer_entity": deployer.get("arkhamEntity", {}) if isinstance(deployer, dict) else {},
                "deployer_label": deployer.get("arkhamLabel") if isinstance(deployer, dict) else None,
                "is_proxy": data.get("isProxy", False),
                "block_number": data.get("blockNumber"),
                "block_timestamp": data.get("blockTimestamp"),
            }

        except httpx.HTTPStatusError as exc:
            logger.debug(
                "arkham.contract_intel.http_error",
                token=token_address[:12],
                status=exc.response.status_code,
            )
            return None
        except Exception:
            logger.debug("arkham.contract_intel.error", token=token_address[:12])
            return None

    async def fetch_token_flows(
        self,
        token_address: str,
        chain: str = "base",
        time_last: str = "24h",
    ) -> Optional[dict]:
        """Get top inflows/outflows for a token.

        Uses GET /token/top_flow/{chain}/{address} (heavy: 1 req/sec).
        Returns dict with top_inflows and top_outflows.
        """
        if not self.enabled:
            return None

        try:
            resp = await self._http.get(
                f"{self.cfg.base_url}/token/top_flow/{chain}/{token_address}",
                headers=self._headers,
                params={"timeLast": time_last},
                timeout=20,
            )
            resp.raise_for_status()
            data = resp.json()

            def _parse_flows(flow_list: list) -> list[dict]:
                parsed = []
                for f in (flow_list or []):
                    addr_obj = f.get("address", {})
                    addr = (addr_obj.get("address", "") if isinstance(addr_obj, dict) else "").lower()
                    entity = addr_obj.get("arkhamEntity", {}) if isinstance(addr_obj, dict) else {}
                    parsed.append({
                        "address": addr,
                        "usd_value": f.get("usd") or 0,
                        "token_value": f.get("value") or 0,
                        "entity_name": entity.get("name") if isinstance(entity, dict) else None,
                        "entity_type": entity.get("type") if isinstance(entity, dict) else None,
                        "label": addr_obj.get("arkhamLabel") if isinstance(addr_obj, dict) else None,
                    })
                return parsed

            return {
                "top_inflows": _parse_flows(data.get("inflows") or data.get("topInflows") or []),
                "top_outflows": _parse_flows(data.get("outflows") or data.get("topOutflows") or []),
            }

        except httpx.HTTPStatusError as exc:
            logger.debug(
                "arkham.token_flows.http_error",
                token=token_address[:12],
                status=exc.response.status_code,
            )
            return None
        except Exception:
            logger.debug("arkham.token_flows.error", token=token_address[:12])
            return None

    async def fetch_wallet_portfolio(
        self,
        address: str,
        chain: str = "base",
    ) -> list[dict]:
        """Get token balances for a wallet on a specific chain.

        Uses GET /balances/address/{address}?chains={chain}.
        Returns list of token holding dicts.
        """
        if not self.enabled:
            return []

        try:
            resp = await self._http.get(
                f"{self.cfg.base_url}/balances/address/{address}",
                headers=self._headers,
                params={"chains": chain},
                timeout=15,
            )
            resp.raise_for_status()
            data = resp.json()

            holdings: list[dict] = []
            # Response is typically a dict of chain -> token balances
            for chain_data in (data.get("chains") or data if isinstance(data, dict) else [data]):
                tokens_list = []
                if isinstance(chain_data, dict):
                    tokens_list = chain_data.get("tokens") or chain_data.get("balances") or []
                elif isinstance(chain_data, list):
                    tokens_list = chain_data

                for tok in tokens_list:
                    if isinstance(tok, dict):
                        holdings.append({
                            "token_address": (tok.get("token", {}).get("address") or tok.get("address") or "").lower(),
                            "token_symbol": tok.get("token", {}).get("symbol") or tok.get("symbol") or "",
                            "token_name": tok.get("token", {}).get("name") or tok.get("name") or "",
                            "balance_usd": tok.get("balanceUSD") or tok.get("usd") or 0,
                            "quantity": tok.get("quantity") or tok.get("balance") or 0,
                        })

            # Sort by USD balance descending
            holdings.sort(key=lambda x: x.get("balance_usd", 0), reverse=True)
            return holdings

        except httpx.HTTPStatusError as exc:
            logger.debug(
                "arkham.portfolio.http_error",
                address=address[:10],
                status=exc.response.status_code,
            )
            return []
        except Exception:
            logger.debug("arkham.portfolio.error", address=address[:10])
            return []

    # ------------------------------------------------------------------
    # Composite intelligence methods — high-level trader-grade analysis
    # ------------------------------------------------------------------

    async def analyze_holder_quality(self, token_address: str) -> dict:
        """Composite analysis: fetch holders → identify → score quality.

        Returns a dict with:
        - holder_score: 0.0-1.0 quality score
        - known_entities: count of Arkham-labeled holders
        - fund_vc_count: count of fund/VC entities holding
        - whale_count: count of large wallets (>$50K in token)
        - concentration_top10: % of supply in top 10 holders
        - exchange_holders: count of exchange-linked addresses
        - risk_flags: list of risk indicators
        - smart_wallet_holders: count of our tracked wallets holding
        """
        result = {
            "holder_score": 0.0,
            "known_entities": 0,
            "fund_vc_count": 0,
            "whale_count": 0,
            "concentration_top10": 0.0,
            "exchange_holders": 0,
            "risk_flags": [],
            "smart_wallet_holders": 0,
            "total_holders_analyzed": 0,
        }

        if not self.enabled:
            return result

        # Step 1: Get top holders
        holders = await self.fetch_token_top_holders(token_address)
        if not holders:
            return result

        result["total_holders_analyzed"] = len(holders)

        # Step 2: Calculate concentration risk (top 10)
        top_10_share = sum(h.get("share_pct", 0) for h in holders[:10])
        result["concentration_top10"] = round(top_10_share * 100 if top_10_share < 1 else top_10_share, 2)

        # Step 3: Batch identify unknown holders
        unknown_addrs = [
            h["address"]
            for h in holders
            if h["address"] and not h.get("entity_name") and h["address"].startswith("0x")
        ]
        if unknown_addrs:
            intel = await self.batch_identify_addresses(unknown_addrs[:100])
            # Merge intel back into holders
            for h in holders:
                if h["address"] in intel:
                    info = intel[h["address"]]
                    h["entity_name"] = h.get("entity_name") or info.get("entity_name")
                    h["entity_type"] = h.get("entity_type") or info.get("entity_type")
                    h["label"] = h.get("label") or info.get("label")
                    h["tags"] = h.get("tags") or info.get("tags", [])

        # Step 4: Classify holders
        fund_vc_types = {"fund", "vc", "venture_capital", "hedge_fund", "investment"}
        exchange_types = {"exchange", "cex", "dex"}

        score = 0.0
        for h in holders:
            entity_type = (h.get("entity_type") or "").lower()
            entity_name = h.get("entity_name") or ""
            label = h.get("label") or ""
            tags = h.get("tags") or []
            balance = h.get("balance_usd") or 0

            # Known entity (has a name/label in Arkham)
            if entity_name or label:
                result["known_entities"] += 1
                score += 0.02  # Each known entity adds a small bonus

            # Fund / VC holding
            if entity_type in fund_vc_types or any(
                t in entity_type for t in ("fund", "capital", "ventures")
            ):
                result["fund_vc_count"] += 1
                score += 0.10  # Strong signal

            # Exchange holding
            if entity_type in exchange_types:
                result["exchange_holders"] += 1

            # Whale detection (>$50K position)
            if balance > 50_000:
                result["whale_count"] += 1
                if entity_name:  # Known whale is good
                    score += 0.05
                else:  # Unknown whale is neutral-positive
                    score += 0.02

            # Tag-based signals
            tag_set = {t.lower() if isinstance(t, str) else "" for t in tags}
            if "smart-money" in tag_set or "early-token-holder" in tag_set:
                result["smart_wallet_holders"] += 1
                score += 0.05

            # Risk: known scammer tags
            scam_tags = {"scam", "hack", "exploit", "phishing", "rugpull"}
            if tag_set & scam_tags:
                result["risk_flags"].append(f"Holder has scam tag: {tag_set & scam_tags}")
                score -= 0.20

        # Risk flags
        if result["concentration_top10"] > 80:
            result["risk_flags"].append(f"High concentration: top 10 hold {result['concentration_top10']:.0f}%")
            score -= 0.10
        elif result["concentration_top10"] > 60:
            result["risk_flags"].append(f"Moderate concentration: top 10 hold {result['concentration_top10']:.0f}%")
            score -= 0.05

        if result["exchange_holders"] >= 3:
            result["risk_flags"].append(f"{result['exchange_holders']} exchange addresses holding")
            score -= 0.05  # Multiple exchange wallets = potential dump vectors

        result["holder_score"] = round(max(min(score, 1.0), 0.0), 4)

        logger.info(
            "arkham.holder_quality",
            token=token_address[:12],
            score=result["holder_score"],
            known=result["known_entities"],
            funds=result["fund_vc_count"],
            whales=result["whale_count"],
            concentration=result["concentration_top10"],
        )
        return result

    async def profile_deployer(self, token_address: str) -> dict:
        """Composite analysis: fetch contract → identify deployer → risk assessment.

        Returns a dict with:
        - deployer_address: the deployer wallet
        - deployer_name: Arkham entity name (if known)
        - deployer_type: entity type (fund, exchange, etc.)
        - deployer_tags: list of tags on the deployer address
        - deployer_score: -1.0 to 1.0 (negative=bad, positive=good)
        - risk_level: 'safe' | 'unknown' | 'risky' | 'dangerous'
        - is_serial_deployer: bool (has deployed many contracts)
        """
        result = {
            "deployer_address": "",
            "deployer_name": None,
            "deployer_type": None,
            "deployer_tags": [],
            "deployer_score": 0.0,
            "risk_level": "unknown",
            "is_serial_deployer": False,
            "is_proxy": False,
        }

        if not self.enabled:
            return result

        # Step 1: Get contract intelligence
        contract = await self.fetch_contract_intel(token_address)
        if not contract:
            return result

        deployer_addr = contract.get("deployer_address", "")
        result["deployer_address"] = deployer_addr
        result["is_proxy"] = contract.get("is_proxy", False)

        if not deployer_addr:
            return result

        # Step 2: Identify deployer via enriched intelligence
        deployer_entity = contract.get("deployer_entity", {})
        deployer_name = deployer_entity.get("name") if isinstance(deployer_entity, dict) else None
        deployer_type = deployer_entity.get("type") if isinstance(deployer_entity, dict) else None
        deployer_label = contract.get("deployer_label")

        # If no entity info from contract, try direct lookup
        if not deployer_name:
            intel = await self.batch_identify_addresses([deployer_addr])
            if deployer_addr in intel:
                info = intel[deployer_addr]
                deployer_name = info.get("entity_name")
                deployer_type = info.get("entity_type")
                deployer_label = deployer_label or info.get("label")
                result["deployer_tags"] = info.get("tags", [])

        result["deployer_name"] = deployer_name or deployer_label
        result["deployer_type"] = deployer_type

        # Step 3: Risk assessment
        score = 0.0
        tags = {t.lower() if isinstance(t, str) else "" for t in result["deployer_tags"]}

        # Known builder / reputable entity → bonus
        if deployer_name:
            score += 0.10
            if deployer_type in ("fund", "vc", "protocol", "dapp"):
                score += 0.15
                result["risk_level"] = "safe"
            elif deployer_type in ("exchange", "cex"):
                score += 0.05
                result["risk_level"] = "safe"

        # Known bad tags → penalty
        bad_tags = {"scam", "hack", "exploit", "phishing", "rugpull", "sanctioned",
                    "ofac-sanctioned", "theft"}
        if tags & bad_tags:
            score = -1.0
            result["risk_level"] = "dangerous"

        # Contract deployer tag → could be serial deployer
        if "contract-deployer" in tags:
            result["is_serial_deployer"] = True
            # Not inherently bad, but a flag
            score -= 0.05

        # Proxy contracts are a yellow flag (can be upgraded = rug vector)
        if result["is_proxy"]:
            score -= 0.10
            if result["risk_level"] == "unknown":
                result["risk_level"] = "risky"

        if result["risk_level"] == "unknown":
            if score > 0:
                result["risk_level"] = "safe"
            elif score < -0.10:
                result["risk_level"] = "risky"

        result["deployer_score"] = round(max(min(score, 1.0), -1.0), 4)

        logger.info(
            "arkham.deployer_profile",
            token=token_address[:12],
            deployer=deployer_addr[:12],
            name=deployer_name,
            risk=result["risk_level"],
            score=result["deployer_score"],
        )
        return result


class TokenFlowMonitor:
    """Monitors token flows for smart money dump/accumulation detection.

    Periodically checks /token/top_flow for alerted tokens to detect:
    - Smart money dumping (known wallets selling) → warning alert
    - Smart money accumulating (known wallets buying) → bullish signal
    - Whale concentration changes → risk assessment update
    """

    def __init__(
        self,
        config: AppConfig,
        arkham: ArkhamClient,
        session_factory,
    ) -> None:
        self.cfg = config.arkham
        self._arkham = arkham
        self._session_factory = session_factory
        self._last_check: dict[str, datetime] = {}  # token_addr -> last check time

    async def check_flows(self, max_tokens: int = 10) -> list[dict]:
        """Check token flows for recently alerted tokens.

        Returns list of significant flow events for alerting.
        Each event dict has: token, direction, usd_total, top_movers, signal_type.
        """
        if not self._arkham.enabled:
            return []

        events: list[dict] = []
        now = datetime.now(timezone.utc)
        check_interval = timedelta(seconds=self.cfg.flow_poll_interval_seconds)

        async with self._session_factory() as session:
            # Get recently alerted tokens with open paper positions
            from sqlalchemy import select as sa_select, or_
            from .models import PaperPosition

            stmt = (
                sa_select(Token)
                .join(PaperPosition, PaperPosition.token_id == Token.id)
                .where(Token.alert_sent.is_(True))
                .where(PaperPosition.status.in_(["open", "tp1", "tp2"]))
                .order_by(Token.updated_at.desc())
                .limit(max_tokens)
            )
            tokens = list((await session.execute(stmt)).scalars().all())

            if not tokens:
                return []

            for token in tokens:
                addr = token.contract_address.lower()

                # Respect check interval per token
                last = self._last_check.get(addr)
                if last and (now - last) < check_interval:
                    continue

                self._last_check[addr] = now

                # Fetch token flows
                flows = await self._arkham.fetch_token_flows(addr, time_last="24h")
                if not flows:
                    continue

                inflows = flows.get("top_inflows", [])
                outflows = flows.get("top_outflows", [])

                # Analyze for significant events
                total_inflow = sum(f.get("usd_value", 0) for f in inflows)
                total_outflow = sum(f.get("usd_value", 0) for f in outflows)

                # Significant outflow from known entities = dump warning
                known_outflows = [
                    f for f in outflows
                    if f.get("entity_name") and f.get("usd_value", 0) >= self.cfg.flow_min_usd
                ]
                if known_outflows:
                    total_known_out = sum(f["usd_value"] for f in known_outflows)
                    if total_known_out >= self.cfg.flow_alert_threshold_usd:
                        events.append({
                            "token": token,
                            "direction": "outflow",
                            "signal_type": "dump_warning",
                            "usd_total": total_known_out,
                            "top_movers": known_outflows[:5],
                            "total_inflow": total_inflow,
                            "total_outflow": total_outflow,
                        })

                # Significant inflow from known entities = accumulation signal
                known_inflows = [
                    f for f in inflows
                    if f.get("entity_name") and f.get("usd_value", 0) >= self.cfg.flow_min_usd
                ]
                if known_inflows:
                    total_known_in = sum(f["usd_value"] for f in known_inflows)
                    if total_known_in >= self.cfg.flow_alert_threshold_usd:
                        events.append({
                            "token": token,
                            "direction": "inflow",
                            "signal_type": "accumulation",
                            "usd_total": total_known_in,
                            "top_movers": known_inflows[:5],
                            "total_inflow": total_inflow,
                            "total_outflow": total_outflow,
                        })

                # Heavy endpoint rate limit
                await asyncio.sleep(self.cfg.heavy_endpoint_delay)

        if events:
            logger.info(
                "flow_monitor.events",
                count=len(events),
                types=[e["signal_type"] for e in events],
            )

        return events


class WalletTracker:
    """Orchestrates wallet sync, performance analysis, and swap monitoring.

    Runs as a loop inside main.py's Tracker class.
    """

    def __init__(
        self,
        config: AppConfig,
        arkham: ArkhamClient,
        session_factory,
    ) -> None:
        self.cfg = config.arkham
        self._arkham = arkham
        self._session_factory = session_factory
        self._tracked_addresses: set[str] = set()
        self._last_swap_check: Optional[datetime] = None

    @property
    def tracked_wallets(self) -> set[str]:
        """Return the current set of tracked wallet addresses."""
        return self._tracked_addresses

    # ------------------------------------------------------------------
    # Full wallet sync — fetch, analyze, persist
    # ------------------------------------------------------------------

    async def sync_wallets(self) -> int:
        """Full sync: fetch fomo wallets → analyze performance → persist.

        Returns the number of profitable wallets added/updated.
        """
        # Step 1: Fetch wallets from Arkham
        raw_wallets = await self._arkham.fetch_fomo_wallets()
        if not raw_wallets:
            logger.warning("wallet_sync.no_wallets")
            return 0

        profitable_count = 0

        async with self._session_factory() as session:
            for wallet_data in raw_wallets:
                address = wallet_data["address"]

                # Step 2: Analyze performance
                perf = await self._arkham.analyze_wallet_performance(address)

                # Step 3: Check profitability
                profitable_periods = sum([
                    1 if (perf["pnl_1d_pct"] or 0) >= self.cfg.min_profit_pct_1d else 0,
                    1 if (perf["pnl_7d_pct"] or 0) >= self.cfg.min_profit_pct_7d else 0,
                    1 if (perf["pnl_30d_pct"] or 0) >= self.cfg.min_profit_pct_30d else 0,
                ])

                is_profitable = profitable_periods >= self.cfg.min_profitable_periods

                # Step 4: Determine tier
                if profitable_periods == 3:
                    tier = 1  # All periods profitable
                elif profitable_periods == 2:
                    tier = 2  # Most periods profitable
                else:
                    tier = 3  # Marginal or not profitable

                # Step 5: Upsert to DB
                stmt = select(SmartWallet).where(SmartWallet.address == address)
                existing = (await session.execute(stmt)).scalar_one_or_none()

                now = datetime.now(timezone.utc)

                if existing:
                    existing.pnl_1d_pct = perf["pnl_1d_pct"]
                    existing.pnl_7d_pct = perf["pnl_7d_pct"]
                    existing.pnl_30d_pct = perf["pnl_30d_pct"]
                    existing.balance_usd = perf["current_balance_usd"] or wallet_data.get("balance_usd")
                    existing.volume_usd = wallet_data.get("volume_usd")
                    existing.tier = tier
                    existing.profitable_periods = profitable_periods
                    existing.is_active = is_profitable
                    existing.last_synced_at = now
                    existing.arkham_entity = wallet_data.get("entity_name")
                    existing.arkham_label = wallet_data.get("label")
                else:
                    wallet = SmartWallet(
                        address=address,
                        chain="base",
                        tag=self.cfg.tag_id,
                        arkham_entity=wallet_data.get("entity_name"),
                        arkham_label=wallet_data.get("label"),
                        tier=tier,
                        pnl_1d_pct=perf["pnl_1d_pct"],
                        pnl_7d_pct=perf["pnl_7d_pct"],
                        pnl_30d_pct=perf["pnl_30d_pct"],
                        balance_usd=perf["current_balance_usd"] or wallet_data.get("balance_usd"),
                        volume_usd=wallet_data.get("volume_usd"),
                        is_active=is_profitable,
                        profitable_periods=profitable_periods,
                        last_synced_at=now,
                    )
                    session.add(wallet)

                if is_profitable:
                    profitable_count += 1
                    self._tracked_addresses.add(address)

                # Rate limit between wallet analyses (history endpoint)
                await asyncio.sleep(0.15)

            await session.commit()

        # Update smart_money_wallets.txt for Stage 4 compatibility
        await self._update_wallet_file()

        logger.info(
            "wallet_sync.complete",
            total_fetched=len(raw_wallets),
            profitable=profitable_count,
            tracked=len(self._tracked_addresses),
        )
        return profitable_count

    async def load_tracked_wallets(self) -> None:
        """Load active wallets from DB into memory on startup."""
        async with self._session_factory() as session:
            stmt = (
                select(SmartWallet.address)
                .where(SmartWallet.is_active.is_(True))
            )
            rows = (await session.execute(stmt)).scalars().all()
            self._tracked_addresses = {addr.lower() for addr in rows}
            logger.info("wallet_tracker.loaded", count=len(self._tracked_addresses))

    # ------------------------------------------------------------------
    # Swap monitoring — detect new buys from tracked wallets
    # ------------------------------------------------------------------

    async def check_swaps(self) -> list[dict]:
        """Check for new swaps from tracked wallets.

        Returns list of new buy signals (conviction candidates).
        """
        if not self._tracked_addresses:
            return []

        # Determine time window
        time_last = "5m"
        if self._last_swap_check:
            elapsed = (datetime.now(timezone.utc) - self._last_swap_check).total_seconds()
            if elapsed > 300:
                time_last = "10m"
            if elapsed > 600:
                time_last = "30m"

        self._last_swap_check = datetime.now(timezone.utc)

        # Fetch recent swaps
        addresses = list(self._tracked_addresses)
        swaps = await self._arkham.fetch_recent_swaps(addresses, time_last)

        # Filter to buys above minimum USD
        new_buys: list[dict] = []
        async with self._session_factory() as session:
            for swap in swaps:
                if swap["action"] != "buy":
                    continue
                if (swap["usd_value"] or 0) < self.cfg.min_swap_usd:
                    continue

                # Deduplicate — check if tx_hash already recorded
                tx_hash = swap.get("tx_hash")
                if tx_hash:
                    existing = (
                        await session.execute(
                            select(WalletSwap.id).where(WalletSwap.tx_hash == tx_hash).limit(1)
                        )
                    ).scalar_one_or_none()
                    if existing:
                        continue

                # Record the swap
                block_ts = None
                if swap.get("block_timestamp"):
                    try:
                        block_ts = datetime.fromisoformat(
                            swap["block_timestamp"].replace("Z", "+00:00"),
                        )
                    except ValueError:
                        pass

                wallet_swap = WalletSwap(
                    wallet_address=swap["wallet_address"],
                    token_address=swap["token_address"],
                    token_symbol=swap["token_symbol"],
                    token_name=swap["token_name"],
                    action="buy",
                    usd_value=swap["usd_value"],
                    tx_hash=tx_hash,
                    block_timestamp=block_ts,
                )
                session.add(wallet_swap)
                new_buys.append(swap)

            await session.commit()

        if new_buys:
            logger.info(
                "wallet_tracker.new_buys",
                count=len(new_buys),
                tokens=[b["token_symbol"] for b in new_buys],
            )

        return new_buys

    # ------------------------------------------------------------------
    # Conviction detection — wallet buys token already in our DB
    # ------------------------------------------------------------------

    async def detect_convictions(self, new_buys: list[dict]) -> list[dict]:
        """Check if any new buys are for tokens already in our database.

        A conviction signal means: a profitable wallet is buying a token
        we already know about (discovered by firehose/champagne/breakout).
        This is the strongest smart money signal.

        Returns list of conviction dicts with token + wallet info.
        """
        if not new_buys:
            return []

        convictions: list[dict] = []
        async with self._session_factory() as session:
            for buy in new_buys:
                token_addr = buy["token_address"].lower()
                if not token_addr:
                    continue

                # Check if token exists in our DB
                stmt = (
                    select(Token)
                    .where(Token.contract_address == token_addr)
                    .limit(1)
                )
                token = (await session.execute(stmt)).scalar_one_or_none()
                if not token:
                    continue

                # Get wallet tier info
                wallet_stmt = (
                    select(SmartWallet)
                    .where(SmartWallet.address == buy["wallet_address"])
                    .limit(1)
                )
                wallet = (await session.execute(wallet_stmt)).scalar_one_or_none()

                convictions.append({
                    "token": token,
                    "wallet": wallet,
                    "buy": buy,
                    "is_alerted": token.alert_sent,
                })

                # Mark the swap as conviction
                if buy.get("tx_hash"):
                    swap_stmt = (
                        select(WalletSwap)
                        .where(WalletSwap.tx_hash == buy["tx_hash"])
                        .limit(1)
                    )
                    swap_record = (await session.execute(swap_stmt)).scalar_one_or_none()
                    if swap_record:
                        swap_record.conviction_sent = True

            await session.commit()

        if convictions:
            logger.info(
                "wallet_tracker.convictions",
                count=len(convictions),
                tokens=[c["token"].symbol for c in convictions],
            )

        return convictions

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    async def _update_wallet_file(self) -> None:
        """Write active tracked wallets to data/smart_money_wallets.txt.

        This keeps the flat file in sync with the DB for Stage 4
        compatibility (TokenFilter loads wallets from this file).
        """
        from pathlib import Path

        path = Path("data/smart_money_wallets.txt")
        lines = [
            "# Smart Money Wallets — auto-synced from Arkham Intel",
            f"# Tag: {self.cfg.tag_id} | Last sync: {datetime.now(timezone.utc).isoformat()}",
            f"# Active wallets: {len(self._tracked_addresses)}",
            "#",
        ]
        for addr in sorted(self._tracked_addresses):
            lines.append(addr)

        path.write_text("\n".join(lines) + "\n")
        logger.debug("wallet_file.updated", wallets=len(self._tracked_addresses))

    async def get_wallet_stats(self) -> dict:
        """Get summary stats for the /wallets bot command."""
        async with self._session_factory() as session:
            from sqlalchemy import func as sqlfunc

            total_stmt = select(sqlfunc.count(SmartWallet.id))
            total = (await session.execute(total_stmt)).scalar() or 0

            active_stmt = (
                select(sqlfunc.count(SmartWallet.id))
                .where(SmartWallet.is_active.is_(True))
            )
            active = (await session.execute(active_stmt)).scalar() or 0

            tier1_stmt = (
                select(sqlfunc.count(SmartWallet.id))
                .where(SmartWallet.tier == 1)
                .where(SmartWallet.is_active.is_(True))
            )
            tier1 = (await session.execute(tier1_stmt)).scalar() or 0

            tier2_stmt = (
                select(sqlfunc.count(SmartWallet.id))
                .where(SmartWallet.tier == 2)
                .where(SmartWallet.is_active.is_(True))
            )
            tier2 = (await session.execute(tier2_stmt)).scalar() or 0

            swaps_24h_stmt = (
                select(sqlfunc.count(WalletSwap.id))
                .where(
                    WalletSwap.recorded_at >= datetime.now(timezone.utc) - timedelta(hours=24)
                )
            )
            swaps_24h = (await session.execute(swaps_24h_stmt)).scalar() or 0

            convictions_stmt = (
                select(sqlfunc.count(WalletSwap.id))
                .where(WalletSwap.conviction_sent.is_(True))
                .where(
                    WalletSwap.recorded_at >= datetime.now(timezone.utc) - timedelta(hours=24)
                )
            )
            convictions = (await session.execute(convictions_stmt)).scalar() or 0

            # Top wallets
            top_stmt = (
                select(SmartWallet)
                .where(SmartWallet.is_active.is_(True))
                .order_by(SmartWallet.tier.asc(), SmartWallet.pnl_7d_pct.desc().nullslast())
                .limit(5)
            )
            top_wallets = list((await session.execute(top_stmt)).scalars().all())

        return {
            "total": total,
            "active": active,
            "tier1": tier1,
            "tier2": tier2,
            "tier3": active - tier1 - tier2,
            "swaps_24h": swaps_24h,
            "convictions_24h": convictions,
            "top_wallets": top_wallets,
        }
