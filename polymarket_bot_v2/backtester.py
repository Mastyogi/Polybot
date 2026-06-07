"""
backtester.py — Backtesting & Dry-Run Simulation Engine.
Uses historical Polymarket data (Gamma API) for in-sample / out-of-sample testing.
Metrics: Win Rate (>80%), Profit Factor, Max Drawdown (<25%), Expectancy, Sharpe.
Run: python backtester.py --months 6 --capital 10
"""

import asyncio
import logging
import json
import argparse
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple
from dataclasses import dataclass, field
import statistics
import aiohttp

from config import settings
from modules.arb_detector import ArbDetector
from modules.signal_generator import SignalGenerator
from modules.scanner import MarketData

log = logging.getLogger("backtester")


@dataclass
class BacktestTrade:
    timestamp: str
    strategy: str
    market_id: str
    question: str
    side: str
    entry_price: float
    exit_price: float
    shares: float
    usd_in: float
    pnl_usd: float
    pnl_pct: float
    won: bool
    edge_at_entry: float
    hold_days: float


@dataclass
class BacktestResult:
    strategy: str
    total_trades: int
    win_trades: int
    loss_trades: int
    win_rate: float
    total_pnl_usd: float
    total_return_pct: float
    profit_factor: float
    max_drawdown_pct: float
    expectancy_usd: float
    sharpe_ratio: float
    avg_hold_days: float
    final_balance: float
    passed: bool          # True if meets all criteria
    fail_reasons: List[str] = field(default_factory=list)


