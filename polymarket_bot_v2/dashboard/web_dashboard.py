import asyncio
import logging
from typing import Optional, TYPE_CHECKING
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
from pathlib import Path

if TYPE_CHECKING:
    from modules.portfolio_tracker import PortfolioTracker
    from modules.risk_manager import RiskManager

log = logging.getLogger("web_dashboard")

app = FastAPI(title="PolyBot Dashboard")
ROOT = Path(__file__).parent
app.mount("/static", StaticFiles(directory=str(ROOT / "static")), name="static")

class WebDashboard:
    def __init__(self):
        self._portfolio: Optional["PortfolioTracker"] = None
        self._risk: Optional["RiskManager"] = None
        self._running = False
        self._clients = set()
        self._recent_events = []
        self._max_events = 15

    def attach(self, portfolio: "PortfolioTracker", risk: "RiskManager"):
        self._portfolio = portfolio
        self._risk = risk

    def add_event(self, event: str):
        import datetime
        ts = datetime.datetime.utcnow().strftime("%H:%M:%S")
        msg = f"{ts} - {event}"
        self._recent_events.append(msg)
        if len(self._recent_events) > self._max_events:
            self._recent_events.pop(0)
            
    def print_startup_banner(self, capital: float, mode: str):
        # The existing console_dashboard printed this. We'll just log it.
        log.info(f"Web Dashboard init - Mode: {mode}, Capital: ${capital}")

    async def start(self, host: str = "0.0.0.0", port: int = 8765):
        self._running = True
        config = uvicorn.Config(app, host=host, port=port, log_level="error")
        self._server = uvicorn.Server(config)
        app.state.dashboard = self
        asyncio.create_task(self._server.serve())
        asyncio.create_task(self._broadcast_loop())
        print(f"🚀 Web dashboard started on http://{host}:{port}")

    async def stop(self):
        self._running = False
        if hasattr(self, '_server'):
            self._server.should_exit = True

    async def _broadcast_loop(self):
        while self._running:
            await asyncio.sleep(1)
            if not self._clients:
                continue
            
            state = await self._get_state()
            dead_clients = set()
            for client in self._clients:
                try:
                    await client.send_json(state)
                except:
                    dead_clients.add(client)
            
            self._clients -= dead_clients

    async def _get_state(self):
        if not self._portfolio or not self._risk:
            return {"status": "loading"}
            
        p = self._portfolio
        r = self._risk.status()
        
        positions = []
        for pos in p.open_positions.values():
            positions.append({
                "market": pos.market_question,
                "side": pos.side,
                "entry": pos.entry_price,
                "current": pos.current_price,
                "shares": pos.shares,
                "cost": pos.cost_usd,
                "pnl": pos.unrealized_pnl,
                "strategy": pos.strategy
            })

        return {
            "balance": p.balance,
            "starting": p.starting_capital,
            "total_pnl": p.balance - p.starting_capital,
            "daily_pnl": p.realized_pnl_today,
            "unrealized_pnl": p.unrealized_pnl,
            "deployed_usd": p.deployed_usd,
            "deployed_pct": p.deployed_pct,
            "free_usd": p.free_usd,
            "drawdown": p.drawdown_pct,
            "positions": positions,
            "risk": r,
            "events": self._recent_events
        }

dashboard_instance = WebDashboard()

@app.get("/")
async def root():
    return FileResponse(str(ROOT / "static" / "index.html"))

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    dashboard = app.state.dashboard
    dashboard._clients.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        dashboard._clients.remove(websocket)
