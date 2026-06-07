"""
dashboard/console_dashboard.py — Live console dashboard using Rich.
Displays: balance, P&L, open positions, circuit breaker status, recent trades.
Refreshes every 5 seconds. Run alongside the bot or embedded.
"""

import asyncio
import logging
from datetime import datetime
from typing import Optional, TYPE_CHECKING

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.layout import Layout
from rich.live import Live
from rich.text import Text
from rich import box

if TYPE_CHECKING:
    from modules.portfolio_tracker import PortfolioTracker
    from modules.risk_manager import RiskManager

log = logging.getLogger("dashboard")
console = Console()


class ConsoleDashboard:
    """
    Real-time Rich console dashboard.
    Embedded in main.py's async loop — no separate process needed.
    """

    REFRESH_SECONDS = 5

    def __init__(self):
        self._portfolio: Optional["PortfolioTracker"] = None
        self._risk: Optional["RiskManager"] = None
        self._recent_events: list = []
        self._max_events = 12
        self._live: Optional[Live] = None
        self._running = False

    def attach(self, portfolio: "PortfolioTracker", risk: "RiskManager"):
        self._portfolio = portfolio
        self._risk = risk

    def add_event(self, event: str):
        """Add an event to the live feed."""
        ts = datetime.utcnow().strftime("%H:%M:%S")
        self._recent_events.append(f"[dim]{ts}[/dim] {event}")
        if len(self._recent_events) > self._max_events:
            self._recent_events.pop(0)

    async def start(self):
        """Start dashboard in background task."""
        self._running = True
        asyncio.create_task(self._render_loop())

    async def stop(self):
        self._running = False

    async def _render_loop(self):
        with Live(self._build_layout(), refresh_per_second=0.2, screen=True) as live:
            self._live = live
            while self._running:
                live.update(self._build_layout())
                await asyncio.sleep(self.REFRESH_SECONDS)

    def _build_layout(self) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=3),
            Layout(name="main"),
            Layout(name="footer", size=3),
        )
        layout["main"].split_row(
            Layout(name="left", ratio=2),
            Layout(name="right", ratio=3),
        )
        layout["left"].split_column(
            Layout(name="portfolio", ratio=2),
            Layout(name="risk", ratio=1),
        )

        layout["header"].update(self._header_panel())
        layout["portfolio"].update(self._portfolio_panel())
        layout["risk"].update(self._risk_panel())
        layout["right"].update(self._positions_panel())
        layout["footer"].update(self._events_panel())

        return layout

    def _header_panel(self) -> Panel:
        mode = getattr(self._portfolio, "_mode", "unknown") if self._portfolio else "N/A"
        ts = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")
        from config import settings
        mode_str = settings.mode.value.upper()
        color = {"LIVE": "bold red", "PAPER": "bold yellow", "DRYRUN": "bold cyan"}.get(mode_str, "white")
        return Panel(
            Text(f"🤖  POLYMARKET BOT  │  Mode: [{color}]{mode_str}[/{color}]  │  {ts}", justify="center"),
            box=box.HEAVY_HEAD,
        )

    def _portfolio_panel(self) -> Panel:
        if not self._portfolio:
            return Panel("Loading...", title="Portfolio")

        p = self._portfolio
        total_pnl = p.balance - p.starting_capital
        pnl_color = "green" if total_pnl >= 0 else "red"
        daily_color = "green" if p.realized_pnl_today >= 0 else "red"

        table = Table(show_header=False, box=box.SIMPLE, expand=True)
        table.add_column("Metric", style="dim")
        table.add_column("Value", justify="right")

        table.add_row("Balance",        f"[bold]${p.balance:.4f}[/bold]")
        table.add_row("Starting",       f"${p.starting_capital:.4f}")
        table.add_row("Total P&L",      f"[{pnl_color}]{total_pnl:+.4f}[/{pnl_color}]")
        table.add_row("Today's P&L",    f"[{daily_color}]{p.realized_pnl_today:+.4f}[/{daily_color}]")
        table.add_row("Unrealized",     f"[cyan]{p.unrealized_pnl:+.4f}[/cyan]")
        table.add_row("Deployed",       f"${p.deployed_usd:.4f} ({p.deployed_pct:.1%})")
        table.add_row("Free Capital",   f"[bold green]${p.free_usd:.4f}[/bold green]")
        table.add_row("Peak Balance",   f"${p.peak_balance:.4f}")
        table.add_row("Drawdown",       f"[yellow]{p.drawdown_pct:.2%}[/yellow]")
        table.add_row("Open Positions", str(len(p.open_positions)))

        return Panel(table, title="💰 Portfolio", border_style="blue")

    def _risk_panel(self) -> Panel:
        if not self._risk:
            return Panel("Loading...", title="Risk")

        status = self._risk.status()
        cb_color = "bold red" if status["circuit_breaker"] else "bold green"
        cb_text = "🚨 TRIGGERED" if status["circuit_breaker"] else "✅ CLEAR"

        lines = [
            f"Circuit Breaker: [{cb_color}]{cb_text}[/{cb_color}]",
            f"Consecutive Losses: {status['consecutive_losses']}",
            f"API Error Streak: {status['api_error_streak']}",
        ]
        if status["circuit_breaker"] and status["cb_reason"]:
            lines.append(f"[red]Reason: {status['cb_reason']}[/red]")

        return Panel("\n".join(lines), title="🔒 Risk Status", border_style="yellow")

    def _positions_panel(self) -> Panel:
        if not self._portfolio:
            return Panel("Loading...", title="Positions")

        positions = list(self._portfolio.open_positions.values())
        if not positions:
            return Panel("[dim]No open positions[/dim]", title="📋 Open Positions")

        table = Table(expand=True, box=box.SIMPLE_HEAVY)
        table.add_column("#", width=4)
        table.add_column("Question", ratio=3)
        table.add_column("Side", width=5)
        table.add_column("Entry", justify="right", width=7)
        table.add_column("Current", justify="right", width=8)
        table.add_column("Shares", justify="right", width=7)
        table.add_column("USD In", justify="right", width=8)
        table.add_column("Unreal. P&L", justify="right", width=11)
        table.add_column("Strategy", width=8)

        for i, pos in enumerate(positions[:8], 1):
            pnl_color = "green" if pos.unrealized_pnl >= 0 else "red"
            side_color = "cyan" if pos.side == "YES" else "magenta"
            table.add_row(
                str(i),
                pos.market_question[:40] + "…" if len(pos.market_question) > 40 else pos.market_question,
                f"[{side_color}]{pos.side}[/{side_color}]",
                f"{pos.entry_price:.4f}",
                f"{pos.current_price:.4f}" if pos.current_price > 0 else "N/A",
                f"{pos.shares:.1f}",
                f"${pos.cost_usd:.4f}",
                f"[{pnl_color}]{pos.unrealized_pnl:+.4f}[/{pnl_color}]",
                pos.strategy,
            )

        return Panel(table, title=f"📋 Open Positions ({len(positions)})", border_style="cyan")

    def _events_panel(self) -> Panel:
        events = self._recent_events[-5:] if self._recent_events else ["[dim]No events yet[/dim]"]
        return Panel(" │ ".join(events), title="📡 Recent Events", border_style="dim")

    def print_startup_banner(self, capital: float, mode: str):
        console.print(Panel(
            f"""
  ██████╗  ██████╗ ██╗  ██╗   ██╗██████╗  ██████╗ ████████╗
  ██╔══██╗██╔═══██╗██║  ╚██╗ ██╔╝██╔══██╗██╔═══██╗╚══██╔══╝
  ██████╔╝██║   ██║██║   ╚████╔╝ ██████╔╝██║   ██║   ██║   
  ██╔═══╝ ██║   ██║██║    ╚██╔╝  ██╔══██╗██║   ██║   ██║   
  ██║     ╚██████╔╝███████╗██║   ██████╔╝╚██████╔╝   ██║   
  ╚═╝      ╚═════╝ ╚══════╝╚═╝   ╚═════╝  ╚═════╝    ╚═╝   

  Mode: [bold]{mode.upper()}[/bold]  |  Capital: [bold green]${capital:.2f}[/bold green]  |  Target: 80%+ Win Rate
  ⚠️  RISK DISCLAIMER: Trading prediction markets involves real financial risk.
  Start with dryrun mode. Never deploy more than you can afford to lose.
            """,
            title="🤖 PolyBot v1.0 — Ultra-Conservative $10 Start",
            border_style="bold blue",
        ))
