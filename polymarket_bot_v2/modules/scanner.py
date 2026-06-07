"""
scanner.py — Market data scanner.
Priority: WebSocket (RTDS) for real-time orderbook/price data.
Fallback: REST polling via Gamma API + CLOB API.
Rate-limit safe with exponential backoff and request queuing.
"""

import asyncio
import logging
import json
import time
from typing import Dict, List, Optional, Callable, Any, Set
from dataclasses import dataclass, field
from datetime import datetime
import aiohttp
import websockets
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

from config import settings
from modules.logger import decision_log

log = logging.getLogger("scanner")


# ──────────────────────────────────────────
# Data Structures
# ──────────────────────────────────────────

@dataclass
class MarketData:
    market_id: str          # CLOB condition_id
    question: str
    category: str           # Politics, Geopolitics, Sports, etc.
    end_date: Optional[str]
    yes_price: float        # Best YES ask (0.0 – 1.0)
    no_price: float         # Best NO ask  (0.0 – 1.0)
    yes_bid: float
    no_bid: float
    volume_24h: float
    liquidity_usd: float
    is_active: bool
    last_updated: float = field(default_factory=time.time)

    @property
    def mid_yes(self) -> float:
        return (self.yes_price + self.yes_bid) / 2

    @property
    def price_sum(self) -> float:
        """YES ask + NO ask. <1.0 may indicate arb opportunity."""
        return self.yes_price + self.no_price

    @property
    def implied_fee(self) -> float:
        """Estimate of round-trip fee embedded in spread."""
        return max(0.0, 1.0 - self.price_sum)

    @property
    def stale(self) -> bool:
        return (time.time() - self.last_updated) > 120  # >2 min = stale


# ──────────────────────────────────────────
# Rate Limiter
# ──────────────────────────────────────────

class RateLimiter:
    """Token bucket rate limiter for API calls."""

    def __init__(self, calls_per_second: float = 5.0):
        self.rate = calls_per_second
        self.tokens = calls_per_second
        self.last_check = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self):
        async with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_check
            self.tokens = min(self.rate, self.tokens + elapsed * self.rate)
            self.last_check = now
            if self.tokens < 1:
                sleep_time = (1 - self.tokens) / self.rate
                await asyncio.sleep(sleep_time)
                self.tokens = 0
            else:
                self.tokens -= 1


# ──────────────────────────────────────────
# Scanner
# ──────────────────────────────────────────

