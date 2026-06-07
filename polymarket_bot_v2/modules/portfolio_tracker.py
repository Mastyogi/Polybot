"""
portfolio_tracker.py — Real-time portfolio state management.
Tracks balance, open positions, daily P&L, and enforces hard capital limits.
The single source of truth for "how much can I deploy right now?"
"""

import asyncio
import logging
from datetime import datetime, date, timezone
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field

from config import settings, BotMode
from modules.database import Database, DailySnapshot

log = logging.getLogger("portfolio")


@dataclass
class Position:
    trade_id: int
    market_id: str
    market_question: str
    strategy: str
    side: str          # YES | NO
    entry_price: float
    shares: float
    cost_usd: float
    entry_time: str
    current_price: float = 0.0
    unrealized_pnl: float = 0.0
    partial_exit_done: bool = False


class PortfolioTracker:
    """
    Manages all capital accounting and position state.
    Thread-safe via asyncio lock.
    """

    def __init__(self, starting_capital: float):
        self.starting_capital = starting_capital
        self.balance = starting_capital          # Virtual / real USDC balance
        self.peak_balance = starting_capital     # For drawdown calculation
        self.day_start_balance = starting_capital
        self.realized_pnl_today = 0.0
        self.open_positions: Dict[int, Position] = {}  # trade_id → Position
        self._lock = asyncio.Lock()
        self._db: Optional[Database] = None

    async def initialize(self):
        self._db = await Database.get()
        # Restore state from DB
        saved_balance = await self._db.get_state("balance", self.starting_capital)
        saved_peak = await self._db.get_state("peak_balance", self.starting_capital)
        self.balance = saved_balance
        self.peak_balance = saved_peak

        # Restore today's realized PnL
        trades_today = await self._db.get_trades_today()
        self.realized_pnl_today = sum(
            t["pnl_usd"] for t in trades_today
            if t["status"] == "closed" and t["pnl_usd"] is not None
        )

        # Restore open positions
        open_trades = await self._db.get_open_trades()
        for t in open_trades:
            self.open_positions[t["id"]] = Position(
                trade_id=t["id"],
                market_id=t["market_id"],
                market_question=t["market_question"] or "",
                strategy=t["strategy"],
                side=t["side"],
                entry_price=t["price"],
                shares=t["shares"],
                cost_usd=t["usd_amount"],
                entry_time=t["timestamp"],
            )

        log.info(
            f"Portfolio restored | balance=${self.balance:.4f} | "
            f"positions={len(self.open_positions)} | pnl_today=${self.realized_pnl_today:+.4f}"
        )

    # ── Capital Queries ───────────────────────

    @property
    def deployed_usd(self) -> float:
        """Total USD locked in open positions."""
        return sum(p.cost_usd for p in self.open_positions.values())

    @property
    def free_usd(self) -> float:
        """USD available to trade."""
        return max(0.0, self.balance - self.deployed_usd)

    @property
    def deployed_pct(self) -> float:
        if self.balance <= 0:
            return 1.0
        return self.deployed_usd / self.balance

    @property
    def drawdown_pct(self) -> float:
        """Current drawdown from peak balance."""
        if self.peak_balance <= 0:
            return 0.0
        return (self.peak_balance - self.balance) / self.peak_balance

    @property
    def daily_loss_pct(self) -> float:
        """Today's P&L as % of day-start balance."""
        if self.day_start_balance <= 0:
            return 0.0
        return self.realized_pnl_today / self.day_start_balance

    @property
    def unrealized_pnl(self) -> float:
        return sum(p.unrealized_pnl for p in self.open_positions.values())

    def can_deploy(self, amount_usd: float) -> Tuple[bool, str]:
        """
        Hard gating: checks all capital rules before allowing deployment.
        Returns (ok, reason).
        """
        if amount_usd < settings.MIN_ORDER_SIZE_USD:
            return False, f"Amount ${amount_usd:.4f} below min ${settings.MIN_ORDER_SIZE_USD}"

        if self.free_usd < amount_usd:
            return False, f"Insufficient free capital: ${self.free_usd:.4f} < ${amount_usd:.4f}"

        max_deploying = self.balance * settings.max_capital_deployed_pct
        if self.deployed_usd + amount_usd > max_deploying:
            return False, (
                f"Would exceed max deployed ({settings.max_capital_deployed_pct:.0%}): "
                f"${self.deployed_usd + amount_usd:.4f} > ${max_deploying:.4f}"
            )

        if self.drawdown_pct >= settings.drawdown_circuit_breaker:
            return False, f"Circuit breaker: drawdown {self.drawdown_pct:.2%}"

        if self.daily_loss_pct <= -settings.max_daily_loss_pct:
            return False, f"Daily loss limit hit: {self.daily_loss_pct:.2%}"

        return True, "OK"

    # ── Position Management ───────────────────

    async def open_position(self, trade_id: int, market_id: str, question: str,
                             strategy: str, side: str, price: float,
                             shares: float, cost_usd: float):
        async with self._lock:
            pos = Position(
                trade_id=trade_id,
                market_id=market_id,
                market_question=question,
                strategy=strategy,
                side=side,
                entry_price=price,
                shares=shares,
                cost_usd=cost_usd,
                entry_time=datetime.now(timezone.utc).isoformat(),
            )
            self.open_positions[trade_id] = pos
            # Balance is reserved (not deducted until exit resolves)
            await self._persist_balance()
            log.info(
                f"Position opened | id={trade_id} | {side} {market_id[:30]} | "
                f"${cost_usd:.4f} @ {price:.4f}"
            )

    async def close_position(self, trade_id: int, exit_price: float, pnl: float, reason: str):
        async with self._lock:
            if trade_id not in self.open_positions:
                log.warning(f"Tried to close unknown position {trade_id}")
                return

            pos = self.open_positions.pop(trade_id)
            self.balance += pnl  # Realize P&L
            self.realized_pnl_today += pnl

            if self.balance > self.peak_balance:
                self.peak_balance = self.balance

            await self._persist_balance()
            await self._db.close_trade(trade_id, exit_price, pnl, reason)

            log.info(
                f"Position closed | id={trade_id} | pnl=${pnl:+.4f} | "
                f"new_balance=${self.balance:.4f}"
            )

    def update_mark_prices(self, prices: Dict[str, float]):
        """Update unrealized P&L with latest market prices."""
        for pos in self.open_positions.values():
            market_price = prices.get(pos.market_id)
            if market_price is not None:
                pos.current_price = market_price
                if pos.side == "YES":
                    pos.unrealized_pnl = (market_price - pos.entry_price) * pos.shares
                else:  # NO
                    pos.unrealized_pnl = (pos.entry_price - market_price) * pos.shares

    def new_day(self):
        """Call at midnight UTC to reset daily counters."""
        self.day_start_balance = self.balance
        self.realized_pnl_today = 0.0
        log.info(f"New day started | balance=${self.balance:.4f}")

    # ── Stats ─────────────────────────────────

    async def get_stats(self) -> dict:
        trades = await self._db.get_all_closed_trades()
        wins = [t for t in trades if t.get("pnl_usd", 0) > 0]
        total_pnl = sum(t.get("pnl_usd", 0) for t in trades)
        win_rate = len(wins) / len(trades) if trades else 0.0

        return {
            "balance": self.balance,
            "starting_capital": self.starting_capital,
            "peak_balance": self.peak_balance,
            "total_pnl": total_pnl,
            "total_return_pct": (self.balance - self.starting_capital) / self.starting_capital,
            "win_rate": win_rate,
            "total_trades": len(trades),
            "open_positions": len(self.open_positions),
            "deployed_usd": self.deployed_usd,
            "free_usd": self.free_usd,
            "deployed_pct": self.deployed_pct,
            "drawdown_pct": self.drawdown_pct,
            "daily_pnl": self.realized_pnl_today,
            "daily_loss_pct": self.daily_loss_pct,
        }

    async def save_daily_snapshot(self):
        """Persist today's snapshot to DB."""
        trades_today = await self._db.get_trades_today()
        closed_today = [t for t in trades_today if t["status"] == "closed"]
        wins_today = [t for t in closed_today if (t.get("pnl_usd") or 0) > 0]

        snap = DailySnapshot(
            id=None,
            snapshot_date=datetime.now(timezone.utc).date().isoformat(),
            balance_usd=self.balance,
            open_positions_count=len(self.open_positions),
            realized_pnl_today=self.realized_pnl_today,
            unrealized_pnl=self.unrealized_pnl,
            total_trades_today=len(closed_today),
            win_trades_today=len(wins_today),
            daily_return_pct=self.daily_loss_pct,
        )
        await self._db.upsert_snapshot(snap)

    async def _persist_balance(self):
        await self._db.set_state("balance", self.balance)
        await self._db.set_state("peak_balance", self.peak_balance)