class Backtester:
    """
    Historical backtest using resolved Polymarket markets.
    Fetches resolved markets from Gamma API and simulates the bot's strategies.
    """

    GAMMA_API = settings.gamma_api
    # Criteria a strategy must pass
    MIN_WIN_RATE = 0.78
    MIN_PROFIT_FACTOR = 1.5
    MAX_DRAWDOWN = 0.25
    MIN_TRADES = 30

    def __init__(self, starting_capital: float = 10.0, months: int = 6):
        self.starting_capital = starting_capital
        self.months = months
        self.arb_detector = ArbDetector()
        self.signal_gen = SignalGenerator()
        self._session: Optional[aiohttp.ClientSession] = None

    async def run(self) -> Dict[str, BacktestResult]:
        """Full backtest run. Returns results per strategy."""
        self._session = aiohttp.ClientSession()
        try:
            log.info(f"Starting backtest | capital=${self.starting_capital} | months={self.months}")
            markets = await self._fetch_resolved_markets()
            log.info(f"Fetched {len(markets)} resolved markets for backtest")

            # Split: 70% in-sample, 30% out-of-sample
            split = int(len(markets) * 0.70)
            in_sample   = markets[:split]
            out_sample  = markets[split:]

            log.info(f"In-sample: {len(in_sample)} | Out-of-sample: {len(out_sample)}")

            # Run strategies on in-sample
            arb_trades_is   = self._backtest_arb(in_sample, "in_sample")
            signal_trades_is = self._backtest_signals(in_sample, "in_sample")

            # Validate on out-of-sample
            arb_trades_oos   = self._backtest_arb(out_sample, "out_of_sample")
            signal_trades_oos = self._backtest_signals(out_sample, "out_of_sample")

            results = {
                "arb_in_sample":       self._compute_metrics("arb_in_sample", arb_trades_is),
                "arb_out_of_sample":   self._compute_metrics("arb_out_of_sample", arb_trades_oos),
                "signal_in_sample":    self._compute_metrics("signal_in_sample", signal_trades_is),
                "signal_out_of_sample": self._compute_metrics("signal_out_of_sample", signal_trades_oos),
            }

            self._print_results(results)
            self._save_results(results)
            return results

        finally:
            await self._session.close()

    # ── Market Data Fetching ──────────────────

    async def _fetch_resolved_markets(self) -> List[dict]:
        """
        Fetch resolved Polymarket markets from the past N months.
        Returns raw market dicts with resolution data.
        """
        all_markets = []
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.months * 30)

        offset = 0
        limit = 100
        while True:
            try:
                params = {
                    "closed": "true",
                    "limit": limit,
                    "offset": offset,
                    "order": "endDate",
                    "ascending": "false",
                }
                async with self._session.get(
                    f"{self.GAMMA_API}/markets", params=params, timeout=aiohttp.ClientTimeout(total=20)
                ) as resp:
                    if resp.status != 200:
                        break
                    data = await resp.json()

                if not data:
                    break

                # Filter by date
                for m in data:
                    end_str = m.get("endDate", "")
                    if not end_str:
                        continue
                    try:
                        end_dt = datetime.fromisoformat(end_str.replace("Z", "+00:00"))
                        if end_dt >= cutoff:
                            all_markets.append(m)
                    except Exception:
                        pass

                if len(data) < limit:
                    break
                offset += limit
                await asyncio.sleep(0.5)   # Rate limit friendly

            except Exception as e:
                log.error(f"Market fetch error at offset={offset}: {e}")
                break

        return all_markets

    # ── Arb Backtest ──────────────────────────

    def _backtest_arb(self, markets: List[dict], label: str) -> List[BacktestTrade]:
        """
        Simulate arb strategy on historical markets.
        Logic: if YES+NO sum < arb_max_sum at some point, we would have entered.
        Resolution: always pays $1 (arb is risk-free if sum < 1.0).
        """
        trades = []
        balance = self.starting_capital

        for m in markets:
            tokens = m.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            no_token  = next((t for t in tokens if t.get("outcome", "").upper() == "NO"), None)
            if not yes_token or not no_token:
                continue

            yes_price = float(yes_token.get("price", 0.5))
            no_price  = float(no_token.get("price", 0.5))
            price_sum = yes_price + no_price

            if price_sum >= settings.arb_max_sum:
                continue

            # Simulate entry
            gap = 1.0 - price_sum
            net_profit_pct = gap - 0.002   # Subtract estimated fees
            if net_profit_pct < settings.arb_min_profit_pct:
                continue

            usd_in = min(balance * settings.max_risk_per_trade_pct, balance * 0.35)
            usd_in = max(usd_in, settings.MIN_ORDER_SIZE_USD)
            if usd_in > balance:
                continue

            # Arb always wins (buys both sides, one always resolves to 1.0)
            pnl = usd_in * net_profit_pct
            won = True

            balance += pnl
            trades.append(BacktestTrade(
                timestamp=m.get("endDate", ""),
                strategy="arb",
                market_id=m.get("conditionId", ""),
                question=m.get("question", "")[:80],
                side="BOTH",
                entry_price=price_sum / 2,
                exit_price=1.0,
                shares=usd_in / price_sum * 2,
                usd_in=usd_in,
                pnl_usd=pnl,
                pnl_pct=net_profit_pct,
                won=won,
                edge_at_entry=min(0.95, 0.80 + gap / 0.05 * 0.17),
                hold_days=1.0,   # Arb resolved quickly on avg
            ))

        log.info(f"Arb backtest [{label}]: {len(trades)} simulated trades")
        return trades

    # ── Signal Backtest ───────────────────────

    def _backtest_signals(self, markets: List[dict], label: str) -> List[BacktestTrade]:
        """
        Simulate signal strategy on resolved markets.
        We use the final resolution as ground truth.
        Near-resolution and extreme-probability signals are most testable here.
        """
        trades = []
        balance = self.starting_capital

        for m in markets:
            tokens = m.get("tokens", [])
            yes_token = next((t for t in tokens if t.get("outcome", "").upper() == "YES"), None)
            if not yes_token:
                continue

            yes_price = float(yes_token.get("price", 0.5))
            yes_winner = yes_token.get("winner", False)

            # Simulate: if YES ≥ 0.88 → we would have bought YES
            if yes_price >= 0.88:
                entry_price = yes_price
                usd_in = min(
                    balance * settings.max_risk_per_trade_pct,
                    balance * 0.35,
                )
                usd_in = max(usd_in, settings.MIN_ORDER_SIZE_USD)
                if usd_in > balance:
                    continue

                shares = usd_in / entry_price
                exit_price = 1.0 if yes_winner else 0.0
                pnl = (exit_price - entry_price) * shares

                balance += pnl
                trades.append(BacktestTrade(
                    timestamp=m.get("endDate", ""),
                    strategy="signal",
                    market_id=m.get("conditionId", ""),
                    question=m.get("question", "")[:80],
                    side="YES",
                    entry_price=entry_price,
                    exit_price=exit_price,
                    shares=shares,
                    usd_in=usd_in,
                    pnl_usd=pnl,
                    pnl_pct=pnl / usd_in if usd_in > 0 else 0,
                    won=yes_winner,
                    edge_at_entry=yes_price,
                    hold_days=3.0,
                ))

            # Simulate: if YES ≤ 0.12 → we would have bought NO
            elif yes_price <= 0.12:
                no_price = 1.0 - yes_price
                entry_price = no_price
                usd_in = min(
                    balance * settings.max_risk_per_trade_pct,
                    balance * 0.35,
                )
                usd_in = max(usd_in, settings.MIN_ORDER_SIZE_USD)
                if usd_in > balance:
                    continue

                shares = usd_in / entry_price
                no_winner = not yes_winner
                exit_price = 1.0 if no_winner else 0.0
                pnl = (exit_price - entry_price) * shares

                balance += pnl
                trades.append(BacktestTrade(
                    timestamp=m.get("endDate", ""),
                    strategy="signal",
                    market_id=m.get("conditionId", ""),
                    question=m.get("question", "")[:80],
                    side="NO",
                    entry_price=entry_price,
                    exit_price=exit_price,
                    shares=shares,
                    usd_in=usd_in,
                    pnl_usd=pnl,
                    pnl_pct=pnl / usd_in if usd_in > 0 else 0,
                    won=no_winner,
                    edge_at_entry=1.0 - yes_price,
                    hold_days=3.0,
                ))

        log.info(f"Signal backtest [{label}]: {len(trades)} simulated trades")
        return trades

    # ── Metrics ───────────────────────────────

    def _compute_metrics(self, name: str, trades: List[BacktestTrade]) -> BacktestResult:
        if not trades:
            return BacktestResult(
                strategy=name, total_trades=0, win_trades=0, loss_trades=0,
                win_rate=0, total_pnl_usd=0, total_return_pct=0, profit_factor=0,
                max_drawdown_pct=0, expectancy_usd=0, sharpe_ratio=0,
                avg_hold_days=0, final_balance=self.starting_capital, passed=False,
                fail_reasons=["No trades"],
            )

        wins  = [t for t in trades if t.won]
        losses = [t for t in trades if not t.won]
        win_rate = len(wins) / len(trades)

        gross_profit = sum(t.pnl_usd for t in wins)
        gross_loss   = abs(sum(t.pnl_usd for t in losses))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")
        total_pnl = sum(t.pnl_usd for t in trades)

        # Max drawdown
        running = self.starting_capital
        peak = self.starting_capital
        max_dd = 0.0
        for t in trades:
            running += t.pnl_usd
            if running > peak:
                peak = running
            dd = (peak - running) / peak if peak > 0 else 0
            max_dd = max(max_dd, dd)

        # Sharpe (daily returns)
        daily_returns = [t.pnl_pct for t in trades]
        if len(daily_returns) > 1:
            avg_r = statistics.mean(daily_returns)
            std_r = statistics.stdev(daily_returns)
            sharpe = (avg_r / std_r) * (252 ** 0.5) if std_r > 0 else 0.0
        else:
            sharpe = 0.0

        expectancy = total_pnl / len(trades)
        avg_hold = statistics.mean(t.hold_days for t in trades)
        final_balance = self.starting_capital + total_pnl
        total_return_pct = total_pnl / self.starting_capital

        fail_reasons = []
        if win_rate < self.MIN_WIN_RATE:
            fail_reasons.append(f"Win rate {win_rate:.1%} < {self.MIN_WIN_RATE:.1%}")
        if profit_factor < self.MIN_PROFIT_FACTOR:
            fail_reasons.append(f"Profit factor {profit_factor:.2f} < {self.MIN_PROFIT_FACTOR}")
        if max_dd > self.MAX_DRAWDOWN:
            fail_reasons.append(f"Max drawdown {max_dd:.1%} > {self.MAX_DRAWDOWN:.1%}")
        if len(trades) < self.MIN_TRADES:
            fail_reasons.append(f"Only {len(trades)} trades (min {self.MIN_TRADES})")

        return BacktestResult(
            strategy=name,
            total_trades=len(trades),
            win_trades=len(wins),
            loss_trades=len(losses),
            win_rate=win_rate,
            total_pnl_usd=total_pnl,
            total_return_pct=total_return_pct,
            profit_factor=profit_factor,
            max_drawdown_pct=max_dd,
            expectancy_usd=expectancy,
            sharpe_ratio=sharpe,
            avg_hold_days=avg_hold,
            final_balance=final_balance,
            passed=len(fail_reasons) == 0,
            fail_reasons=fail_reasons,
        )

    def _print_results(self, results: Dict[str, BacktestResult]):
        print("\n" + "=" * 70)
        print("POLYBOT BACKTEST RESULTS")
        print("=" * 70)
        for name, r in results.items():
            status = "✅ PASS" if r.passed else "❌ FAIL"
            print(f"\n{status} | {name}")
            print(f"  Trades:        {r.total_trades} ({r.win_trades}W / {r.loss_trades}L)")
            print(f"  Win Rate:      {r.win_rate:.1%}")
            print(f"  Profit Factor: {r.profit_factor:.2f}")
            print(f"  Total PnL:     ${r.total_pnl_usd:+.4f} ({r.total_return_pct:+.1%})")
            print(f"  Max Drawdown:  {r.max_drawdown_pct:.1%}")
            print(f"  Expectancy:    ${r.expectancy_usd:+.4f}/trade")
            print(f"  Sharpe Ratio:  {r.sharpe_ratio:.2f}")
            print(f"  Final Balance: ${r.final_balance:.4f}")
            if r.fail_reasons:
                print(f"  Fail Reasons:  {', '.join(r.fail_reasons)}")
        print("=" * 70)

    def _save_results(self, results: Dict[str, BacktestResult]):
        output = {
            name: {
                "strategy": r.strategy,
                "total_trades": r.total_trades,
                "win_rate": r.win_rate,
                "profit_factor": r.profit_factor,
                "total_pnl_usd": r.total_pnl_usd,
                "max_drawdown_pct": r.max_drawdown_pct,
                "sharpe_ratio": r.sharpe_ratio,
                "final_balance": r.final_balance,
                "passed": r.passed,
                "fail_reasons": r.fail_reasons,
            }
            for name, r in results.items()
        }
        with open("data/backtest_results.json", "w") as f:
            json.dump(output, f, indent=2)
        log.info("Backtest results saved to data/backtest_results.json")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Polymarket Bot Backtester")
    parser.add_argument("--months", type=int, default=6, help="Months of history to backtest")
    parser.add_argument("--capital", type=float, default=10.0, help="Starting capital USD")
    args = parser.parse_args()

    import logging
    logging.basicConfig(level=logging.INFO)

    bt = Backtester(starting_capital=args.capital, months=args.months)
    asyncio.run(bt.run())
