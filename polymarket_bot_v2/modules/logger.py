"""
logger.py — Centralized structured logger.
Outputs to rotating daily files + colored console.
Every decision is logged with reasoning.
"""

import logging
import logging.handlers
import colorlog
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Optional
from config import settings


LOG_DIR = Path(settings.LOG_DIR)
LOG_DIR.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────
# Setup
# ──────────────────────────────────────────

def setup_logging() -> logging.Logger:
    """Configure root logger with console + rotating file handlers."""
    log_level = getattr(logging, settings.log_level.upper(), logging.INFO)

    # Console handler (colored)
    console = colorlog.StreamHandler()
    console.setLevel(log_level)
    console.setFormatter(colorlog.ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(name)s — %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            "DEBUG":    "cyan",
            "INFO":     "green",
            "WARNING":  "yellow",
            "ERROR":    "red",
            "CRITICAL": "bold_red",
        },
    ))

    # File handler (JSON lines for easy parsing)
    log_file = LOG_DIR / f"bot_{datetime.utcnow().strftime('%Y%m%d')}.log"
    file_handler = logging.handlers.TimedRotatingFileHandler(
        log_file, when="midnight", interval=1, backupCount=30, encoding="utf-8"
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    root.handlers.clear()
    root.addHandler(console)
    root.addHandler(file_handler)

    return root


class JsonFormatter(logging.Formatter):
    """Format log records as JSON lines for easy grep/query."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts": datetime.utcnow().isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            log_obj["exc"] = self.formatException(record.exc_info)
        if hasattr(record, "extra_data"):
            log_obj["data"] = record.extra_data
        return json.dumps(log_obj, ensure_ascii=False)


# ──────────────────────────────────────────
# Decision Logger — every trade decision logged with reason
# ──────────────────────────────────────────

class DecisionLogger:
    """
    Structured logger for bot decisions.
    Ensures every trade action has a documented reason.
    """

    def __init__(self):
        self.log = logging.getLogger("decisions")

    def log_signal_rejected(self, market: str, reason: str, details: dict = {}):
        self.log.info(
            f"SIGNAL_REJECTED | market={market} | reason={reason} | {json.dumps(details)}"
        )

    def log_signal_accepted(self, market: str, strategy: str, edge: float, details: dict = {}):
        self.log.info(
            f"SIGNAL_ACCEPTED | market={market} | strategy={strategy} | "
            f"edge={edge:.2%} | {json.dumps(details)}"
        )

    def log_order_placed(self, market: str, side: str, price: float,
                         shares: float, usd: float, mode: str):
        self.log.info(
            f"ORDER_PLACED | mode={mode} | market={market} | side={side} | "
            f"price={price:.4f} | shares={shares:.2f} | usd=${usd:.4f}"
        )

    def log_order_skipped(self, market: str, reason: str):
        self.log.warning(f"ORDER_SKIPPED | market={market} | reason={reason}")

    def log_exit(self, trade_id: int, pnl: float, reason: str, mode: str):
        emoji = "✅" if pnl >= 0 else "🔴"
        self.log.info(
            f"EXIT {emoji} | mode={mode} | trade_id={trade_id} | "
            f"pnl=${pnl:+.4f} | reason={reason}"
        )

    def log_risk_event(self, event: str, details: dict = {}):
        self.log.warning(f"RISK_EVENT | event={event} | {json.dumps(details)}")

    def log_circuit_breaker(self, reason: str, loss_pct: float):
        self.log.critical(
            f"🚨 CIRCUIT_BREAKER TRIGGERED | reason={reason} | "
            f"loss_pct={loss_pct:.2%}"
        )

    def log_arb(self, market: str, yes_p: float, no_p: float,
                profit_pct: float, acted: bool, skip: str = ""):
        tag = "ARB_ACTED" if acted else "ARB_DETECTED"
        self.log.info(
            f"{tag} | market={market} | YES={yes_p:.4f} NO={no_p:.4f} "
            f"sum={yes_p+no_p:.4f} | profit={profit_pct:.2%} | skip={skip}"
        )

    def log_copy_signal(self, wallet: str, market: str, side: str, acted: bool, reason: str = ""):
        tag = "COPY_ACTED" if acted else "COPY_SKIPPED"
        self.log.info(
            f"{tag} | wallet={wallet[:10]}… | market={market} | side={side} | reason={reason}"
        )

    def log_balance(self, balance: float, deployed: float, free: float):
        self.log.info(
            f"BALANCE | total=${balance:.4f} | deployed=${deployed:.4f} | free=${free:.4f}"
        )

    def log_daily_summary(self, pnl: float, trades: int, win_rate: float, balance: float):
        self.log.info(
            f"📊 DAILY_SUMMARY | pnl=${pnl:+.4f} | trades={trades} | "
            f"win_rate={win_rate:.1%} | balance=${balance:.4f}"
        )


# Global instances
setup_logging()
decision_log = DecisionLogger()
bot_log = logging.getLogger("bot")
