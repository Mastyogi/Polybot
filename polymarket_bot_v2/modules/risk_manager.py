"""
risk_manager.py — All position sizing and circuit breaker logic.
Uses a capped fractional-Kelly formula safe for micro-capital ($10 start).
No trade ever goes through without passing all risk gates here.
"""

import logging
import asyncio
from datetime import datetime, date, timezone
from typing import Optional, Tuple
from dataclasses import dataclass

from config import settings
from modules.portfolio_tracker import PortfolioTracker
from modules.logger import decision_log

log = logging.getLogger("risk_manager")


# ──────────────────────────────────────────
# Circuit Breaker State
# ──────────────────────────────────────────

@dataclass
class CircuitBreakerState:
    triggered: bool = False
    reason: str = ""
    triggered_at: Optional[str] = None
    daily_loss_hit: bool = False
    drawdown_hit: bool = False
    api_error_streak: int = 0
    consecutive_losses: int = 0


class RiskManager:
    """
    Single gatekeeper for all risk decisions.
    Every order MUST call approve() before execution.
    """

    # Hard limits — never overridden by config
    HARD_MAX_RISK_PER_TRADE = 0.10      # 10% of balance absolute max
    HARD_MIN_ORDER_USD = 0.50           # minimum $0.50 order
    HARD_MAX_CONSECUTIVE_LOSSES = 5     # pause after 5 straight losses
    HARD_MAX_API_ERROR_STREAK = 10      # pause if 10 API errors in a row
    KELLY_FRACTION = 0.25               # Use 25% of full Kelly (ultra-conservative)
    MAX_KELLY_BET_PCT = 0.08            # Hard cap Kelly output at 8%

    def __init__(self, portfolio: PortfolioTracker):
        self.portfolio = portfolio
        self.cb = CircuitBreakerState()
        self._lock = asyncio.Lock()
        self._daily_reset_date: Optional[date] = None

    # ── Core Approval Gate ────────────────────

    async def approve(
        self,
        market_id: str,
        strategy: str,
        estimated_edge: float,
        win_probability: float,
        proposed_usd: float,
    ) -> Tuple[bool, float, str]:
        """
        Main risk gate. Returns (approved, final_usd_size, reason).
        All trade requests must pass through here.
        """
        async with self._lock:
            self._check_daily_reset()

            # 1. Circuit breaker
            if self.cb.triggered:
                return False, 0.0, f"Circuit breaker active: {self.cb.reason}"

            # 2. Min edge threshold
            if estimated_edge < settings.min_edge_threshold:
                decision_log.log_signal_rejected(
                    market_id, "edge_below_threshold",
                    {"edge": estimated_edge, "min": settings.min_edge_threshold}
                )
                return False, 0.0, f"Edge {estimated_edge:.2%} < min {settings.min_edge_threshold:.2%}"

            # 3. Daily loss limit
            if self.portfolio.daily_loss_pct <= -settings.max_daily_loss_pct:
                self._trigger_cb("daily_loss_limit", self.portfolio.daily_loss_pct)
                return False, 0.0, "Daily loss limit reached"

            # 4. Drawdown circuit breaker
            if self.portfolio.drawdown_pct >= settings.drawdown_circuit_breaker:
                self._trigger_cb("max_drawdown", self.portfolio.drawdown_pct)
                return False, 0.0, "Max drawdown circuit breaker"

            # 5. Consecutive losses
            if self.cb.consecutive_losses >= self.HARD_MAX_CONSECUTIVE_LOSSES:
                self._trigger_cb("consecutive_losses", 0.0)
                return False, 0.0, f"{self.cb.consecutive_losses} consecutive losses"

            # 5b. Max concurrent positions tier check
            if len(self.portfolio.open_positions) >= self.max_concurrent_positions():
                return False, 0.0, f"Max concurrent positions ({self.max_concurrent_positions()}) reached for current tier"

            # 6. Calculate safe position size via Kelly
            kelly_size_usd = self._kelly_size(
                win_probability, estimated_edge, self.portfolio.balance
            )

            # 7. Take the minimum of Kelly vs proposed vs hard cap
            final_usd = min(
                kelly_size_usd,
                proposed_usd,
                self.portfolio.balance * self.HARD_MAX_RISK_PER_TRADE,
            )
            final_usd = max(final_usd, self.HARD_MIN_ORDER_USD)
            final_usd = round(final_usd, 4)

            # 8. Check portfolio-level deployment limits
            can_deploy, deploy_reason = self.portfolio.can_deploy(final_usd)
            if not can_deploy:
                decision_log.log_signal_rejected(market_id, "portfolio_limit", {"reason": deploy_reason})
                return False, 0.0, deploy_reason

            # 9. Sanity: final amount must be meaningful
            if final_usd < self.HARD_MIN_ORDER_USD:
                return False, 0.0, f"Computed size ${final_usd:.4f} too small"

            log.debug(
                f"Risk approved | market={market_id[:30]} | kelly=${kelly_size_usd:.4f} "
                f"| final=${final_usd:.4f} | edge={estimated_edge:.2%}"
            )
            return True, final_usd, "OK"

    # ── Kelly Formula (Micro-Capital Safe) ───

    def _kelly_size(self, win_prob: float, edge: float, balance: float) -> float:
        """
        Fractional Kelly criterion, capped for micro-capital safety.
        
        Full Kelly: f = (bp - q) / b
        where b = odds, p = win prob, q = 1 - p
        
        For prediction markets: b ≈ (1/p) - 1 (fair odds)
        We use 25% of full Kelly as our fraction.
        """
        if win_prob <= 0 or win_prob >= 1 or balance <= 0:
            return self.HARD_MIN_ORDER_USD

        # Fair odds implied by probability
        b = (1.0 / win_prob) - 1.0
        p = win_prob
        q = 1.0 - p

        full_kelly_f = (b * p - q) / b
        fractional_kelly_f = full_kelly_f * self.KELLY_FRACTION

        # Hard cap at MAX_KELLY_BET_PCT
        capped_f = min(fractional_kelly_f, self.MAX_KELLY_BET_PCT)
        capped_f = max(capped_f, 0.0)

        tier_mult = self.get_tier_multiplier(balance)
        size_usd = balance * capped_f * tier_mult

        log.debug(
            f"Kelly calc | p={p:.3f} b={b:.3f} full_k={full_kelly_f:.4f} "
            f"frac_k={fractional_kelly_f:.4f} capped={capped_f:.4f} size=${size_usd:.4f}"
        )
        return size_usd

    def get_tier_multiplier(self, balance: float) -> float:
        """
        Scales position sizing as equity grows.
        Tier 1 ($0–$50):   0.7x Kelly  — ultra-conservative, protect seed capital
        Tier 2 ($50–$200): 1.0x Kelly  — standard fractional Kelly
        Tier 3 ($200–$1k): 1.2x Kelly  — scale up with proven edge
        Tier 4 ($1k+):     1.5x Kelly  — full compounding mode
        Returns a multiplier applied to the fractional Kelly output.
        """
        if balance < 50:
            return 0.7
        elif balance < 200:
            return 1.0
        elif balance < 1000:
            return 1.2
        else:
            return 1.5

    def max_concurrent_positions(self) -> int:
        balance = self.portfolio.balance
        if balance < 50: return 1
        elif balance < 200: return 2
        elif balance < 500: return 4
        else: return 6

    # ── Circuit Breaker Controls ─────────────

    def _trigger_cb(self, reason: str, loss_pct: float):
        if not self.cb.triggered:
            self.cb.triggered = True
            self.cb.reason = reason
            self.cb.triggered_at = datetime.now(timezone.utc).isoformat()
            decision_log.log_circuit_breaker(reason, loss_pct)
            log.critical(f"🚨 CIRCUIT BREAKER: {reason} | loss={loss_pct:.2%}")

    def reset_circuit_breaker(self, manual: bool = False):
        """
        Manual reset only — bot does NOT auto-reset circuit breakers.
        Requires explicit operator action for safety.
        """
        if manual:
            self.cb = CircuitBreakerState()
            log.warning("⚠️ Circuit breaker manually reset by operator.")

    def record_api_error(self):
        self.cb.api_error_streak += 1
        if self.cb.api_error_streak >= self.HARD_MAX_API_ERROR_STREAK:
            self._trigger_cb("api_error_streak", 0.0)

    def record_api_success(self):
        self.cb.api_error_streak = 0

    def record_trade_result(self, win: bool):
        if win:
            self.cb.consecutive_losses = 0
        else:
            self.cb.consecutive_losses += 1
            if self.cb.consecutive_losses >= self.HARD_MAX_CONSECUTIVE_LOSSES:
                decision_log.log_risk_event(
                    "consecutive_losses",
                    {"count": self.cb.consecutive_losses},
                )

    # ── Daily Reset ───────────────────────────

    def _check_daily_reset(self):
        today = datetime.now(timezone.utc).date()
        if self._daily_reset_date != today:
            self._daily_reset_date = today
            self.portfolio.new_day()
            # Reset daily circuit breakers (drawdown CB persists)
            self.cb.daily_loss_hit = False
            log.info(f"Daily risk reset for {today}")

    # ── Sizing Helpers ────────────────────────

    def compute_partial_exit_size(self, position_shares: float) -> float:
        """
        Partial exit: take {profit_partial_exit_pct} of shares off at target profit.
        """
        return round(position_shares * settings.profit_partial_exit_pct, 2)

    def should_partial_exit(self, entry_price: float, current_price: float, side: str) -> bool:
        """Returns True if position has hit the partial exit profit target (+30%)."""
        if side == "YES":
            profit_pct = (current_price - entry_price) / entry_price
        else:
            profit_pct = (entry_price - current_price) / entry_price
        return profit_pct >= 0.30

    def should_stop_loss(self, entry_price: float, current_price: float, side: str) -> bool:
        """
        Hard stop loss at -50% of position value.
        For prediction markets, a position at 0.1 that cost 0.20 is a -50% loss.
        """
        if side == "YES":
            loss_pct = (entry_price - current_price) / entry_price
        else:
            loss_pct = (current_price - entry_price) / entry_price
        return loss_pct >= 0.50

    def trailing_stop_triggered(self, peak_price: float, current_price: float,
                                 side: str, trail_pct: float = 0.15) -> bool:
        """Trailing stop — if price retreats trail_pct% from peak, exit remainder."""
        if side == "YES":
            retreat = (peak_price - current_price) / peak_price
        else:
            retreat = (current_price - peak_price) / peak_price
        return retreat >= trail_pct

    def is_paused(self) -> bool:
        return self.cb.triggered

    def status(self) -> dict:
        return {
            "circuit_breaker": self.cb.triggered,
            "cb_reason": self.cb.reason,
            "cb_triggered_at": self.cb.triggered_at,
            "consecutive_losses": self.cb.consecutive_losses,
            "api_error_streak": self.cb.api_error_streak,
            "drawdown_pct": self.portfolio.drawdown_pct,
            "daily_loss_pct": self.portfolio.daily_loss_pct,
            "deployed_pct": self.portfolio.deployed_pct,
            "free_usd": self.portfolio.free_usd,
        }
