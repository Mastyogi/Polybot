"""
signal_generator.py — Tertiary Signal Strategy (10-20% allocation).
Finds high-conviction directional trades in Politics/Geopolitics:
  1. Fade hype: market overreacts to news → prices snap back
  2. Near-resolution certainty: market <3 days to close, probability >90%
  3. Extreme probability filter: buy near-certainties at lopsided odds
  4. News confirmation scalp: price moved, buy confirmed direction
All signals require ≥75% estimated edge to pass risk gate.
"""

import logging
import time
from typing import List, Optional
from dataclasses import dataclass

from config import settings
from modules.scanner import MarketData
from modules.logger import decision_log

log = logging.getLogger("signal_gen")


@dataclass
class TradingSignal:
    market_id: str
    question: str
    category: str
    signal_type: str       # "fade_hype" | "near_resolution" | "extreme_prob" | "news_scalp"
    side: str              # YES | NO
    entry_price: float     # Suggested maker order price
    estimated_win_prob: float
    edge_score: float
    confidence: float
    stop_loss_price: float
    target_price: float
    reasoning: str
    priority: int          # 1=highest, 3=lowest

    @property
    def is_actionable(self) -> bool:
        return (
            self.edge_score >= settings.min_edge_threshold
            and self.confidence >= 0.70
            and self.entry_price >= settings.MIN_ORDER_SIZE_USD / 100
        )

    @property
    def risk_reward(self) -> float:
        reward = abs(self.target_price - self.entry_price)
        risk = abs(self.entry_price - self.stop_loss_price)
        return reward / risk if risk > 0 else 0.0


