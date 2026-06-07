"""
telegram_notifier.py — Async Telegram notifications.
Sends trade alerts, daily summaries, risk events, circuit breaker alerts.
Non-blocking — never stalls the main bot loop.
"""

import asyncio
import logging
import httpx
from datetime import datetime
from typing import Optional
from config import settings

log = logging.getLogger("telegram")


class TelegramNotifier:
    """
    Lightweight async Telegram messenger using raw HTTP API.
    Falls back gracefully if token/chat_id not configured.
    """

    def __init__(self):
        self.token = settings.telegram_bot_token
        self.chat_id = settings.telegram_chat_id
        self.enabled = bool(self.token and self.chat_id)
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._queue: asyncio.Queue = asyncio.Queue()
        self._task: Optional[asyncio.Task] = None

        if not self.enabled:
            log.warning("Telegram not configured — notifications disabled.")

    async def start(self):
        """Start the background message sender task."""
        if self.enabled:
            self._task = asyncio.create_task(self._worker())
            log.info("Telegram notifier started.")

    async def stop(self):
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _worker(self):
        """Background worker — drains queue and sends messages."""
        async with httpx.AsyncClient(timeout=10.0) as client:
            while True:
                msg = await self._queue.get()
                try:
                    await client.post(
                        f"{self.base_url}/sendMessage",
                        json={
                            "chat_id": self.chat_id,
                            "text": msg,
                            "parse_mode": "HTML",
                        },
                    )
                except Exception as e:
                    log.error(f"Telegram send error: {e}")
                finally:
                    self._queue.task_done()
                    await asyncio.sleep(0.5)  # rate limit

    def _enqueue(self, message: str):
        """Non-blocking enqueue — never blocks the bot loop."""
        if self.enabled:
            try:
                self._queue.put_nowait(message)
            except asyncio.QueueFull:
                log.warning("Telegram queue full, dropping message.")

    # ── Public notification methods ──────────

    def notify_startup(self, mode: str, capital: float):
        msg = (
            f"🤖 <b>PolyBot Started</b>\n"
            f"Mode: <code>{mode.upper()}</code>\n"
            f"Capital: <code>${capital:.2f}</code>\n"
            f"Time: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}"
        )
        self._enqueue(msg)

    def notify_trade(self, strategy: str, market: str, side: str,
                     price: float, usd: float, edge: float, mode: str):
        emoji = "🟢" if side == "YES" else "🔴"
        mode_tag = f"[{mode.upper()}]" if mode != "live" else ""
        msg = (
            f"{emoji} <b>Trade {mode_tag}</b>\n"
            f"Strategy: <code>{strategy}</code>\n"
            f"Market: {market[:60]}\n"
            f"Side: <code>{side}</code> @ {price:.4f}\n"
            f"Size: <code>${usd:.4f}</code>\n"
            f"Edge: <code>{edge:.1%}</code>"
        )
        self._enqueue(msg)

    def notify_exit(self, market: str, pnl: float, reason: str, mode: str):
        emoji = "✅" if pnl >= 0 else "🔴"
        mode_tag = f"[{mode.upper()}]" if mode != "live" else ""
        msg = (
            f"{emoji} <b>Exit {mode_tag}</b>\n"
            f"Market: {market[:60]}\n"
            f"PnL: <code>${pnl:+.4f}</code>\n"
            f"Reason: {reason}"
        )
        self._enqueue(msg)

    def notify_arb(self, market: str, yes_p: float, no_p: float, profit_pct: float):
        msg = (
            f"⚡ <b>Arb Detected</b>\n"
            f"Market: {market[:60]}\n"
            f"YES={yes_p:.4f} | NO={no_p:.4f}\n"
            f"Sum={yes_p+no_p:.4f} | Profit≈{profit_pct:.2%}"
        )
        self._enqueue(msg)

    def notify_circuit_breaker(self, reason: str, loss_pct: float, balance: float):
        msg = (
            f"🚨 <b>CIRCUIT BREAKER TRIGGERED</b>\n"
            f"Reason: {reason}\n"
            f"Loss: <code>{loss_pct:.2%}</code>\n"
            f"Balance: <code>${balance:.4f}</code>\n"
            f"<b>Bot paused. Manual review required.</b>"
        )
        self._enqueue(msg)

    def notify_daily_summary(self, pnl: float, trades: int,
                              win_rate: float, balance: float,
                              start_balance: float):
        pnl_pct = (balance - start_balance) / start_balance if start_balance > 0 else 0
        emoji = "📈" if pnl >= 0 else "📉"
        msg = (
            f"{emoji} <b>Daily Summary</b>\n"
            f"Balance: <code>${balance:.4f}</code> ({pnl_pct:+.2%})\n"
            f"PnL Today: <code>${pnl:+.4f}</code>\n"
            f"Trades: {trades} | Win Rate: {win_rate:.1%}\n"
            f"Date: {datetime.utcnow().strftime('%Y-%m-%d UTC')}"
        )
        self._enqueue(msg)

    def notify_risk_warning(self, event: str, detail: str):
        msg = (
            f"⚠️ <b>Risk Warning</b>\n"
            f"Event: {event}\n"
            f"Detail: {detail}"
        )
        self._enqueue(msg)

    def notify_error(self, error: str):
        msg = f"❌ <b>Bot Error</b>\n<code>{error[:300]}</code>"
        self._enqueue(msg)
