"""
database.py — SQLite persistence layer (async via aiosqlite).
Stores: trades, positions, daily snapshots, wallet tracking, arb log.
"""

import asyncio
import aiosqlite
import json
from datetime import datetime, date, timezone
from pathlib import Path
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, asdict
from config import settings


DB_PATH = Path(settings.DB_PATH)
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

# ──────────────────────────────────────────
# Data Models (lightweight dataclasses)
# ──────────────────────────────────────────

@dataclass
class Trade:
    id: Optional[int]
    timestamp: str
    mode: str               # dryrun | paper | live
    strategy: str           # arb | copy | signal
    market_id: str
    market_question: str
    side: str               # YES | NO
    price: float            # Entry price (probability 0-1)
    shares: float
    usd_amount: float
    estimated_edge: float
    status: str             # open | closed | cancelled
    exit_price: Optional[float]
    pnl_usd: Optional[float]
    exit_reason: Optional[str]
    order_id: Optional[str]
    meta: Optional[str]     # JSON blob for extra data


@dataclass
class DailySnapshot:
    id: Optional[int]
    snapshot_date: str
    balance_usd: float
    open_positions_count: int
    realized_pnl_today: float
    unrealized_pnl: float
    total_trades_today: int
    win_trades_today: int
    daily_return_pct: float


@dataclass
class TrackedWallet:
    address: str
    win_rate: float
    total_trades: int
    avg_profit_per_trade: float
    max_drawdown: float
    last_updated: str
    active: bool


# ──────────────────────────────────────────
# Database Manager
# ──────────────────────────────────────────