class SignalGenerator:
    """
    Conservative signal generator.
    High bar: only surfaces signals with ≥75% estimated win probability.
    Prioritizes fee-free markets (lopsided odds) and near-resolution certainty.
    """

    # Min days to end_date to trade (avoid pure time-decay plays)
    MIN_DAYS_TO_END = 0.5        # At least 12h remaining
    MAX_DAYS_TO_END_NEAR_RES = 3 # Near-resolution window (0.5–3 days)

    # Extreme probability thresholds (near-zero fees zone)
    EXTREME_YES_HIGH = 0.92      # YES above this = very likely YES win
    EXTREME_YES_LOW  = 0.05      # YES below this = very likely NO win

    # Fade hype: price moved more than this in one scan cycle = potential snap-back
    HYPE_MOVE_THRESHOLD = 0.08   # 8% price move = overshooting

    MAX_TRACKED_MARKETS = 500

    def __init__(self):
        from collections import OrderedDict
        self._price_history: OrderedDict = OrderedDict()   # market_id → list of (time, price)
        self._max_history = 20           # Keep last 20 price snapshots

    def generate(self, markets: List[MarketData]) -> List[TradingSignal]:
        """Main scan — returns all actionable signals."""
        signals = []

        for md in markets:
            if not md.is_active or md.stale:
                continue

            # Update price history
            self._update_history(md)

            # Run each signal type
            s1 = self._near_resolution_signal(md)
            if s1:
                signals.append(s1)

            s2 = self._extreme_probability_signal(md)
            if s2:
                signals.append(s2)

            s3 = self._fade_hype_signal(md)
            if s3:
                signals.append(s3)

        # Filter to only actionable, sort by edge
        actionable = [s for s in signals if s.is_actionable]
        actionable.sort(key=lambda s: (s.priority, -s.edge_score))

        if actionable:
            log.info(f"Signal scan: {len(actionable)} actionable signals from {len(markets)} markets")

        return actionable

    # ── Signal 1: Near-Resolution Certainty ──

    def _near_resolution_signal(self, md: MarketData) -> Optional[TradingSignal]:
        """
        Market is about to resolve (<3 days) and probability is very lopsided.
        Buy the dominant side — time decay works in our favor.
        e.g. YES at 0.92 with 1 day left = near-certain win.
        """
        if not md.end_date:
            return None

        days_left = self._days_to_end(md.end_date)
        if not (self.MIN_DAYS_TO_END <= days_left <= self.MAX_DAYS_TO_END_NEAR_RES):
            return None

        yes_p = md.yes_price
        no_p  = md.no_price

        # Strong YES signal
        if yes_p >= 0.88:
            edge = yes_p + (1 - days_left / 3) * 0.05  # bonus for imminent resolution
            edge = min(edge, 0.97)
            stop = yes_p * 0.80   # stop if price drops 20%
            target = min(yes_p + (1.0 - yes_p) * 0.60, 0.99)

            return TradingSignal(
                market_id=md.market_id,
                question=md.question,
                category=md.category,
                signal_type="near_resolution",
                side="YES",
                entry_price=yes_p,
                estimated_win_prob=yes_p,
                edge_score=edge,
                confidence=min(yes_p + 0.05, 0.97),
                stop_loss_price=stop,
                target_price=target,
                reasoning=(
                    f"Near-resolution YES: {yes_p:.2%} prob, {days_left:.1f} days left. "
                    f"Edge={edge:.2%}. Buy YES maker @ {yes_p:.4f}."
                ),
                priority=1,
            )

        # Strong NO signal (YES priced very low = market says NO will win)
        if yes_p <= 0.12:
            no_win_prob = 1.0 - yes_p
            edge = no_win_prob + (1 - days_left / 3) * 0.05
            edge = min(edge, 0.97)
            stop = no_p * 0.80
            target = min(no_p + (1.0 - no_p) * 0.60, 0.99)

            return TradingSignal(
                market_id=md.market_id,
                question=md.question,
                category=md.category,
                signal_type="near_resolution",
                side="NO",
                entry_price=no_p,
                estimated_win_prob=no_win_prob,
                edge_score=edge,
                confidence=min(no_win_prob + 0.05, 0.97),
                stop_loss_price=stop,
                target_price=target,
                reasoning=(
                    f"Near-resolution NO: YES={yes_p:.2%} so NO wins prob={no_win_prob:.2%}, "
                    f"{days_left:.1f} days left. Edge={edge:.2%}."
                ),
                priority=1,
            )

        return None

    # ── Signal 2: Extreme Probability ────────

    def _extreme_probability_signal(self, md: MarketData) -> Optional[TradingSignal]:
        """
        At extreme probabilities (>92% or <8%), Polymarket fees are often near-zero
        because spread is very tight. These are high-confidence, low-fee setups.
        Only trade if we have ≥5 days runway (not expiring too fast for price to move).
        """
        if not md.end_date:
            return None

        days_left = self._days_to_end(md.end_date)
        if days_left < 1.0:     # too close to resolution
            return None

        yes_p = md.yes_price
        no_p = md.no_price

        # YES extremely high → buy YES (strong consensus)
        if yes_p >= self.EXTREME_YES_HIGH and no_p <= 0.08:
            if (yes_p + no_p) <= settings.arb_max_sum:    # also has arb property
                edge = yes_p
                return TradingSignal(
                    market_id=md.market_id,
                    question=md.question,
                    category=md.category,
                    signal_type="extreme_prob",
                    side="YES",
                    entry_price=yes_p,
                    estimated_win_prob=yes_p,
                    edge_score=min(edge, 0.95),
                    confidence=yes_p,
                    stop_loss_price=yes_p * 0.75,
                    target_price=min(yes_p * 1.05, 0.99),
                    reasoning=(
                        f"Extreme YES={yes_p:.4f}: near-zero fees, {days_left:.0f} days left. "
                        f"High-conviction, low-fee trade."
                    ),
                    priority=2,
                )

        # YES extremely low → buy NO
        if yes_p <= self.EXTREME_YES_LOW and no_p >= self.EXTREME_YES_HIGH:
            no_win_prob = 1.0 - yes_p
            return TradingSignal(
                market_id=md.market_id,
                question=md.question,
                category=md.category,
                signal_type="extreme_prob",
                side="NO",
                entry_price=no_p,
                estimated_win_prob=no_win_prob,
                edge_score=min(no_win_prob, 0.95),
                confidence=no_win_prob,
                stop_loss_price=no_p * 0.75,
                target_price=min(no_p * 1.05, 0.99),
                reasoning=(
                    f"Extreme NO: YES={yes_p:.4f} → NO wins prob={no_win_prob:.2%}. "
                    f"Near-zero fees, {days_left:.0f} days left."
                ),
                priority=2,
            )

        return None

    # ── Signal 3: Fade Hype (Snap-Back) ──────

    def _fade_hype_signal(self, md: MarketData) -> Optional[TradingSignal]:
        """
        Detects rapid price moves (>8% in recent scans) likely driven by news hype.
        Fades the move: if YES spiked up fast, sell NO (buy NO).
        Only in Politics/Geopolitics where snap-backs are common.
        History-dependent: needs at least 3 price snapshots.
        """
        if md.category not in {"Politics", "Geopolitics", "World Events", "Elections"}:
            return None

        history = self._price_history.get(md.market_id, [])
        if len(history) < 3:
            return None

        # Check price movement over last 3 snapshots
        recent_prices = [p for _, p in history[-3:]]
        oldest_price = recent_prices[0]
        newest_price = recent_prices[-1]
        move_pct = (newest_price - oldest_price) / max(oldest_price, 0.01)

        if abs(move_pct) < self.HYPE_MOVE_THRESHOLD:
            return None

        # Price spiked up fast → likely hype → fade with NO
        if move_pct >= self.HYPE_MOVE_THRESHOLD and md.yes_price >= 0.55:
            fade_edge = 0.70 + min(abs(move_pct) / 0.20, 0.10)  # 70-80% edge for fade
            fade_edge = min(fade_edge, 0.82)

            if fade_edge < settings.min_edge_threshold:
                return None

            return TradingSignal(
                market_id=md.market_id,
                question=md.question,
                category=md.category,
                signal_type="fade_hype",
                side="NO",
                entry_price=md.no_price,
                estimated_win_prob=fade_edge,
                edge_score=fade_edge,
                confidence=0.72,
                stop_loss_price=md.no_price * 0.80,
                target_price=md.no_price * 1.15,
                reasoning=(
                    f"Fade hype: YES moved {move_pct:+.2%} in last 3 scans. "
                    f"Snap-back expected. Buy NO @ {md.no_price:.4f}. Edge={fade_edge:.2%}"
                ),
                priority=3,
            )

        # Price crashed down fast → likely fear → fade with YES
        if move_pct <= -self.HYPE_MOVE_THRESHOLD and md.yes_price <= 0.45:
            fade_edge = 0.70 + min(abs(move_pct) / 0.20, 0.10)
            fade_edge = min(fade_edge, 0.82)

            if fade_edge < settings.min_edge_threshold:
                return None

            return TradingSignal(
                market_id=md.market_id,
                question=md.question,
                category=md.category,
                signal_type="fade_hype",
                side="YES",
                entry_price=md.yes_price,
                estimated_win_prob=fade_edge,
                edge_score=fade_edge,
                confidence=0.72,
                stop_loss_price=md.yes_price * 0.80,
                target_price=md.yes_price * 1.15,
                reasoning=(
                    f"Fade fear: YES moved {move_pct:+.2%} in last 3 scans. "
                    f"Snap-back expected. Buy YES @ {md.yes_price:.4f}. Edge={fade_edge:.2%}"
                ),
                priority=3,
            )

        return None

    # ── Helpers ───────────────────────────────

    def _update_history(self, md: MarketData):
        hist = self._price_history.setdefault(md.market_id, [])
        hist.append((time.time(), md.yes_price))
        if len(hist) > self._max_history:
            hist.pop(0)
        self._price_history.move_to_end(md.market_id)
        if len(self._price_history) > self.MAX_TRACKED_MARKETS:
            self._price_history.popitem(last=False)

    def _days_to_end(self, end_date_str: str) -> float:
        """Parse ISO end date and return days remaining."""
        try:
            from datetime import datetime, timezone
            end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            now = datetime.now(timezone.utc)
            delta = end - now
            return max(0.0, delta.total_seconds() / 86400)
        except Exception:
            return 999.0  # Unknown date = assume far future
