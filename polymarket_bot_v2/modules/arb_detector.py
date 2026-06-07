"""
arb_detector.py v2 — Arbitrage detection engine with Kelly Criterion sizing.

Improvements over v1:
  - Kelly Criterion: mathematically optimal position sizing per opportunity
  - Time-decay urgency: markets closing soon get priority boost
  - Liquidity-adjusted edge: thin orderbooks reduce effective edge
  - Oracle v2 integration: uses veto/boost flags instead of raw edge
  - Better bundle arb: Jaccard-similarity grouping for mutually exclusive markets
  - Staleness guard: skips markets not updated in last 90 seconds
  - Slippage model: estimates actual fill vs quoted price
"""

import logging
import asyncio
from typing import List, Optional, Dict, Tuple
from dataclasses import dataclass
import time
import math
from datetime import datetime

from config import settings
from modules.scanner import MarketData
from modules.database import Database
from modules.logger import decision_log
from modules.probability_oracle import ProbabilityOracle

log = logging.getLogger("arb_detector")


@dataclass
class ArbOpportunity:
    market_id: str
    question: str
    arb_type: str           # "sum_arb" | "mispricing" | "bundle"
    yes_price: float
    no_price: float
    price_sum: float
    gross_profit_pct: float
    net_profit_pct: float
    confidence: float
    edge_score: float
    kelly_fraction: float        # v2: optimal bet size as % of bankroll
    urgency_multiplier: float    # v2: 1.0–1.5 based on time-to-close
    suggested_side: str
    suggested_price: float
    min_usd_required: float
    reasoning: str

    def is_actionable(self) -> bool:
        return (
            self.net_profit_pct >= settings.arb_min_profit_pct
            and self.edge_score >= settings.min_edge_threshold
            and self.kelly_fraction > 0.001           # Kelly says bet something
            and self.min_usd_required <= settings.starting_capital * 0.35
        )

    def recommended_usd(self, bankroll: float) -> float:
        """Kelly-optimal USD amount for this opportunity."""
        raw = bankroll * self.kelly_fraction * self.urgency_multiplier
        return max(settings.MIN_ORDER_SIZE_USD, min(raw, bankroll * 0.10))


