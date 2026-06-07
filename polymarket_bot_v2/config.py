"""
config.py — Central configuration loader.
All settings validated via Pydantic. Single source of truth.
"""

from pydantic_settings import BaseSettings, SettingsConfigDict
from pydantic import Field, field_validator
from decimal import Decimal
from enum import Enum
from pathlib import Path


class BotMode(str, Enum):
    DRYRUN = "dryrun"    # Real data, zero execution, full logging
    PAPER = "paper"      # Live sim with virtual balance, no real orders
    LIVE = "live"        # Real money, real orders


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Wallet ---
    private_key: str = Field(default="", alias="PRIVATE_KEY")
    wallet_address: str = Field(default="", alias="WALLET_ADDRESS")

    # --- CLOB API ---
    clob_api_key: str = Field(default="", alias="CLOB_API_KEY")
    clob_secret: str = Field(default="", alias="CLOB_SECRET")
    clob_passphrase: str = Field(default="", alias="CLOB_PASSPHRASE")
    clob_host: str = Field(default="https://clob.polymarket.com", alias="CLOB_HOST")
    gamma_api: str = Field(default="https://gamma-api.polymarket.com", alias="GAMMA_API")

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", alias="TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field(default="", alias="TELEGRAM_CHAT_ID")

    # --- Capital & Risk ---
    starting_capital: float = Field(default=10.0, alias="STARTING_CAPITAL")
    max_risk_per_trade_pct: float = Field(default=0.08, alias="MAX_RISK_PER_TRADE_PCT")
    max_daily_loss_pct: float = Field(default=0.15, alias="MAX_DAILY_LOSS_PCT")
    drawdown_circuit_breaker: float = Field(default=0.25, alias="DRAWDOWN_CIRCUIT_BREAKER")
    max_capital_deployed_pct: float = Field(default=0.35, alias="MAX_CAPITAL_DEPLOYED_PCT")
    min_edge_threshold: float = Field(default=0.75, alias="MIN_EDGE_THRESHOLD")
    profit_partial_exit_pct: float = Field(default=0.45, alias="PROFIT_PARTIAL_EXIT_PCT")

    # --- Strategy Weights ---
    arb_weight: float = Field(default=0.65, alias="ARB_WEIGHT")
    copy_weight: float = Field(default=0.25, alias="COPY_WEIGHT")
    signal_weight: float = Field(default=0.10, alias="SIGNAL_WEIGHT")

    # --- Copy Trading ---
    min_wallet_win_rate: float = Field(default=0.70, alias="MIN_WALLET_WIN_RATE")
    min_wallet_trades: int = Field(default=30, alias="MIN_WALLET_TRADES")
    max_wallet_drawdown: float = Field(default=0.20, alias="MAX_WALLET_DRAWDOWN")
    max_wallets_to_track: int = Field(default=5, alias="MAX_WALLETS_TO_TRACK")

    # --- Arb ---
    arb_max_sum: float = Field(default=0.97, alias="ARB_MAX_SUM")
    arb_min_profit_pct: float = Field(default=0.035, alias="ARB_MIN_PROFIT_PCT")  # Effective floor for $5–$50 positions after market impact

    # --- Operational ---
    mode: BotMode = Field(default=BotMode.DRYRUN, alias="MODE")
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    polygon_rpc: str = Field(default="https://polygon-rpc.com", alias="POLYGON_RPC")
    ireland_optimized: bool = Field(default=True, alias="IRELAND_OPTIMIZED")

    # --- Static Constants (not from env) ---
    TAKER_FEE: float = 0.01          # 1% taker fee (conservative est.)
    MAKER_FEE: float = 0.0           # 0% maker fee
    MIN_ORDER_SIZE_USD: float = 1.0  # Polymarket minimum order
    MIN_SHARES: float = 5.0          # Minimum shares per order
    SCAN_INTERVAL_SEC: int = 30      # Market scan every 30 seconds
    COPY_SCAN_INTERVAL_SEC: int = 60 # Copy trading scan every 60 sec
    WS_RECONNECT_DELAY: int = 5      # WebSocket reconnect delay
    MAX_RETRIES: int = 5             # Max API retries
    DB_PATH: str = "data/polybot.db"
    LOG_DIR: str = "logs"

    @field_validator("max_risk_per_trade_pct")
    @classmethod
    def cap_risk(cls, v: float) -> float:
        """Hard cap: never more than 10% risk per trade."""
        return min(v, 0.10)

    @field_validator("mode", mode="before")
    @classmethod
    def normalize_mode(cls, v: str) -> str:
        return v.lower().strip()

    def is_live(self) -> bool:
        return self.mode == BotMode.LIVE

    def is_simulation(self) -> bool:
        return self.mode in (BotMode.DRYRUN, BotMode.PAPER)

    def validate_live_keys(self) -> bool:
        """Ensure all required keys are present before going live."""
        if self.is_live():
            return all([
                self.private_key,
                self.wallet_address,
                self.clob_api_key,
                self.clob_secret,
                self.clob_passphrase,
            ])
        return True

    def override(self, **kwargs) -> "Settings":
        """Returns a copy of the settings with overridden values."""
        return self.model_copy(update=kwargs)


# Global singleton
settings = Settings()
