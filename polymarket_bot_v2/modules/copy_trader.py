"""
copy_trader.py v2 — Copy Trading Engine with Sharpe-ratio wallet scoring.

Improvements over v1:
  - Sharpe ratio: risk-adjusted return (not just win rate)
  - Exponential recency weighting: recent trades matter 3x more than old ones
  - Category specialization score: wallets that dominate specific niches score higher
  - Anti-correlation guard: avoid copying 2 wallets into the same active market
  - Slippage guard: only enter if price moved <3% since wallet entered (not 5%)
  - Smart position sizing: copy proportionally to wallet's own position size
  - Minimum trade history: 45 trades required (up from 30) for statistical confidence
"""

import asyncio
import logging
import math
import time
from typing import Dict, List, Optional, Set, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timedelta

import aiohttp
from tenacity import retry, stop_after_attempt, wait_exponential

from config import settings
from modules.database import Database, TrackedWallet
from modules.scanner import MarketData
from modules.logger import decision_log

log = logging.getLogger("copy_trader")


@dataclass
class WalletProfile:
    address: str
    win_rate: float
    total_trades: int
    profit_trades: int
    avg_profit_pct: float
    max_drawdown: float
    recent_markets: List[str]
    preferred_categories: List[str]
    avg_position_size_usd: float
    last_trade_time: Optional[float]
    # v2 additions
    sharpe_ratio: float = 0.0         # Risk-adjusted return
    category_depth: Dict[str, float] = field(default_factory=dict)  # cat → win_rate
    recent_win_rate: float = 0.0      # Last-30-days win rate (recency weighted)
    score: float = 0.0

    @property
    def is_qualified(self) -> bool:
        return (
            self.win_rate >= settings.min_wallet_win_rate
            and self.total_trades >= 45            # v2: raised from 30
            and self.max_drawdown <= settings.max_wallet_drawdown
            and self.avg_position_size_usd >= settings.MIN_ORDER_SIZE_USD
            and self.last_trade_time is not None
            and (time.time() - self.last_trade_time) < 7 * 24 * 3600
            and self.sharpe_ratio >= 0.5           # v2: must be risk-adjusted profitable
        )

    def compute_score(self) -> float:
        """
        v2 Composite score with Sharpe ratio weighting.

        Weights:
          35% — recent win rate (last 30 days, exponentially weighted)
          25% — Sharpe ratio (risk-adjusted return, capped at 3.0)
          20% — low drawdown penalty
          10% — trade count (experience)
          10% — recency (traded within 7 days)
        """
        recent_score   = self.recent_win_rate * 35
        sharpe_score   = min(self.sharpe_ratio / 3.0, 1.0) * 25
        dd_score       = (1.0 - self.max_drawdown) * 20
        trade_score    = min(self.total_trades / 300, 1.0) * 10
        recency_score  = 0.0
        if self.last_trade_time:
            days_ago = (time.time() - self.last_trade_time) / 86400
            recency_score = max(0, 1 - days_ago / 7) * 10

        self.score = recent_score + sharpe_score + dd_score + trade_score + recency_score
        return self.score

    @staticmethod
    def compute_sharpe(returns: List[float], risk_free: float = 0.0) -> float:
        """
        Compute annualised Sharpe ratio from a list of per-trade returns.
        Returns 0 if fewer than 5 trades or zero variance.
        """
        if len(returns) < 5:
            return 0.0
        n = len(returns)
        mean = sum(returns) / n
        variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
        std = math.sqrt(variance) if variance > 0 else 0.0
        if std == 0:
            return 0.0
        # Annualise: assume ~4 trades/week
        return ((mean - risk_free) / std) * math.sqrt(4 * 52)

    @staticmethod
    def exponential_win_rate(trades: List[dict], half_life_days: int = 14) -> float:
        """
        Recency-weighted win rate.
        Trades from `half_life_days` ago get 0.5 weight, older get less.
        """
        now = time.time()
        total_weight = 0.0
        weighted_wins = 0.0
        for t in trades:
            age_days = (now - t.get("timestamp", now)) / 86400
            weight = math.exp(-math.log(2) * age_days / half_life_days)
            total_weight += weight
            if t.get("profitable"):
                weighted_wins += weight
        if total_weight == 0:
            return 0.0
        return weighted_wins / total_weight


@dataclass
class CopySignal:
    source_wallet: str
    wallet_score: float              # v2: include wallet quality in signal
    market_id: str
    market_question: str
    side: str
    wallet_entry_price: float
    current_market_price: float
    wallet_win_rate: float
    slippage_pct: float
    edge_estimate: float
    suggested_usd: float             # v2: proportional to wallet's own position
    reasoning: str

    @property
    def is_still_valid(self) -> bool:
        return (
            self.slippage_pct <= 0.03               # v2: tightened from 5% → 3%
            and self.edge_estimate >= settings.min_edge_threshold
        )