class ArbDetector:
    MAKER_FEE = 0.0000
    TAKER_FEE = 0.0100
    GAS_USD   = 0.001

    # Staleness: skip markets not updated in >90s (stale quote risk)
    MAX_STALE_SEC = 90

    # Minimum liquidity as multiple of order size (safety margin)
    MIN_LIQUIDITY_MULT = 5.0

    def __init__(self):
        self._db: Optional[Database] = None
        self._seen_arbs: Dict[str, float] = {}
        self._oracle: Optional[ProbabilityOracle] = None

    async def initialize(self, oracle=None):
        self._db = await Database.get()
        self._oracle = oracle

    # ── Main scan loop ────────────────────────

    async def scan(self, markets: List[MarketData]) -> List[ArbOpportunity]:
        cutoff = time.time() - 21600
        self._seen_arbs = {k: v for k, v in self._seen_arbs.items() if v > cutoff}

        opportunities = []

        for md in markets:
            if not md.is_active:
                continue
            # Staleness guard (v2 improvement)
            if (time.time() - md.last_updated) > self.MAX_STALE_SEC:
                log.debug("Skipping stale market %s (%.0fs old)", md.market_id,
                          time.time() - md.last_updated)
                continue

            arb = self._check_sum_arb(md)
            if arb:
                opportunities.append(arb)

            misprice = await self._check_mispricing(md)
            if misprice:
                opportunities.append(misprice)

        # Bundle arb across related markets
        bundle_opps = self.check_bundle_arb(markets)
        opportunities.extend(bundle_opps)

        # Sort by Kelly-adjusted expected value
        opportunities.sort(
            key=lambda a: a.net_profit_pct * a.kelly_fraction * a.urgency_multiplier,
            reverse=True,
        )

        # Deduplicate by market_id
        seen, unique = set(), []
        for arb in opportunities:
            if arb.market_id not in seen:
                seen.add(arb.market_id)
                unique.append(arb)

        if unique:
            log.info("Arb scan: %d opportunities from %d markets", len(unique), len(markets))
        return unique

    # ── Kelly Criterion ───────────────────────

    @staticmethod
    def kelly_fraction(
        win_prob: float,
        market_price: float,
        fraction: float = 0.25,
    ) -> float:
        """
        Fractional Kelly bet sizing for prediction markets.

        Simplified prediction-market Kelly formula:
          f* = (p - price) / (1 - price)

        Interpretation:
          win_prob == market_price → f*=0 (no edge)
          win_prob  > market_price → f*>0 (positive edge → bet)
          win_prob  < market_price → f*<0 → clipped to 0

        Quarter Kelly (fraction=0.25) for safety.
        Hard cap at 15% of bankroll per bet.

        Args:
            win_prob:     our estimated probability of win (0–1)
            market_price: price we pay to enter (0–1)
            fraction:     0.25 = quarter Kelly
        """
        if market_price <= 0 or market_price >= 1 or win_prob <= 0:
            return 0.0
        raw_kelly = (win_prob - market_price) / (1.0 - market_price)
        return max(0.0, min(raw_kelly * fraction, 0.15))

    # ── Time Urgency ──────────────────────────

    @staticmethod
    def urgency_multiplier(end_date_str: Optional[str]) -> float:
        """
        Markets closing within 24h → higher urgency (1.3–1.5x multiplier).
        Markets closing in 7d+  → standard (1.0x).
        Closing in 1–7 days     → mild boost (1.1–1.3x).
        """
        if not end_date_str:
            return 1.0
        try:
            end = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
            hours_left = (end.timestamp() - time.time()) / 3600
            if hours_left <= 0:
                return 0.5     # Already resolving, skip
            elif hours_left <= 24:
                return 1.5     # Closes tomorrow — maximum urgency
            elif hours_left <= 72:
                return 1.3
            elif hours_left <= 168:
                return 1.1
            else:
                return 1.0
        except Exception:
            return 1.0

    # ── Liquidity-adjusted edge ───────────────

    @staticmethod
    def liquidity_adj_edge(raw_edge: float, liquidity_usd: float, order_usd: float) -> float:
        """
        Thin orderbooks cause slippage, reducing effective edge.
        Slippage model: order fills at worse price when liquidity < 10x order size.
        """
        if liquidity_usd <= 0:
            return 0.0
        depth_ratio = liquidity_usd / max(order_usd, 1.0)
        if depth_ratio >= 10:
            return raw_edge           # Plenty of liquidity — no adjustment
        elif depth_ratio >= 5:
            return raw_edge * 0.85    # Light slippage
        elif depth_ratio >= 2:
            return raw_edge * 0.65    # Moderate slippage
        else:
            return raw_edge * 0.30    # Thin book — edge mostly eroded

    # ── Strategy 1: Sum Arb ───────────────────

    def _check_sum_arb(self, md: MarketData) -> Optional[ArbOpportunity]:
        price_sum = md.yes_price + md.no_price
        gap = 1.0 - price_sum

        if price_sum > settings.arb_max_sum:
            return None

        fee_cost = self.MAKER_FEE * 2 + self.GAS_USD
        net_profit_pct = gap - fee_cost

        if net_profit_pct < settings.arb_min_profit_pct:
            return None

        min_usd = max(settings.MIN_ORDER_SIZE_USD * 2, 2.0)

        if md.liquidity_usd < (min_usd * self.MIN_LIQUIDITY_MULT):
            log.debug("Sum arb skipped — liquidity $%.2f < $%.2f needed",
                      md.liquidity_usd, min_usd * self.MIN_LIQUIDITY_MULT)
            return None

        # Liquidity-adjusted edge
        adj_edge = self.liquidity_adj_edge(net_profit_pct, md.liquidity_usd, min_usd)
        if adj_edge < settings.arb_min_profit_pct:
            return None

        confidence = min(1.0, gap / 0.05)
        edge_score = min(0.97, 0.80 + confidence * 0.17)

        # Kelly: sum arb — buy cheaper side as the "bet"
        kelly = self.kelly_fraction(
            win_prob=confidence,
            market_price=suggested_price,
        )
        urgency = self.urgency_multiplier(md.end_date)

        suggested_side = "YES" if md.yes_price <= md.no_price else "NO"
        suggested_price = md.yes_price if suggested_side == "YES" else md.no_price

        reasoning = (
            f"Sum arb: YES({md.yes_price:.4f})+NO({md.no_price:.4f})={price_sum:.4f}. "
            f"Gap={gap:.4f} | Net≈{net_profit_pct:.2%} | "
            f"Kelly={kelly:.3f} | Urgency={urgency:.1f}x | "
            f"Liquidity=${md.liquidity_usd:.0f}"
        )

        decision_log.log_arb(md.market_id, md.yes_price, md.no_price, net_profit_pct, acted=False)

        return ArbOpportunity(
            market_id=md.market_id,
            question=md.question,
            arb_type="sum_arb",
            yes_price=md.yes_price,
            no_price=md.no_price,
            price_sum=price_sum,
            gross_profit_pct=gap,
            net_profit_pct=net_profit_pct,
            confidence=confidence,
            edge_score=edge_score,
            kelly_fraction=kelly,
            urgency_multiplier=urgency,
            suggested_side=suggested_side,
            suggested_price=suggested_price,
            min_usd_required=min_usd,
            reasoning=reasoning,
        )

    # ── Strategy 2: Mispricing ────────────────

    async def _check_mispricing(self, md: MarketData) -> Optional[ArbOpportunity]:
        yes_p, no_p = md.yes_price, md.no_price

        # Determine if there's a lopsided mispricing
        side, entry_price, implied_win, opp_price = None, None, None, None

        if 0.02 <= yes_p <= 0.12 and no_p >= 0.85:
            side, entry_price = "NO", no_p
            implied_win = 1.0 - yes_p
            opp_price = yes_p
        elif 0.02 <= no_p <= 0.12 and yes_p >= 0.85:
            side, entry_price = "YES", yes_p
            implied_win = 1.0 - no_p
            opp_price = no_p

        if side is None or implied_win < 0.88:
            return None

        min_usd = settings.MIN_ORDER_SIZE_USD
        if md.liquidity_usd < (min_usd * self.MIN_LIQUIDITY_MULT):
            return None

        net_profit = (1.0 - entry_price) - self.MAKER_FEE
        adj_edge = self.liquidity_adj_edge(implied_win, md.liquidity_usd, min_usd)

        edge_score = min(0.95, adj_edge)

        # Oracle v2 check — uses veto/boost flags
        if self._oracle:
            consensus = await self._oracle.get_consensus(md.question, entry_price)
            if consensus:
                if consensus.get("veto"):
                    log.debug("Oracle v2 VETO on %s mispricing", md.market_id)
                    return None
                if consensus.get("boost"):
                    edge_score = min(0.97, edge_score + 0.08)
                    log.debug("Oracle v2 BOOST on %s → edge_score=%.3f", md.market_id, edge_score)

        kelly = self.kelly_fraction(win_prob=implied_win, market_price=entry_price)
        urgency = self.urgency_multiplier(md.end_date)

        return ArbOpportunity(
            market_id=md.market_id,
            question=md.question,
            arb_type="mispricing",
            yes_price=yes_p,
            no_price=no_p,
            price_sum=yes_p + no_p,
            gross_profit_pct=1.0 - entry_price,
            net_profit_pct=net_profit,
            confidence=implied_win,
            edge_score=edge_score,
            kelly_fraction=kelly,
            urgency_multiplier=urgency,
            suggested_side=side,
            suggested_price=entry_price,
            min_usd_required=min_usd,
            reasoning=(
                f"Mispricing: {side}={entry_price:.4f} implied win={implied_win:.2%} | "
                f"Kelly={kelly:.3f} | Urgency={urgency:.1f}x | "
                f"Opp side={opp_price:.4f}"
            ),
        )

    # ── Strategy 3: Bundle Arb ────────────────

    def check_bundle_arb(self, markets: List[MarketData]) -> List[ArbOpportunity]:
        """
        Mutually exclusive markets in the same event must sum to 1.0.
        Group by Jaccard similarity on question tokens for accuracy.
        """
        def jaccard(a: str, b: str) -> float:
            ta, tb = set(a.lower().split()), set(b.lower().split())
            if not ta or not tb:
                return 0.0
            return len(ta & tb) / len(ta | tb)

        # Build adjacency groups (Jaccard > 0.5 → same event)
        active = [m for m in markets if m.is_active]
        visited, groups = set(), []

        for i, m in enumerate(active):
            if i in visited:
                continue
            group = [m]
            visited.add(i)
            for j, n in enumerate(active):
                if j in visited or i == j:
                    continue
                if jaccard(m.question, n.question) > 0.50:
                    group.append(n)
                    visited.add(j)
            if len(group) >= 2:
                groups.append(group)

        opportunities = []
        for group in groups:
            total_yes = sum(m.yes_price for m in group)
            if total_yes < 0.92:
                gap = 1.0 - total_yes
                fee = self.MAKER_FEE * len(group)
                net = gap - fee
                if net < settings.arb_min_profit_pct:
                    continue
                cheapest = min(group, key=lambda m: m.yes_price)
                min_usd = settings.MIN_ORDER_SIZE_USD * len(group)
                kelly = self.kelly_fraction(win_prob=0.85, market_price=cheapest.yes_price)
                urgency = self.urgency_multiplier(cheapest.end_date)
                opportunities.append(ArbOpportunity(
                    market_id=cheapest.market_id,
                    question=f"BUNDLE({len(group)}): {cheapest.question[:50]}",
                    arb_type="bundle",
                    yes_price=total_yes / len(group),
                    no_price=0.0,
                    price_sum=total_yes,
                    gross_profit_pct=gap,
                    net_profit_pct=net,
                    confidence=0.82,
                    edge_score=0.82,
                    kelly_fraction=kelly,
                    urgency_multiplier=urgency,
                    suggested_side="YES",
                    suggested_price=cheapest.yes_price,
                    min_usd_required=min_usd,
                    reasoning=(
                        f"Bundle arb: {len(group)} outcomes sum={total_yes:.4f} (<1.0). "
                        f"Net≈{net:.2%} | Kelly={kelly:.3f}"
                    ),
                ))

        return opportunities

    async def log_arb_to_db(self, arb: ArbOpportunity, acted: bool, skip_reason: str = ""):
        if self._db:
            await self._db.log_arb(
                arb.market_id, arb.question,
                arb.yes_price, arb.no_price,
                arb.net_profit_pct, acted, skip_reason,
            )
