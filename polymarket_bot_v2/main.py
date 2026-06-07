"""
main.py — PolyBot Main Orchestrator.
Wires all modules together and runs 5 parallel async loops:
  1. Market scan loop (WebSocket + REST fallback)
  2. Arb detection + execution loop
  3. Copy trading scan loop
  4. Signal generation + execution loop
  5. Position monitoring + exit loop
  6. Portfolio snapshot + daily summary loop
  7. Dashboard render loop

Usage:
  python main.py --mode dryrun --capital 10
  python main.py --mode paper  --capital 10
  python main.py --mode live   --capital 10   ← Real money! Review all config first.
  python main.py --reset-cb                    ← Manual circuit breaker reset
"""

import asyncio
import argparse
import logging
import os
import signal
import sys
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from typing import Optional

# ── Setup paths ─────────────────────────────
ROOT = Path(__file__).parent
sys.path.insert(0, str(ROOT))

from config import settings, BotMode
from modules.database import Database
from modules.logger import bot_log, decision_log, setup_logging
from modules.portfolio_tracker import PortfolioTracker
from modules.risk_manager import RiskManager
from modules.scanner import MarketScanner, MarketData
from modules.arb_detector import ArbDetector, ArbOpportunity
from modules.copy_trader import CopyTrader, CopySignal
from modules.signal_generator import SignalGenerator, TradingSignal
from modules.executor import Executor, OrderRequest
from modules.telegram_notifier import TelegramNotifier
from dashboard.web_dashboard import dashboard_instance

log = logging.getLogger("main")


