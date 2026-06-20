"""Central config loader. Reads .env and exposes typed constants."""
from __future__ import annotations

import os
from pathlib import Path
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

load_dotenv(PROJECT_ROOT / ".env")


def _req(key: str) -> str:
    v = os.getenv(key)
    if not v:
        raise RuntimeError(f"Missing required env var: {key}")
    return v


def _opt(key: str, default: str) -> str:
    return os.getenv(key) or default


def _flag(key: str, default: bool = False) -> bool:
    v = os.getenv(key)
    if v is None:
        return default
    return v.strip().lower() in {"1", "true", "yes", "on"}


# Wallets
MAIN_WALLET_PUBKEY = _opt("MAIN_WALLET_PUBKEY", "")
TRADING_WALLET_PUBKEY = _opt("TRADING_WALLET_PUBKEY", "")
TRADING_WALLET_PRIVKEY = _opt("TRADING_WALLET_PRIVKEY", "")

# RPC
SOLANA_RPC_URL = _opt("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")

# Telegram
TELEGRAM_BOT_TOKEN = _opt("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID = _opt("TELEGRAM_CHAT_ID", "")

# Risk
MAX_CAPITAL_INR = float(_opt("MAX_CAPITAL_INR", "5000"))
RISK_PER_TRADE_PCT = float(_opt("RISK_PER_TRADE_PCT", "2.0"))
MAX_DRAWDOWN_PCT = float(_opt("MAX_DRAWDOWN_PCT", "15.0"))
DAILY_LOSS_HALT_PCT = float(_opt("DAILY_LOSS_HALT_PCT", "5.0"))

# Strategy
EMA_FAST = int(_opt("EMA_FAST", "9"))
EMA_SLOW = int(_opt("EMA_SLOW", "21"))
RSI_PERIOD = int(_opt("RSI_PERIOD", "14"))
RSI_MIN = float(_opt("RSI_MIN", "40"))
RSI_MAX = float(_opt("RSI_MAX", "70"))
ATR_PERIOD = int(_opt("ATR_PERIOD", "14"))
ATR_STOP_MULT = float(_opt("ATR_STOP_MULT", "1.5"))
ATR_TARGET_MULT = float(_opt("ATR_TARGET_MULT", "3.0"))
# Volume filter multiplier: candle volume must exceed (vol_avg20 * VOL_FILTER_MULT)
# 1.0 = strict (must be above average); 0.8 = allows 20% margin; 0.0 = disabled
VOL_FILTER_MULT = float(_opt("VOL_FILTER_MULT", "0.8"))
# Cross-detection lookback (closed candles). 1 = strict (cross must be on the
# latest closed bar). 3 = catches crosses up to 3 hours late, useful when
# scheduler reliability is patchy (e.g., GHA free-tier dropping cycles).
CROSS_LOOKBACK = int(_opt("CROSS_LOOKBACK", "3"))

# Operational
PAPER_MODE = _flag("PAPER_MODE", True)
EOD_FLATTEN_HOUR_IST = int(_opt("EOD_FLATTEN_HOUR_IST", "23"))
EOD_FLATTEN_MINUTE_IST = int(_opt("EOD_FLATTEN_MINUTE_IST", "55"))

# Token mint addresses on Solana mainnet (needed for Phase 4 live swaps via Jupiter)
TOKENS = {
    "SOL": "So11111111111111111111111111111111111111112",
    "JTO": "jtojtomepa8beP8AuQc6eXt5FriJwfFMwQx2v2f9mCL",
    "USDC": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
}

# Decimal places for each token (SPL token spec). Used for unit conversion.
DECIMALS = {"SOL": 9, "JTO": 9, "USDC": 6}

# Trading universe — bot scans these tokens each cycle, takes first valid entry.
# Comma-separated env var (e.g., "SOL,JTO,RAY"). Position is always vs USDC.
UNIVERSE = [s.strip() for s in _opt("UNIVERSE", "SOL,JTO").split(",") if s.strip()]

# File paths
STATE_FILE = DATA_DIR / "state.json"
TRADES_FILE = DATA_DIR / "trades.csv"
TAX_LOG_FILE = DATA_DIR / "tax_log.csv"
