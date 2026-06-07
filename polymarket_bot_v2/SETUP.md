# PolyBot — Complete Setup & Operations Guide

## ⚠️ RISK DISCLAIMER
This bot trades on Polymarket with REAL money in live mode.
Prediction market trading involves substantial financial risk.
Start with `--mode dryrun`, then `--mode paper` for 7-14 days minimum.
Never deploy more capital than you can afford to lose entirely.

---

## Prerequisites
- Python 3.11+
- Polygon wallet with USDC (for live mode)
- Polymarket account (international version, non-US)
- Telegram bot token (optional but recommended)

---

## Installation

### Option A: Local (recommended for development)
```bash
# Clone / extract the project
cd polymarket_bot

# Create virtual environment
python3.11 -m venv venv
source venv/bin/activate      # Linux/Mac
# venv\Scripts\activate       # Windows

# Install dependencies
pip install -r requirements.txt

# Copy and edit config
cp .env.example .env
nano .env   # Fill in your values
```

### Option B: Docker (recommended for VPS / production)
```bash
cp .env.example .env
nano .env          # Fill in your values
docker-compose up -d --build
docker-compose logs -f
```

---

## Configuration (.env)

### Minimum for Dryrun (no keys needed):
```env
MODE=dryrun
STARTING_CAPITAL=10.0
```

### For Live Trading:
1. Get Polymarket CLOB API keys from: https://docs.polymarket.com/
2. Export your Polygon wallet private key (MetaMask → Account Details → Export)
3. Fund wallet with USDC on Polygon network
4. Fill all fields in .env

---

## Running the Bot

### Phase 1: Dry Run (1-3 days) — ALWAYS start here
```bash
python main.py --mode dryrun --capital 10
```
- Real market data, zero execution
- Full logging of "would-have" trades
- Monitor logs/ folder for signal quality

### Phase 2: Paper Trading (7-14 days minimum)
```bash
python main.py --mode paper --capital 10
```
- Simulates real execution with virtual balance
- Watch win rate — must be >75% before going live

### Phase 3: Backtest (run before paper trading)
```bash
python backtester.py --months 6 --capital 10
```
- Tests on 6 months of resolved markets
- Generates data/backtest_results.json
- Only proceed to live if all strategies PASS

### Phase 4: Live (only after paper trading validates)
```bash
python main.py --mode live --capital 10
```
- Requires explicit confirmation prompt
- Real USDC orders on Polymarket

---

## Monitoring

### Logs
```bash
tail -f logs/bot_YYYYMMDD.log          # Full structured log
grep "SIGNAL_ACCEPTED" logs/*.log      # Filter accepted trades
grep "CIRCUIT_BREAKER" logs/*.log      # Filter risk events
grep "EXIT" logs/*.log                 # Filter all exits
grep "DAILY_SUMMARY" logs/*.log        # Filter daily summaries
```

### Database queries (SQLite)
```bash
sqlite3 data/polybot.db

# Open positions
SELECT market_id, side, price, usd_amount, strategy FROM trades WHERE status='open';

# Today's P&L
SELECT SUM(pnl_usd), COUNT(*) FROM trades WHERE status='closed' AND timestamp LIKE date('now')||'%';

# Win rate overall
SELECT 
  COUNT(*) total,
  SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) wins,
  ROUND(100.0 * SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) / COUNT(*), 1) win_rate_pct
FROM trades WHERE status='closed';

# Arb opportunities log
SELECT market_question, yes_price, no_price, expected_profit_pct, acted FROM arb_log ORDER BY timestamp DESC LIMIT 20;
```

---

## Emergency Procedures

### Pause bot immediately
```bash
# Send SIGTERM (graceful shutdown)
kill -TERM $(pgrep -f "python main.py")

# Docker
docker-compose stop
```

### Reset circuit breaker (after manual review)
```bash
python main.py --reset-cb
```

### Emergency close all positions (live mode)
```python
# In Python REPL
import asyncio
from modules.executor import Executor
# ... see emergency_close_all() method in executor.py
```

### Restore from backup
```bash
cp data/polybot.db.bak data/polybot.db
python main.py --mode paper --capital <restored_balance>
```

---

## VPS Deployment (Dublin / Ireland for low latency)

### Recommended: AWS eu-west-1 or Hetzner Helsinki
```bash
# systemd service (auto-restart)
sudo nano /etc/systemd/system/polybot.service
```

```ini
[Unit]
Description=PolyBot Trading Bot
After=network.target

[Service]
Type=simple
User=ubuntu
WorkingDirectory=/home/ubuntu/polymarket_bot
ExecStart=/home/ubuntu/polymarket_bot/venv/bin/python main.py --mode live --capital 10
Restart=on-failure
RestartSec=30
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable polybot
sudo systemctl start polybot
sudo journalctl -u polybot -f
```

---

## Strategy Allocation (Automatic)
| Strategy | Allocation | Win Rate Target | Notes |
|----------|-----------|-----------------|-------|
| Arb      | 65%       | >95%            | YES+NO sum < 0.97 |
| Copy     | 25%       | >75%            | Top-wallet mirroring |
| Signal   | 10%       | >80%            | Extreme prob / fade hype |

---

## Key Risk Parameters
| Parameter | Default | Meaning |
|-----------|---------|---------|
| MAX_RISK_PER_TRADE_PCT | 8% | Max $ per trade as % of balance |
| MAX_DAILY_LOSS_PCT | 15% | Daily loss limit → auto-pause |
| DRAWDOWN_CIRCUIT_BREAKER | 25% | Total drawdown → emergency stop |
| MAX_CAPITAL_DEPLOYED_PCT | 35% | Max capital in open positions |
| MIN_EDGE_THRESHOLD | 75% | Minimum edge to take any trade |
| ARB_MAX_SUM | 0.97 | YES+NO must be below this for arb |

---

## File Structure
```
polymarket_bot/
├── main.py                    ← Bot orchestrator (start here)
├── config.py                  ← All settings (reads .env)
├── backtester.py              ← Historical backtest engine
├── requirements.txt
├── Dockerfile
├── docker-compose.yml
├── .env.example               ← Copy to .env, fill in values
├── modules/
│   ├── database.py            ← SQLite persistence
│   ├── logger.py              ← Structured logging
│   ├── telegram_notifier.py   ← Async Telegram alerts
│   ├── portfolio_tracker.py   ← Balance, positions, P&L
│   ├── risk_manager.py        ← Kelly sizing, circuit breakers
│   ├── scanner.py             ← Market data (WS + REST)
│   ├── arb_detector.py        ← Arbitrage detection (primary)
│   ├── copy_trader.py         ← Wallet copy strategy (secondary)
│   ├── signal_generator.py    ← Directional signals (tertiary)
│   └── executor.py            ← Order execution engine
├── dashboard/
│   └── console_dashboard.py   ← Live Rich console dashboard
├── data/                      ← SQLite DB (auto-created)
└── logs/                      ← Rotating daily logs (auto-created)
```
