"""
executor.py — Order Execution Engine.
Handles all order placement, position monitoring, partial exits, and stop losses.
Operates in three modes:
  - dryrun: Logs orders only, no real execution
  - paper:  Simulates execution with virtual balance
  - live:   Real orders via py-clob-client-v2

SAFETY: Every order double-checked by risk_manager before placement.
Always uses MAKER (limit) orders to get zero fees.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, Dict
from dataclasses import dataclass

from config import settings, BotMode
from modules.database import Database, Trade
from modules.portfolio_tracker import PortfolioTracker, Position
from modules.risk_manager import RiskManager
from modules.telegram_notifier import TelegramNotifier
from modules.logger import decision_log

log = logging.getLogger("executor")


@dataclass
class OrderRequest:
    market_id: str
    question: str
    strategy: str       # arb | copy | signal
    side: str           # YES | NO
    entry_price: float  # Maker limit price
    estimated_edge: float
    win_probability: float
    source_reasoning: str


class Executor:
    """
    Unified order execution engine.
    Wraps CLOB client for live, simulates for dry/paper.
    All trades tracked in DB and portfolio.
    """

    def __init__(
        self,
        portfolio: PortfolioTracker,
        risk_manager: RiskManager,
        notifier: TelegramNotifier,
    ):
        self.portfolio = portfolio
        self.risk = risk_manager
        self.notifier = notifier
        self._db: Optional[Database] = None
        self._clob_client = None           # Initialized only in live mode
        self._pending_exits: Dict[int, dict] = {}  # trade_id → exit tracking state
        self._mode = settings.mode
        self._exits_lock = asyncio.Lock()

    async def initialize(self):
        self._db = await Database.get()

        if settings.is_live():
            await self._init_clob_client()

        log.info(f"Executor initialized | mode={self._mode.value}")

    async def _init_clob_client(self):
        """Initialize py-clob-client-v2 for live trading."""
        try:
            from py_clob_client.client import ClobClient
            from py_clob_client.clob_types import ApiCreds

            creds = ApiCreds(
                api_key=settings.clob_api_key,
                api_secret=settings.clob_secret,
                api_passphrase=settings.clob_passphrase,
            )
            self._clob_client = ClobClient(
                host=settings.clob_host,
                chain_id=137,           # Polygon mainnet
                private_key=settings.private_key,
                creds=creds,
            )
            log.info("CLOB client initialized for LIVE trading.")
        except ImportError:
            log.critical("py-clob-client not installed! Cannot run in live mode.")
            raise
        except Exception as e:
            log.critical(f"CLOB client init failed: {type(e).__name__}")
            raise

    # ── Main Entry Point ──────────────────────

    async def execute_order(self, req: OrderRequest) -> Optional[int]:
        """
        Execute an order request. Returns trade_id if successful, None if rejected.
        Runs the full pipeline:
          validate → risk_approve → size → place → record → notify
        """
        # 1. Risk approval + sizing
        approved, final_usd, reason = await self.risk.approve(
            market_id=req.market_id,
            strategy=req.strategy,
            estimated_edge=req.estimated_edge,
            win_probability=req.win_probability,
            proposed_usd=self.portfolio.balance * settings.max_risk_per_trade_pct,
        )

        if not approved:
            decision_log.log_signal_rejected(req.market_id, reason, {
                "strategy": req.strategy,
                "edge": req.estimated_edge,
            })
            return None

        # 2. Calculate shares
        shares = self._price_to_shares(final_usd, req.entry_price)
        if shares < settings.MIN_SHARES:
            decision_log.log_order_skipped(
                req.market_id,
                f"Shares {shares:.2f} < min {settings.MIN_SHARES}"
            )
            return None

        # 3. Execute based on mode
        order_id = None
        actual_price = req.entry_price
        actual_usd = final_usd

        if self._mode == BotMode.LIVE:
            order_id, actual_price, actual_usd = await self._place_live_order(
                req, shares, final_usd
            )
            if order_id is None:
                return None
        elif self._mode == BotMode.PAPER:
            order_id = f"PAPER_{datetime.utcnow().timestamp():.0f}"
            log.info(f"📄 PAPER order | {req.side} {req.market_id[:30]} | ${actual_usd:.4f}")
        else:  # DRYRUN
            order_id = f"DRY_{datetime.utcnow().timestamp():.0f}"
            log.info(f"🔍 DRYRUN order | {req.side} {req.market_id[:30]} | ${actual_usd:.4f}")

        # 4. Record in DB
        trade = Trade(
            id=None,
            timestamp=datetime.utcnow().isoformat(),
            mode=self._mode.value,
            strategy=req.strategy,
            market_id=req.market_id,
            market_question=req.question,
            side=req.side,
            price=actual_price,
            shares=shares,
            usd_amount=actual_usd,
            estimated_edge=req.estimated_edge,
            status="open",
            exit_price=None,
            pnl_usd=None,
            exit_reason=None,
            order_id=order_id,
            meta=req.source_reasoning[:500],
        )
        trade_id = await self._db.insert_trade(trade)

        # 5. Update portfolio
        await self.portfolio.open_position(
            trade_id=trade_id,
            market_id=req.market_id,
            question=req.question,
            strategy=req.strategy,
            side=req.side,
            price=actual_price,
            shares=shares,
            cost_usd=actual_usd,
        )

        # 6. Log + notify
        decision_log.log_signal_accepted(
            req.market_id, req.strategy, req.estimated_edge,
            {"side": req.side, "usd": actual_usd, "shares": shares}
        )
        decision_log.log_order_placed(
            req.market_id, req.side, actual_price,
            shares, actual_usd, self._mode.value
        )
        self.notifier.notify_trade(
            req.strategy, req.question[:60], req.side,
            actual_price, actual_usd, req.estimated_edge, self._mode.value
        )

        log.info(
            f"Order executed | id={trade_id} | {req.side} @ {actual_price:.4f} "
            f"| ${actual_usd:.4f} | {req.strategy}"
        )
        return trade_id

    async def _place_live_order(
        self, req: OrderRequest, shares: float, usd: float
    ) -> tuple[Optional[str], float, float]:
        """Place a real maker limit order via CLOB client."""
        try:
            from py_clob_client.clob_types import (
                OrderArgs, OrderType, MarketOrderArgs
            )

            # Build limit (maker) order
            order_args = OrderArgs(
                token_id=req.market_id,
                price=req.entry_price,
                size=shares,
                side=req.side,
            )

            # Verify balance first
            balance = await self._get_live_balance()
            if balance < usd:
                log.error(f"Insufficient live balance: ${balance:.4f} < ${usd:.4f}")
                return None, req.entry_price, usd

            resp = self._clob_client.create_limit_order(order_args)
            order_id = resp.get("orderID") or resp.get("id")
            fill_price = float(resp.get("price", req.entry_price))
            fill_size = float(resp.get("size", shares))
            fill_usd = fill_price * fill_size

            log.info(f"Live order placed | id={order_id} | fill={fill_price:.4f}")
            self.risk.record_api_success()
            return order_id, fill_price, fill_usd

        except Exception as e:
            log.error(f"Live order failed: {e}")
            self.risk.record_api_error()
            return None, req.entry_price, usd

    # ── Position Monitoring + Exit Logic ──────

    async def monitor_positions(self, current_prices: Dict[str, float]):
        """
        Run every scan cycle. Check all open positions for:
        - Partial exit targets (30% profit → exit 45%)
        - Stop loss (-50% of position value)
        - Trailing stop (15% retreat from peak)
        - Market resolution (price hits 0.99 or 0.01)
        """
        self.portfolio.update_mark_prices(current_prices)

        for trade_id, pos in list(self.portfolio.open_positions.items()):
            current_price = current_prices.get(pos.market_id)
            if current_price is None:
                continue

            # Track peak price for trailing stop
            async with self._exits_lock:
                state = self._pending_exits.setdefault(trade_id, {
                    "peak_price": pos.entry_price,
                    "partial_done": False,
                })

                if pos.side == "YES":
                    state["peak_price"] = max(state["peak_price"], current_price)
                else:
                    state["peak_price"] = min(state["peak_price"], current_price)
                
                # Copy state to avoid holding lock during exits
                state_copy = state.copy()

            # --- Check exit conditions ---

            # 1. Near full resolution (95% confident outcome)
            if current_price >= 0.97 and pos.side == "YES":
                await self._exit_position(trade_id, pos, current_price, "near_resolution_YES")
                continue
            if current_price <= 0.03 and pos.side == "NO":
                await self._exit_position(trade_id, pos, 1.0 - current_price, "near_resolution_NO")
                continue

            # 2. Stop loss
            if self.risk.should_stop_loss(pos.entry_price, current_price, pos.side):
                await self._exit_position(trade_id, pos, current_price, "stop_loss")
                continue

            # 3. Trailing stop
            if self.risk.trailing_stop_triggered(
                state_copy["peak_price"], current_price, pos.side
            ):
                await self._exit_position(trade_id, pos, current_price, "trailing_stop")
                continue

            # 4. Partial exit at +30% profit
            if (not state_copy["partial_done"] and
                    self.risk.should_partial_exit(pos.entry_price, current_price, pos.side)):
                await self._partial_exit(trade_id, pos, current_price, state_copy)
                async with self._exits_lock:
                    if trade_id in self._pending_exits:
                        self._pending_exits[trade_id]["partial_done"] = True

    async def _exit_position(self, trade_id: int, pos: Position, exit_price: float, reason: str):
        """Full exit of a position."""
        if pos.side == "YES":
            pnl = (exit_price - pos.entry_price) * pos.shares
        else:
            pnl = (pos.entry_price - exit_price) * pos.shares

        if self._mode == BotMode.LIVE:
            success = await self._place_live_exit(pos, exit_price)
            if not success:
                log.error(f"Live exit failed for trade {trade_id}")
                return

        await self.portfolio.close_position(trade_id, exit_price, pnl, reason)
        self.risk.record_trade_result(win=pnl > 0)
        async with self._exits_lock:
            self._pending_exits.pop(trade_id, None)

        decision_log.log_exit(trade_id, pnl, reason, self._mode.value)
        self.notifier.notify_exit(pos.market_question[:60], pnl, reason, self._mode.value)

    async def _partial_exit(self, trade_id: int, pos: Position, exit_price: float, state: dict):
        """Exit partial_exit_pct of position at profit target."""
        exit_shares = self.risk.compute_partial_exit_size(pos.shares)
        if exit_shares < 1.0:
            return

        if pos.side == "YES":
            partial_pnl = (exit_price - pos.entry_price) * exit_shares
        else:
            partial_pnl = (pos.entry_price - exit_price) * exit_shares

        if self._mode == BotMode.LIVE:
            await self._place_live_exit(pos, exit_price, shares=exit_shares)

        # Update position (reduce shares, don't close fully)
        pos.shares -= exit_shares
        pos.cost_usd *= (1 - settings.profit_partial_exit_pct)

        log.info(
            f"Partial exit | trade={trade_id} | shares={exit_shares:.2f} | "
            f"pnl=${partial_pnl:+.4f} | remaining={pos.shares:.2f}"
        )
        self.notifier.notify_exit(
            pos.market_question[:60],
            partial_pnl,
            f"partial_exit (45% off @ +30%)",
            self._mode.value,
        )

    async def _place_live_exit(self, pos: Position, exit_price: float, shares: float = None) -> bool:
        """Place a live sell/exit order."""
        try:
            from py_clob_client.clob_types import OrderArgs
            exit_shares = shares or pos.shares
            # Sell the opposite side: if we own YES, sell YES
            order_args = OrderArgs(
                token_id=pos.market_id,
                price=exit_price,
                size=exit_shares,
                side=pos.side,   # Sell our held side
            )
            self._clob_client.create_limit_order(order_args)
            self.risk.record_api_success()
            return True
        except Exception as e:
            log.error(f"Live exit order failed: {e}")
            self.risk.record_api_error()
            return False

    async def _get_live_balance(self) -> float:
        """Fetch real USDC balance from CLOB."""
        try:
            bal_data = self._clob_client.get_balance()
            return float(bal_data.get("balance", 0))
        except Exception as e:
            log.error(f"Balance fetch failed: {e}")
            return 0.0

    # ── Helpers ───────────────────────────────

    @staticmethod
    def _price_to_shares(usd_amount: float, price: float) -> float:
        """Convert USD to shares at given price."""
        if price <= 0 or price >= 1:
            return 0.0
        return round(usd_amount / price, 2)

    async def emergency_close_all(self, reason: str = "emergency"):
        """Close all open positions immediately. Use only in critical situations."""
        log.critical(f"EMERGENCY CLOSE ALL: {reason}")
        for trade_id, pos in list(self.portfolio.open_positions.items()):
            # Use last known price or conservative estimate
            exit_price = pos.current_price if pos.current_price > 0 else pos.entry_price * 0.9
            await self._exit_position(trade_id, pos, exit_price, f"emergency: {reason}")