class CopyTrader:
    LEADERBOARD_URL   = "https://data-api.polymarket.com/leaderboard"
    WALLET_TRADES_URL = "https://data-api.polymarket.com/activity"

    def __init__(self):
        self._db: Optional[Database] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._tracked_wallets: Dict[str, WalletProfile] = {}
        self._active_copy_markets: Dict[str, str] = {}   # market_id → wallet we're copying
        self._known_positions: Dict[str, Set[str]] = {}
        self._last_leaderboard_refresh = 0.0
        self._running = False

    async def initialize(self, session: aiohttp.ClientSession):
        self._db = await Database.get()
        self._session = session
        saved = await self._db.get_active_wallets()
        for w in saved:
            profile = WalletProfile(
                address=w["address"],
                win_rate=w["win_rate"],
                total_trades=w["total_trades"],
                profit_trades=int(w["total_trades"] * w["win_rate"]),
                avg_profit_pct=w["avg_profit_per_trade"],
                max_drawdown=w["max_drawdown"],
                recent_markets=[],
                preferred_categories=[],
                avg_position_size_usd=w.get("avg_position_size_usd", 10.0),
                last_trade_time=w.get("last_trade_time"),
                sharpe_ratio=w.get("sharpe_ratio", 0.5),
                recent_win_rate=w.get("recent_win_rate", w["win_rate"]),
            )
            profile.compute_score()
            self._tracked_wallets[w["address"]] = profile
        log.info("CopyTrader v2 initialized with %d saved wallets", len(self._tracked_wallets))

    # ── Leaderboard refresh ───────────────────

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=20))
    async def refresh_leaderboard(self) -> List[WalletProfile]:
        """
        Fetch top wallets from Polymarket leaderboard.
        Refresh every 6 hours (leaderboard doesn't update faster).
        """
        now = time.time()
        if (now - self._last_leaderboard_refresh) < 21600:
            return list(self._tracked_wallets.values())

        try:
            params = {"limit": 50, "window": "all"}
            async with self._session.get(
                self.LEADERBOARD_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    log.warning("Leaderboard API returned %d", resp.status)
                    return list(self._tracked_wallets.values())
                data = await resp.json()

            wallets = data if isinstance(data, list) else data.get("data", [])
            profiles = []

            for w in wallets[:30]:
                addr = w.get("proxyWalletAddress") or w.get("address", "")
                if not addr:
                    continue

                # Fetch trade history for Sharpe calculation
                trades = await self._fetch_trade_history(addr, limit=100)
                returns = [t.get("profit_pct", 0.0) for t in trades]
                sharpe = WalletProfile.compute_sharpe(returns)
                recent_wr = WalletProfile.exponential_win_rate(trades)

                # Category specialization
                cat_wins: Dict[str, List[bool]] = {}
                for t in trades:
                    cat = t.get("category", "Other")
                    cat_wins.setdefault(cat, []).append(t.get("profitable", False))
                category_depth = {
                    cat: sum(v) / len(v)
                    for cat, v in cat_wins.items() if len(v) >= 5
                }

                profile = WalletProfile(
                    address=addr,
                    win_rate=float(w.get("profitableTradePercentage", 0)),
                    total_trades=int(w.get("tradesCount", 0)),
                    profit_trades=int(w.get("profitableTradesCount", 0)),
                    avg_profit_pct=float(w.get("averageProfit", 0)),
                    max_drawdown=float(w.get("maxDrawdown", 0.5)),
                    recent_markets=[],
                    preferred_categories=list(category_depth.keys()),
                    avg_position_size_usd=float(w.get("averagePositionSize", 10.0)),
                    last_trade_time=None,
                    sharpe_ratio=sharpe,
                    category_depth=category_depth,
                    recent_win_rate=recent_wr,
                )
                profile.compute_score()

                if profile.is_qualified:
                    profiles.append(profile)
                    self._tracked_wallets[addr] = profile
                    await self._db.upsert_wallet(TrackedWallet(
                        address=addr,
                        win_rate=profile.win_rate,
                        total_trades=profile.total_trades,
                        avg_profit_per_trade=profile.avg_profit_pct,
                        max_drawdown=profile.max_drawdown,
                        last_trade_time=profile.last_trade_time,
                    ))

            profiles.sort(key=lambda p: p.score, reverse=True)
            top = profiles[:settings.max_wallets_to_track]
            log.info(
                "Leaderboard refreshed: %d qualified wallets | Top score=%.1f",
                len(top), top[0].score if top else 0,
            )
            self._last_leaderboard_refresh = now
            return top

        except Exception as e:
            log.error("Leaderboard refresh failed: %s", type(e).__name__)
            return list(self._tracked_wallets.values())

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=2, max=15))
    async def _fetch_trade_history(self, address: str, limit: int = 100) -> List[dict]:
        try:
            params = {"user": address, "limit": limit}
            async with self._session.get(
                self.WALLET_TRADES_URL, params=params,
                timeout=aiohttp.ClientTimeout(total=10),
            ) as resp:
                if resp.status != 200:
                    return []
                raw = await resp.json()
                trades = raw if isinstance(raw, list) else raw.get("data", [])
                return [
                    {
                        "market_id": t.get("market") or t.get("conditionId", ""),
                        "side":      t.get("side", "YES"),
                        "price":     float(t.get("price", 0)),
                        "size_usd":  float(t.get("usdcSize", 0)),
                        "timestamp": float(t.get("timestamp", time.time())),
                        "profitable": float(t.get("profit", 0)) > 0,
                        "profit_pct": float(t.get("profit", 0)) / max(float(t.get("usdcSize", 1)), 1),
                        "category":  t.get("category", "Other"),
                    }
                    for t in trades
                ]
        except Exception:
            return []

    # ── Signal generation ─────────────────────

    async def scan_for_signals(
        self, markets: Dict[str, MarketData]
    ) -> List[CopySignal]:
        """
        Detect new positions opened by top wallets.
        Anti-correlation guard: skip if we already have another wallet in same market.
        """
        await self.refresh_leaderboard()

        top_wallets = sorted(
            [p for p in self._tracked_wallets.values() if p.is_qualified],
            key=lambda p: p.score, reverse=True,
        )[:settings.max_wallets_to_track]

        signals = []
        for wallet in top_wallets:
            new_trades = await self._detect_new_positions(wallet)
            for trade in new_trades:
                mid = trade["market_id"]

                # Anti-correlation guard (v2): don't double up on a market
                if mid in self._active_copy_markets:
                    log.debug(
                        "Skipping %s — already copying via %s",
                        mid, self._active_copy_markets[mid],
                    )
                    continue

                md = markets.get(mid)
                if not md:
                    continue

                # Slippage: how much has price moved since wallet entered?
                wallet_price = trade["price"]
                current_price = md.yes_price if trade["side"] == "YES" else md.no_price
                slippage = abs(current_price - wallet_price) / max(wallet_price, 0.001)

                # Edge estimate: weighted by wallet's category specialization
                base_edge = wallet.win_rate
                cat_bonus = wallet.category_depth.get(md.category, 0.0)
                if cat_bonus > wallet.win_rate:
                    # Wallet is a specialist in this category
                    base_edge = (base_edge + cat_bonus) / 2

                edge = base_edge

                # Proportional sizing: copy a fraction of wallet's position
                copy_usd = min(
                    trade["size_usd"] * 0.10,        # 10% of wallet's position
                    settings.starting_capital * settings.copy_weight * 0.20,  # 20% of copy budget
                )
                copy_usd = max(copy_usd, settings.MIN_ORDER_SIZE_USD)

                signal = CopySignal(
                    source_wallet=wallet.address,
                    wallet_score=wallet.score,
                    market_id=mid,
                    market_question=md.question,
                    side=trade["side"],
                    wallet_entry_price=wallet_price,
                    current_market_price=current_price,
                    wallet_win_rate=wallet.win_rate,
                    slippage_pct=slippage,
                    edge_estimate=edge,
                    suggested_usd=copy_usd,
                    reasoning=(
                        f"Copy wallet={wallet.address[:8]}... score={wallet.score:.1f} "
                        f"wr={wallet.win_rate:.1%} sharpe={wallet.sharpe_ratio:.2f} "
                        f"cat_spec={cat_bonus:.1%} slip={slippage:.2%}"
                    ),
                )

                if signal.is_still_valid:
                    signals.append(signal)
                    self._active_copy_markets[mid] = wallet.address

        signals.sort(key=lambda s: s.wallet_score, reverse=True)
        if signals:
            log.info("Copy signals: %d valid | Top wallet score=%.1f",
                     len(signals), signals[0].wallet_score)
        return signals

    # Backwards-compatible wrapper for older callers
    async def scan_for_copy_signals(self, markets: Dict[str, MarketData]) -> List[CopySignal]:
        """Compatibility shim for `scan_for_copy_signals` used by main.py in older versions."""
        return await self.scan_for_signals(markets)

    async def _detect_new_positions(self, wallet: WalletProfile) -> List[dict]:
        """Detect markets newly entered since last scan."""
        trades = await self._fetch_trade_history(wallet.address, limit=20)
        known = self._known_positions.get(wallet.address, set())
        new_trades = []
        for t in trades:
            mid = t["market_id"]
            # New position: not seen before and within last 10 minutes
            if mid not in known and (time.time() - t["timestamp"]) < 600:
                new_trades.append(t)
                known.add(mid)
        self._known_positions[wallet.address] = known
        return new_trades

    def release_market(self, market_id: str):
        """Call when position is closed so market can be re-entered by another wallet."""
        self._active_copy_markets.pop(market_id, None)