class Database:
    _instance = None

    def __init__(self):
        self.db_path = str(DB_PATH)
        self._conn: Optional[aiosqlite.Connection] = None

    @classmethod
    async def get(cls) -> "Database":
        if cls._instance is None:
            cls._instance = Database()
            await cls._instance.initialize()
        return cls._instance

    async def initialize(self):
        """Create tables if not exist."""
        self._conn = await aiosqlite.connect(self.db_path)
        self._conn.row_factory = aiosqlite.Row
        await self._create_tables()

    async def _create_tables(self):
        async with self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                mode TEXT NOT NULL,
                strategy TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_question TEXT,
                side TEXT NOT NULL,
                price REAL NOT NULL,
                shares REAL NOT NULL,
                usd_amount REAL NOT NULL,
                estimated_edge REAL NOT NULL,
                status TEXT NOT NULL DEFAULT 'open',
                exit_price REAL,
                pnl_usd REAL,
                exit_reason TEXT,
                order_id TEXT,
                meta TEXT
            );

            CREATE TABLE IF NOT EXISTS daily_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                snapshot_date TEXT NOT NULL UNIQUE,
                balance_usd REAL NOT NULL,
                open_positions_count INTEGER DEFAULT 0,
                realized_pnl_today REAL DEFAULT 0.0,
                unrealized_pnl REAL DEFAULT 0.0,
                total_trades_today INTEGER DEFAULT 0,
                win_trades_today INTEGER DEFAULT 0,
                daily_return_pct REAL DEFAULT 0.0
            );

            CREATE TABLE IF NOT EXISTS tracked_wallets (
                address TEXT PRIMARY KEY,
                win_rate REAL NOT NULL,
                total_trades INTEGER NOT NULL,
                avg_profit_per_trade REAL NOT NULL,
                max_drawdown REAL NOT NULL,
                last_updated TEXT NOT NULL,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS arb_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                market_id TEXT NOT NULL,
                market_question TEXT,
                yes_price REAL NOT NULL,
                no_price REAL NOT NULL,
                sum_price REAL NOT NULL,
                expected_profit_pct REAL NOT NULL,
                acted INTEGER NOT NULL DEFAULT 0,
                skip_reason TEXT
            );

            CREATE TABLE IF NOT EXISTS bot_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_trades_status ON trades(status);
            CREATE INDEX IF NOT EXISTS idx_trades_strategy ON trades(strategy);
            CREATE INDEX IF NOT EXISTS idx_trades_timestamp ON trades(timestamp);
        """):
            pass
        await self._conn.commit()

    # ── Trades ──────────────────────────────

    async def insert_trade(self, trade: Trade) -> int:
        cur = await self._conn.execute("""
            INSERT INTO trades (timestamp, mode, strategy, market_id, market_question,
                side, price, shares, usd_amount, estimated_edge, status,
                exit_price, pnl_usd, exit_reason, order_id, meta)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade.timestamp, trade.mode, trade.strategy, trade.market_id,
            trade.market_question, trade.side, trade.price, trade.shares,
            trade.usd_amount, trade.estimated_edge, trade.status,
            trade.exit_price, trade.pnl_usd, trade.exit_reason,
            trade.order_id, trade.meta,
        ))
        await self._conn.commit()
        return cur.lastrowid

    async def close_trade(self, trade_id: int, exit_price: float, pnl_usd: float, reason: str):
        await self._conn.execute("""
            UPDATE trades SET status='closed', exit_price=?, pnl_usd=?, exit_reason=?
            WHERE id=?
        """, (exit_price, pnl_usd, reason, trade_id))
        await self._conn.commit()

    async def get_open_trades(self) -> List[Dict]:
        async with self._conn.execute(
            "SELECT * FROM trades WHERE status='open' ORDER BY timestamp DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_trades_today(self) -> List[Dict]:
        today = datetime.now(timezone.utc).date().isoformat()
        async with self._conn.execute(
            "SELECT * FROM trades WHERE timestamp LIKE ? ORDER BY timestamp DESC",
            (f"{today}%",),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    async def get_all_closed_trades(self) -> List[Dict]:
        async with self._conn.execute(
            "SELECT * FROM trades WHERE status='closed' ORDER BY timestamp DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ── Snapshots ────────────────────────────

    async def upsert_snapshot(self, snap: DailySnapshot):
        await self._conn.execute("""
            INSERT INTO daily_snapshots
                (snapshot_date, balance_usd, open_positions_count, realized_pnl_today,
                 unrealized_pnl, total_trades_today, win_trades_today, daily_return_pct)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_date) DO UPDATE SET
                balance_usd=excluded.balance_usd,
                open_positions_count=excluded.open_positions_count,
                realized_pnl_today=excluded.realized_pnl_today,
                unrealized_pnl=excluded.unrealized_pnl,
                total_trades_today=excluded.total_trades_today,
                win_trades_today=excluded.win_trades_today,
                daily_return_pct=excluded.daily_return_pct
        """, (
            snap.snapshot_date, snap.balance_usd, snap.open_positions_count,
            snap.realized_pnl_today, snap.unrealized_pnl,
            snap.total_trades_today, snap.win_trades_today, snap.daily_return_pct,
        ))
        await self._conn.commit()

    async def get_snapshots(self, days: int = 30) -> List[Dict]:
        async with self._conn.execute(
            "SELECT * FROM daily_snapshots ORDER BY snapshot_date DESC LIMIT ?",
            (days,),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ── Wallets ──────────────────────────────

    async def upsert_wallet(self, wallet: TrackedWallet):
        await self._conn.execute("""
            INSERT INTO tracked_wallets
                (address, win_rate, total_trades, avg_profit_per_trade, max_drawdown, last_updated, active)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(address) DO UPDATE SET
                win_rate=excluded.win_rate,
                total_trades=excluded.total_trades,
                avg_profit_per_trade=excluded.avg_profit_per_trade,
                max_drawdown=excluded.max_drawdown,
                last_updated=excluded.last_updated,
                active=excluded.active
        """, (
            wallet.address, wallet.win_rate, wallet.total_trades,
            wallet.avg_profit_per_trade, wallet.max_drawdown,
            wallet.last_updated, int(wallet.active),
        ))
        await self._conn.commit()

    async def get_active_wallets(self) -> List[Dict]:
        async with self._conn.execute(
            "SELECT * FROM tracked_wallets WHERE active=1 ORDER BY win_rate DESC"
        ) as cur:
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    # ── Arb Log ──────────────────────────────

    async def log_arb(self, market_id: str, question: str, yes_p: float,
                      no_p: float, profit_pct: float, acted: bool, skip: str = ""):
        await self._conn.execute("""
            INSERT INTO arb_log
                (timestamp, market_id, market_question, yes_price, no_price,
                 sum_price, expected_profit_pct, acted, skip_reason)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            datetime.now(timezone.utc).isoformat(), market_id, question,
            yes_p, no_p, yes_p + no_p, profit_pct, int(acted), skip,
        ))
        await self._conn.commit()

    # ── Bot State ────────────────────────────

    async def set_state(self, key: str, value: Any):
        await self._conn.execute(
            "INSERT OR REPLACE INTO bot_state (key, value) VALUES (?, ?)",
            (key, json.dumps(value)),
        )
        await self._conn.commit()

    async def get_state(self, key: str, default=None) -> Any:
        async with self._conn.execute(
            "SELECT value FROM bot_state WHERE key=?", (key,)
        ) as cur:
            row = await cur.fetchone()
            if row:
                return json.loads(row["value"])
            return default

    async def close(self):
        if self._conn:
            await self._conn.close()
