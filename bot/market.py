"""Market data: prices, quotes, historical candles.

Sources:
- Jupiter price API (lite-api.jup.ag) for spot prices and swap quotes
- Birdeye public OHLCV for historical candles (no API key needed for basic use)
- CoinGecko for SOL/INR conversion (free tier, no key)
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Optional

import pandas as pd
import requests

from . import config

log = logging.getLogger(__name__)

JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3"
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
# Binance public klines — free, no key, very reliable. Used only for OHLCV history.
# Execution still happens on-chain via Jupiter; Binance is the price oracle.
BINANCE_KLINES_URL = "https://api.binance.com/api/v3/klines"
# Frankfurter — ECB-sourced forex rates, free, no API key, generous limits.
FRANKFURTER_URL = "https://api.frankfurter.app/latest"
FX_CACHE_FILE = config.DATA_DIR / "fx_cache.json"

# Map our token symbols to Binance trading pairs (vs USDT, which tracks USDC closely).
BINANCE_SYMBOLS = {
    "SOL": "SOLUSDT",
}

# Map our interval names to Binance interval codes.
BINANCE_INTERVALS = {
    "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1H": "1h", "4H": "4h", "1D": "1d", "1W": "1w",
}

# Default request timeout
TIMEOUT = 10


@dataclass
class Quote:
    in_token: str
    out_token: str
    in_amount_raw: int       # smallest units
    out_amount_raw: int      # smallest units
    price: float             # out per in (UI units)
    price_impact_pct: float
    route_plan: list
    raw: dict                # full Jupiter response (needed for swap tx)


def get_spot_price_usd(token_symbol: str) -> float:
    """Get USD spot price for a token via Jupiter price API."""
    mint = config.TOKENS[token_symbol]
    r = requests.get(JUPITER_PRICE_URL, params={"ids": mint}, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()
    return float(data[mint]["usdPrice"])


# Forex rate is slow-moving — cache on disk for 1 hour across cron cycles.
_FX_CACHE_TTL = 3600.0


def _get_usd_inr_rate() -> float:
    """USD/INR forex rate via Frankfurter (ECB), cached to disk for 1 hour."""
    now = time.time()
    if FX_CACHE_FILE.exists():
        try:
            cached = json.loads(FX_CACHE_FILE.read_text())
            if now - cached["ts"] < _FX_CACHE_TTL:
                return float(cached["usd_inr"])
        except Exception:
            pass

    r = requests.get(FRANKFURTER_URL, params={"from": "USD", "to": "INR"}, timeout=TIMEOUT)
    r.raise_for_status()
    rate = float(r.json()["rates"]["INR"])
    FX_CACHE_FILE.write_text(json.dumps({"ts": now, "usd_inr": rate}))
    return rate


def get_usd_inr_price() -> float:
    """USD/INR forex rate (USDC ~= USD, so this is also USDC/INR)."""
    return _get_usd_inr_rate()


def get_sol_inr_price() -> float:
    """SOL price in INR — needed for tax cost basis.

    Computed as SOL/USD (Jupiter) * USD/INR (Frankfurter ECB rate).
    """
    return get_spot_price_usd("SOL") * _get_usd_inr_rate()


def get_quote(
    in_token: str,
    out_token: str,
    in_amount_ui: float,
    slippage_bps: int = 50,
) -> Quote:
    """Get a Jupiter swap quote.

    in_amount_ui is in UI units (e.g. 0.1 SOL, not lamports).
    slippage_bps: 50 = 0.5%.
    """
    in_mint = config.TOKENS[in_token]
    out_mint = config.TOKENS[out_token]

    decimals = 9 if in_token == "SOL" else 6
    in_amount_raw = int(round(in_amount_ui * (10 ** decimals)))

    params = {
        "inputMint": in_mint,
        "outputMint": out_mint,
        "amount": in_amount_raw,
        "slippageBps": slippage_bps,
    }
    r = requests.get(JUPITER_QUOTE_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    data = r.json()

    out_decimals = 9 if out_token == "SOL" else 6
    out_amount_raw = int(data["outAmount"])
    out_amount_ui = out_amount_raw / (10 ** out_decimals)

    return Quote(
        in_token=in_token,
        out_token=out_token,
        in_amount_raw=in_amount_raw,
        out_amount_raw=out_amount_raw,
        price=out_amount_ui / in_amount_ui,
        price_impact_pct=float(data.get("priceImpactPct", 0) or 0) * 100,
        route_plan=data.get("routePlan", []),
        raw=data,
    )


def get_candles(
    token_symbol: str,
    interval: str = "1H",
    limit: int = 200,
) -> pd.DataFrame:
    """Fetch OHLCV candles from Binance public API.

    Binance is used as a *price oracle only* — actual swaps still execute
    on Solana via Jupiter. SOL/USDT on Binance tracks on-chain SOL/USDC
    within a few basis points and is the most reliable free OHLCV source.

    interval: "1m", "5m", "15m", "30m", "1H", "4H", "1D", "1W"
    Returns DataFrame: time, open, high, low, close, volume.
    """
    if token_symbol not in BINANCE_SYMBOLS:
        raise ValueError(f"No Binance mapping for {token_symbol}")
    if interval not in BINANCE_INTERVALS:
        raise ValueError(f"Unsupported interval {interval}")

    params = {
        "symbol": BINANCE_SYMBOLS[token_symbol],
        "interval": BINANCE_INTERVALS[interval],
        "limit": limit,
    }
    r = requests.get(BINANCE_KLINES_URL, params=params, timeout=TIMEOUT)
    r.raise_for_status()
    rows = r.json()

    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    df = pd.DataFrame(rows, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_vol", "num_trades",
        "taker_buy_base", "taker_buy_quote", "_ignore",
    ])
    df["time"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    return df[["time", "open", "high", "low", "close", "volume"]].reset_index(drop=True)


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO)

    print("=== Spot prices ===")
    sol_usd = get_spot_price_usd("SOL")
    sol_inr = get_sol_inr_price()
    usd_inr = get_usd_inr_price()
    print(f"SOL/USD: ${sol_usd:.2f}")
    print(f"SOL/INR: Rs {sol_inr:.2f}")
    print(f"USD/INR (via USDC): Rs {usd_inr:.2f}")

    print("\n=== Sample quote: 0.01 SOL -> USDC ===")
    q = get_quote("SOL", "USDC", 0.01)
    print(f"In:  {q.in_amount_raw / 1e9:.6f} SOL")
    print(f"Out: {q.out_amount_raw / 1e6:.4f} USDC")
    print(f"Price: {q.price:.4f} USDC/SOL")
    print(f"Price impact: {q.price_impact_pct:.4f}%")

    print("\n=== Last 5 hourly candles for SOL ===")
    df = get_candles("SOL", "1H", 5)
    print(df.to_string(index=False))