class MarketScanner:
    """
    Maintains a live cache of MarketData for all active markets.
    Drives the bot's awareness of current prices and opportunities.
    """

    # Low-fee categories — prioritize these for micro-capital
    PRIORITY_CATEGORIES = {
        "Politics", "Geopolitics", "World Events",
        "Economics", "Science", "Elections",
    }
    # These are often fee-free at lopsided probabilities
    ZERO_FEE_THRESHOLD = 0.02  # price_sum <0.98 treated as fee-free zone

    def __init__(self):
        self.markets: Dict[str, MarketData] = {}      # condition_id → MarketData
        self._callbacks: List[Callable] = []
        self._ws_subscriptions: Set[str] = set()
        self._rate_limiter = RateLimiter(calls_per_second=4.0)
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[websockets.WebSocketClientProtocol] = None
        self._running = False
        self._markets_lock = asyncio.Lock()
        self._last_ws_msg_time = time.monotonic()

    async def get_markets_snapshot(self) -> dict:
        """Thread-safe copy of markets dict. Always use this in scan loops."""
        async with self._markets_lock:
            return dict(self.markets)

    async def set_market(self, md) -> None:
        """Thread-safe market data write."""
        async with self._markets_lock:
            self.markets[md.market_id] = md

    def on_update(self, callback: Callable[[MarketData], Any]):
        """Register a callback for market updates."""
        self._callbacks.append(callback)

    async def start(self):
        """Initialize HTTP session and start scanning."""
        connector = aiohttp.TCPConnector(
            limit=20,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(total=15, connect=5)
        self._session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={"User-Agent": "PolyBot/1.0"},
        )
        self._running = True
        log.info("Scanner started.")

    async def stop(self):
        self._running = False
        if self._ws:
            await self._ws.close()
        if self._session:
            await self._session.close()

    # ── REST Market Fetching ──────────────────

    @retry(
        stop=stop_after_attempt(settings.MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
        reraise=True,
    )
    async def fetch_active_markets(self, limit: int = 100, offset: int = 0) -> List[MarketData]:
        """Fetch active markets from Gamma API."""
        await self._rate_limiter.acquire()
        url = f"{settings.gamma_api}/markets"
        params = {
            "active": "true",
            "closed": "false",
            "limit": limit,
            "offset": offset,
            "order": "volume24hr",
            "ascending": "false",
        }

        async with self._session.get(url, params=params) as resp:
            if resp.status != 200:
                log.warning(f"Gamma API returned {resp.status}")
                return []
            raw = await resp.json()

        markets = []
        for m in raw:
            md = self._parse_gamma_market(m)
            if md:
                await self.set_market(md)
                markets.append(md)

        log.debug(f"Fetched {len(markets)} active markets (offset={offset})")
        return markets

    def _parse_gamma_market(self, raw: dict) -> Optional[MarketData]:
        """Parse Gamma API market response into MarketData."""
        try:
            tokens = raw.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token  = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)

            if not yes_token or not no_token:
                return None

            yes_price = float(yes_token.get("price", 0.5))
            no_price  = float(no_token.get("price", 0.5))

            return MarketData(
                market_id=raw.get("conditionId", raw.get("id", "")),
                question=raw.get("question", "Unknown")[:120],
                category=raw.get("groupItemTagID", raw.get("category", "Other")),
                end_date=raw.get("endDate"),
                yes_price=yes_price,
                no_price=no_price,
                yes_bid=yes_price - 0.01,   # approx bid; WebSocket gives real bid
                no_bid=no_price - 0.01,
                volume_24h=float(raw.get("volume24hr", 0)),
                liquidity_usd=float(raw.get("liquidity", 0)),
                is_active=raw.get("active", False) and not raw.get("closed", True),
                last_updated=time.time(),
            )
        except Exception as e:
            log.debug(f"Failed to parse market: {e}")
            return None

    @retry(
        stop=stop_after_attempt(settings.MAX_RETRIES),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((aiohttp.ClientError, asyncio.TimeoutError)),
    )
    async def fetch_market_orderbook(self, token_id: str) -> Optional[dict]:
        """Fetch live orderbook for a specific token from CLOB."""
        await self._rate_limiter.acquire()
        url = f"{settings.clob_host}/book"
        params = {"token_id": token_id}
        try:
            async with self._session.get(url, params=params) as resp:
                if resp.status == 200:
                    return await resp.json()
        except Exception as e:
            log.debug(f"Orderbook fetch error for {token_id}: {e}")
        return None

    # ── WebSocket Real-Time Data ──────────────

    async def subscribe_ws(self, market_ids: List[str]):
        """
        Subscribe to real-time price updates via CLOB WebSocket (RTDS).
        Falls back to polling if WS unavailable.
        """
        if not market_ids:
            return

        ws_url = "wss://clob.polymarket.com/ws/market"
        reconnect_delay = settings.WS_RECONNECT_DELAY

        while self._running:
            try:
                async with websockets.connect(
                    ws_url,
                    ping_interval=20,
                    ping_timeout=10,
                    close_timeout=5,
                ) as ws:
                    self._ws = ws
                    reconnect_delay = settings.WS_RECONNECT_DELAY  # reset on success

                    # Subscribe to markets
                    for mid in market_ids:
                        sub_msg = json.dumps({
                            "type": "subscribe",
                            "channel": "price_change",
                            "market": mid,
                        })
                        await ws.send(sub_msg)
                        self._ws_subscriptions.add(mid)

                    log.info(f"WS subscribed to {len(market_ids)} markets")

                    async def _watchdog(ws_conn):
                        while True:
                            await asyncio.sleep(10)
                            if time.monotonic() - self._last_ws_msg_time > 60:
                                log.warning("WS heartbeat timeout (60s no data) — forcing reconnect")
                                await ws_conn.close()
                                return

                    watchdog_task = asyncio.create_task(_watchdog(ws))
                    try:
                        async for raw_msg in ws:
                            self._last_ws_msg_time = time.monotonic()
                            if not self._running:
                                break
                            await self._handle_ws_message(raw_msg)
                    finally:
                        watchdog_task.cancel()

            except (websockets.exceptions.ConnectionClosed,
                    websockets.exceptions.WebSocketException,
                    asyncio.TimeoutError,
                    OSError) as e:
                log.warning(f"WS disconnected: {e} — reconnecting in {reconnect_delay}s")
                reconnect_delay = min(reconnect_delay * 2, 60)
                await asyncio.sleep(reconnect_delay)
            except Exception as e:
                log.error(f"WS unexpected error: {e}")
                await asyncio.sleep(reconnect_delay)

    async def _handle_ws_message(self, raw: str):
        """Parse WebSocket price update and update market cache."""
        try:
            msg = json.loads(raw)
            event_type = msg.get("event_type", msg.get("type", ""))

            if event_type in ("price_change", "tick"):
                market_id = msg.get("asset_id", msg.get("market", ""))
                outcome = msg.get("outcome", "").upper()
                price = float(msg.get("price", 0))

                if market_id in self.markets:
                    md = self.markets[market_id]
                    if outcome == "YES":
                        md.yes_price = price
                    elif outcome == "NO":
                        md.no_price = price
                    md.last_updated = time.time()

                    # Notify listeners
                    for cb in self._callbacks:
                        try:
                            asyncio.create_task(cb(md))
                        except Exception:
                            pass

        except (json.JSONDecodeError, ValueError, KeyError):
            pass

    # ── Polling Fallback ──────────────────────

    async def poll_markets_loop(self):
        """
        Fallback REST polling loop when WS is unavailable.
        Runs every SCAN_INTERVAL_SEC seconds.
        """
        while self._running:
            try:
                markets = await self.fetch_active_markets(limit=200)
                log.debug(f"Poll: refreshed {len(markets)} markets")
                for md in markets:
                    for cb in self._callbacks:
                        try:
                            asyncio.create_task(cb(md))
                        except Exception:
                            pass
            except Exception as e:
                log.error(f"Poll error: {e}")
            await asyncio.sleep(settings.SCAN_INTERVAL_SEC)

    # ── Utility ───────────────────────────────

    def get_priority_markets(self) -> List[MarketData]:
        """Return markets in priority categories, sorted by volume."""
        return sorted(
            [m for m in self.markets.values()
             if m.category in self.PRIORITY_CATEGORIES and m.is_active and not m.stale],
            key=lambda m: m.volume_24h,
            reverse=True,
        )

    def get_all_active(self) -> List[MarketData]:
        return [m for m in self.markets.values() if m.is_active and not m.stale]

    def get_market(self, market_id: str) -> Optional[MarketData]:
        return self.markets.get(market_id)