class PolyBot:
    """
    Top-level bot orchestrator.
    All async tasks managed here. Clean startup/shutdown lifecycle.
    """

    def __init__(self, capital: float, mode: BotMode):
        self.capital = capital
        settings.override(mode=mode, starting_capital=capital)

        # Core modules
        self.portfolio    = PortfolioTracker(starting_capital=capital)
        self.risk         = RiskManager(self.portfolio)
        self.notifier     = TelegramNotifier()
        self.scanner      = MarketScanner()
        self.arb          = ArbDetector()
        self.copy_trader  = CopyTrader()
        self.signal_gen   = SignalGenerator()
        self.executor     = Executor(self.portfolio, self.risk, self.notifier)
        self.dashboard    = dashboard_instance

        self._tasks: list = []
        self._running = False
        self._current_prices: dict = {}    # market_id → current YES price

        # Register scanner callback for live price tracking
        self.scanner.on_update(self._on_market_update)

    # ── Lifecycle ─────────────────────────────

    async def start(self):
        """Initialize all modules and start async loops."""
        self.dashboard.print_startup_banner(self.capital, settings.mode.value)

        # Validate live mode has all keys
        if settings.is_live() and not settings.validate_live_keys():
            log.critical("Live mode requires all API keys configured in .env")
            sys.exit(1)

        # Initialize all modules
        await self.portfolio.initialize()
        await self.arb.initialize()
        await self.notifier.start()

        # Fetch initial market data
        bot_log.info("Fetching initial market data...")
        await self.scanner.start()
        await self.scanner.fetch_active_markets(limit=200)

        await self.executor.initialize()
        # Initialize copy trader with existing HTTP session
        await self.copy_trader.initialize(self.scanner._session)

        # Attach dashboard
        self.dashboard.attach(self.portfolio, self.risk)
        await self.dashboard.start(
            port=int(os.environ.get("DASHBOARD_PORT", "8765"))
        )

        self._running = True

        # Log startup
        stats = await self.portfolio.get_stats()
        self.notifier.notify_startup(settings.mode.value, self.capital)
        decision_log.log_balance(
            self.portfolio.balance,
            self.portfolio.deployed_usd,
            self.portfolio.free_usd,
        )

        bot_log.info(
            f"Bot started | mode={settings.mode.value} | "
            f"capital=${self.capital:.2f} | "
            f"restored_positions={len(self.portfolio.open_positions)}"
        )

        # Start all async loops
        self._tasks = [
            asyncio.create_task(self._ws_loop(),       name="ws_loop"),
            asyncio.create_task(self._arb_loop(),      name="arb_loop"),
            asyncio.create_task(self._copy_loop(),     name="copy_loop"),
            asyncio.create_task(self._signal_loop(),   name="signal_loop"),
            asyncio.create_task(self._monitor_loop(),  name="monitor_loop"),
            asyncio.create_task(self._snapshot_loop(), name="snapshot_loop"),
            asyncio.create_task(self._health_loop(),   name="health_loop"),
        ]

        # Run until stopped
        try:
            done, pending = await asyncio.wait(
                self._tasks,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for task in done:
                exc = task.exception()
                if exc and not isinstance(exc, asyncio.CancelledError):
                    log.critical(
                        "Task '%s' crashed: %s — initiating emergency shutdown",
                        task.get_name(),
                        type(exc).__name__,
                    )
                    await self.stop(f"task_crash:{task.get_name()}")
                    raise exc
        except asyncio.CancelledError:
            pass

    async def stop(self, reason: str = "manual"):
        """Graceful shutdown."""
        bot_log.info(f"Shutting down: {reason}")
        self._running = False

        for task in self._tasks:
            task.cancel()

        await asyncio.gather(*self._tasks, return_exceptions=True)
        await self.portfolio.save_daily_snapshot()
        await self.scanner.stop()
        await self.notifier.stop()
        await self.dashboard.stop()

        db = await Database.get()
        await db.close()

        bot_log.info("Bot shutdown complete.")

    # ── Scanner Callback ──────────────────────

    async def _on_market_update(self, md: MarketData):
        """Called on every market price update (WebSocket or REST)."""
        self._current_prices[md.market_id] = md.yes_price
        await self.scanner.set_market(md)

    # ── Loop 1: WebSocket Market Data ────────

    async def _ws_loop(self):
        """
        WebSocket subscription for top markets.
        Falls back to REST polling if WS fails.
        """
        while self._running:
            try:
                priority_markets = self.scanner.get_priority_markets()
                if priority_markets:
                    market_ids = [m.market_id for m in priority_markets[:50]]
                    try:
                        await asyncio.wait_for(
                            self.scanner.subscribe_ws(market_ids),
                            timeout=300,
                        )
                    except asyncio.TimeoutError:
                        log.warning("WS subscription timeout (300s) — reconnecting")
                        continue
                else:
                    # No WS data yet → fallback to polling
                    await self.scanner.poll_markets_loop()
            except Exception as e:
                log.error(f"WS loop error: {e}")
                await asyncio.sleep(10)

    # ── Loop 2: Arb Detection ─────────────────

    async def _arb_loop(self):
        """
        Primary strategy loop: scan for arb opportunities every N seconds.
        Executes on best opportunities that pass risk gate.
        """
        while self._running:
            try:
                if self.risk.is_paused():
                    await asyncio.sleep(30)
                    continue

                all_markets = self.scanner.get_all_active()
                opportunities = self.arb.scan(all_markets)

                for opp in opportunities:
                    if not opp.is_actionable():
                        await self.arb.log_arb_to_db(
                            opp, acted=False, skip_reason="below_threshold"
                        )
                        continue

                    # Check capital allocation limit for arb
                    arb_budget = self.portfolio.balance * settings.arb_weight
                    arb_deployed = sum(
                        p.cost_usd for p in self.portfolio.open_positions.values()
                        if p.strategy == "arb"
                    )
                    if arb_deployed >= arb_budget:
                        log.debug("Arb budget exhausted, skipping")
                        break

                    req = OrderRequest(
                        market_id=opp.market_id,
                        question=opp.question,
                        strategy="arb",
                        side=opp.suggested_side,
                        entry_price=opp.suggested_price,
                        estimated_edge=opp.edge_score,
                        win_probability=opp.confidence,
                        source_reasoning=opp.reasoning,
                    )

                    trade_id = await self.executor.execute_order(req)
                    if trade_id:
                        await self.arb.log_arb_to_db(opp, acted=True)
                        self.notifier.notify_arb(
                            opp.question, opp.yes_price, opp.no_price, opp.net_profit_pct
                        )
                        self.dashboard.add_event(
                            f"ARB: {opp.question[:40]} | {opp.net_profit_pct:.2%} profit"
                        )
                        # One arb per cycle — don't over-deploy
                        break

            except Exception as e:
                log.error(f"Arb loop error: {e}")
                self.risk.record_api_error()

            await asyncio.sleep(settings.SCAN_INTERVAL_SEC)

    # ── Loop 3: Copy Trading ──────────────────

    async def _copy_loop(self):
        """
        Secondary strategy: check tracked wallet activity periodically.
        """
        await asyncio.sleep(30)  # Give arb loop a head start

        while self._running:
            try:
                if self.risk.is_paused():
                    await asyncio.sleep(60)
                    continue

                # Refresh leaderboard every 6h
                await self.copy_trader.refresh_leaderboard()

                # Check capital allocation for copy
                copy_budget = self.portfolio.balance * settings.copy_weight
                copy_deployed = sum(
                    p.cost_usd for p in self.portfolio.open_positions.values()
                    if p.strategy == "copy"
                )
                if copy_deployed >= copy_budget:
                    await asyncio.sleep(settings.COPY_SCAN_INTERVAL_SEC)
                    continue

                markets = await self.scanner.get_markets_snapshot()
                market_data_dict = {mid: md for mid, md in markets.items()}
                signals = await self.copy_trader.scan_for_copy_signals(market_data_dict)

                for sig in signals:
                    if not sig.is_still_valid:
                        continue

                    req = OrderRequest(
                        market_id=sig.market_id,
                        question=sig.market_question,
                        strategy="copy",
                        side=sig.side,
                        entry_price=sig.current_market_price,
                        estimated_edge=sig.edge_estimate,
                        win_probability=sig.wallet_win_rate,
                        source_reasoning=sig.reasoning,
                    )

                    trade_id = await self.executor.execute_order(req)
                    if trade_id:
                        self.dashboard.add_event(
                            f"COPY: {sig.market_question[:40]} | {sig.side}"
                        )
                        break  # One copy trade per cycle

            except Exception as e:
                log.error(f"Copy loop error: {e}")

            await asyncio.sleep(settings.COPY_SCAN_INTERVAL_SEC)

    # ── Loop 4: Signal Generation ─────────────

    async def _signal_loop(self):
        """
        Tertiary strategy: filtered high-conviction directional signals.
        """
        await asyncio.sleep(60)  # Start after other loops

        while self._running:
            try:
                if self.risk.is_paused():
                    await asyncio.sleep(60)
                    continue

                signal_budget = self.portfolio.balance * settings.signal_weight
                signal_deployed = sum(
                    p.cost_usd for p in self.portfolio.open_positions.values()
                    if p.strategy == "signal"
                )
                if signal_deployed >= signal_budget:
                    await asyncio.sleep(settings.SCAN_INTERVAL_SEC * 2)
                    continue

                priority_markets = self.scanner.get_priority_markets()
                signals = self.signal_gen.generate(priority_markets)

                for sig in signals[:3]:  # Max 3 signal trades per cycle
                    if not sig.is_actionable:
                        continue

                    req = OrderRequest(
                        market_id=sig.market_id,
                        question=sig.question,
                        strategy="signal",
                        side=sig.side,
                        entry_price=sig.entry_price,
                        estimated_edge=sig.edge_score,
                        win_probability=sig.estimated_win_prob,
                        source_reasoning=sig.reasoning,
                    )

                    trade_id = await self.executor.execute_order(req)
                    if trade_id:
                        self.dashboard.add_event(
                            f"SIGNAL ({sig.signal_type}): {sig.question[:35]} | {sig.side}"
                        )
                        break

            except Exception as e:
                log.error(f"Signal loop error: {e}")

            await asyncio.sleep(settings.SCAN_INTERVAL_SEC * 2)

    # ── Loop 5: Position Monitoring ───────────

    async def _monitor_loop(self):
        """
        Check all open positions for exits, stop losses, partial exits.
        Runs every 15 seconds.
        """
        while self._running:
            try:
                if self.portfolio.open_positions:
                    await self.executor.monitor_positions(self._current_prices)
            except Exception as e:
                log.error(f"Monitor loop error: {e}")
            await asyncio.sleep(15)

    # ── Loop 6: Snapshots + Daily Summary ────

    async def _snapshot_loop(self):
        """
        Save portfolio snapshot every 30 minutes.
        Send daily summary at midnight UTC.
        """
        last_summary_date = None

        while self._running:
            try:
                # Save snapshot
                await self.portfolio.save_daily_snapshot()
                decision_log.log_balance(
                    self.portfolio.balance,
                    self.portfolio.deployed_usd,
                    self.portfolio.free_usd,
                )

                # Daily summary at midnight UTC
                now_utc = datetime.now(timezone.utc)
                today = now_utc.date()
                if last_summary_date != today and now_utc.hour == 0:
                    stats = await self.portfolio.get_stats()
                    decision_log.log_daily_summary(
                        stats["daily_pnl"],
                        stats["total_trades"],
                        stats["win_rate"],
                        stats["balance"],
                    )
                    self.notifier.notify_daily_summary(
                        pnl=stats["daily_pnl"],
                        trades=stats["total_trades"],
                        win_rate=stats["win_rate"],
                        balance=stats["balance"],
                        start_balance=self.capital,
                    )
                    last_summary_date = today

            except Exception as e:
                log.error(f"Snapshot loop error: {e}")

            await asyncio.sleep(1800)  # Every 30 minutes

    # ── Loop 7: Health Monitor ────────────────

    async def _health_loop(self):
        """
        Checks for conditions that need immediate action:
        - Daily loss limit → trigger circuit breaker
        - Total drawdown → emergency stop
        - API error streak → pause
        """
        while self._running:
            try:
                # Check drawdown
                if self.portfolio.drawdown_pct >= settings.drawdown_circuit_breaker:
                    self.risk._trigger_cb(
                        "drawdown_circuit_breaker", self.portfolio.drawdown_pct
                    )
                    self.notifier.notify_circuit_breaker(
                        "Total drawdown limit", self.portfolio.drawdown_pct,
                        self.portfolio.balance
                    )
                    self.dashboard.add_event("🚨 CIRCUIT BREAKER: drawdown limit")

                # Check daily loss
                if self.portfolio.daily_loss_pct <= -settings.max_daily_loss_pct:
                    self.risk._trigger_cb(
                        "daily_loss_limit", abs(self.portfolio.daily_loss_pct)
                    )
                    self.notifier.notify_circuit_breaker(
                        "Daily loss limit", abs(self.portfolio.daily_loss_pct),
                        self.portfolio.balance
                    )
                    self.dashboard.add_event("🚨 CIRCUIT BREAKER: daily loss limit")

                # Log health pulse every 10 minutes
                status = self.risk.status()
                log.debug(
                    f"Health | balance=${self.portfolio.balance:.4f} | "
                    f"drawdown={status['drawdown_pct']:.2%} | "
                    f"daily_loss={status['daily_loss_pct']:.2%} | "
                    f"cb={status['circuit_breaker']}"
                )

            except Exception as e:
                log.error(f"Health loop error: {e}")

            await asyncio.sleep(60)


# ── CLI Entry Point ───────────────────────────

def parse_args():
    parser = argparse.ArgumentParser(
        description="PolyBot — Ultra-Conservative Polymarket Trading Bot",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python main.py --mode dryrun --capital 10       # Full simulation, no real orders
  python main.py --mode paper  --capital 10       # Paper trading with live data
  python main.py --mode live   --capital 10       # Real money (careful!)
  python main.py --reset-cb                        # Reset circuit breaker and exit
  python backtester.py --months 6 --capital 10    # Run full backtest
        """,
    )
    parser.add_argument("--mode", choices=["dryrun", "paper", "live"],
                        default="dryrun", help="Bot operating mode")
    parser.add_argument("--capital", type=float, default=10.0,
                        help="Starting/current capital in USD")
    parser.add_argument("--reset-cb", action="store_true",
                        help="Manually reset circuit breaker state")
    return parser.parse_args()


async def reset_circuit_breaker():
    """Standalone CB reset utility."""
    db = await Database.get()
    await db.set_state("circuit_breaker", False)
    await db.set_state("cb_reason", "")
    print("✅ Circuit breaker reset. Restart the bot normally.")
    await db.close()


async def main():
    args = parse_args()

    if args.reset_cb:
        await reset_circuit_breaker()
        return

    mode = BotMode(args.mode)

    # Safety confirmation for live mode
    if mode == BotMode.LIVE:
        print("\n⚠️  WARNING: You are about to run in LIVE mode with REAL money.")
        print(f"   Capital: ${args.capital:.2f}")
        print("   This bot trades on Polymarket with real USDC.")
        confirm = input("   Type 'YES I UNDERSTAND THE RISKS' to continue: ")
        if confirm.strip() != "YES I UNDERSTAND THE RISKS":
            print("Aborted.")
            return

    bot = PolyBot(capital=args.capital, mode=mode)

    # Handle Ctrl+C gracefully
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(bot.stop("signal")))
        except NotImplementedError:
            pass # Windows doesn't support add_signal_handler

    await bot.start()


if __name__ == "__main__":
    asyncio.run(main())
