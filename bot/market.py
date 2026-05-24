"""Market data: prices, quotes, historical candles.

Sources:
- Coinbase Exchange API for SOL-USD spot price + hourly candles.
  (Binance was the original source, but it returns HTTP 451 from US IPs
  including all GitHub Actions runners, due to its 2023 SEC settlement.
  Coinbase has no such restriction.)
- Frankfurter (ECB) for USD/INR conversion — free, no key, generous limits.
- Synthetic quote derived from Coinbase spot for paper-mode swaps.
  Live (Phase 4) swaps will use Jupiter directly on Solana.
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

COINBASE_BASE = "https://api.exchange.coinbase.com"
# Jupiter URLs kept here for Phase 4 live execution; not used in paper mode.
JUPITER_PRICE_URL = "https://lite-api.jup.ag/price/v3"
JUPITER_QUOTE_URL = "https://lite-api.jup.ag/swap/v1/quote"
# Frankfurter — ECB-sourced forex rates, free, no API key, generous limits.
FRANKFURTER_URL = "https://api.frankfurter.app/latest"
FX_CACHE_FILE = config.DATA_DIR / "fx_cache.json"

# Map our token symbols to Coinbase product IDs.
COINBASE_PRODUCTS = {
    "SOL": "SOL-USD",
    "JTO": "JTO-USD",
}

# Map our interval names to Coinbase granularity (seconds).
# Coinbase supports: 60, 300, 900, 3600, 21600, 86400.
COINBASE_GRANULARITY = {
    "1m": 60, "5m": 300, "15m": 900,
    "1H": 3600, "6H": 21600, "1D": 86400,
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
    """Get USD spot price for a token via Coinbase Exchange ticker.

    Coinbase SOL-USD tracks on-chain SOL/USDC within a few basis points and
    is reachable from any IP (no geo-block).
    """
    if token_symbol not in COINBASE_PRODUCTS:
        raise ValueError(f"No Coinbase mapping for {token_symbol}")
    product = COINBASE_PRODUCTS[token_symbol]
    r = requests.get(
        f"{COINBASE_BASE}/products/{product}/ticker",
        timeout=TIMEOUT,
        headers={"User-Agent": "solana-trade-bot/1.0"},
    )
    r.raise_for_status()
    return float(r.json()["price"])


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

    Computed as SOL/USD (Binance) * USD/INR (Frankfurter ECB rate).
    """
    return get_spot_price_usd("SOL") * _get_usd_inr_rate()


def get_quote(
    in_token: str,
    out_token: str,
    in_amount_ui: float,
    slippage_bps: int = 50,
) -> Quote:
    """Get a swap quote.

    In paper mode (and from environments where Jupiter is unreachable), this
    synthesizes a quote from Binance spot price. The result is good enough for
    paper-trade simulation — real on-chain swaps in Phase 4 will need the live
    Jupiter path which we'll wire then.
    """
    if config.PAPER_MODE:
        return _synthetic_quote(in_token, out_token, in_amount_ui, slippage_bps)
    return _live_jupiter_quote(in_token, out_token, in_amount_ui, slippage_bps)


def _synthetic_quote(in_token, out_token, in_amount_ui, slippage_bps) -> Quote:
    """Build a paper-mode quote from Coinbase spot price.

    Supports any token<->USDC pair where the token exists in COINBASE_PRODUCTS.
    """
    # All prices are denominated in USDC (~=USD).
    if out_token == "USDC" and in_token in COINBASE_PRODUCTS:
        price = get_spot_price_usd(in_token)              # USDC per token
    elif in_token == "USDC" and out_token in COINBASE_PRODUCTS:
        price = 1.0 / get_spot_price_usd(out_token)       # token per USDC
    else:
        raise ValueError(f"Unsupported pair: {in_token} -> {out_token}")

    in_decimals = config.DECIMALS.get(in_token, 9)
    out_decimals = config.DECIMALS.get(out_token, 9)

    in_amount_raw = int(round(in_amount_ui * (10 ** in_decimals)))
    out_amount_ui = in_amount_ui * price
    out_amount_raw = int(round(out_amount_ui * (10 ** out_decimals)))

    return Quote(
        in_token=in_token,
        out_token=out_token,
        in_amount_raw=in_amount_raw,
        out_amount_raw=out_amount_raw,
        price=price,
        price_impact_pct=0.0,
        route_plan=[],
        raw={"synthetic": True, "source": "coinbase"},
    )


def _live_jupiter_quote(in_token, out_token, in_amount_ui, slippage_bps) -> Quote:
    """Real Jupiter quote — used only in live mode (Phase 4)."""
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
    closed_only: bool = True,
) -> pd.DataFrame:
    """Fetch OHLCV candles from Coinbase Exchange.

    Coinbase is used as the *price oracle only* — actual swaps still execute
    on Solana via Jupiter. Coinbase SOL-USD has deep liquidity and reliable
    OHLCV history that tracks on-chain SOL/USDC within a few basis points.

    interval: "1m", "5m", "15m", "1H", "6H", "1D"
    closed_only: if True (default), drop the most recent candle if it is
        still forming. Critical for strategy stability — indicators on a
        live-forming candle produce spurious cross signals.
    Returns DataFrame: time, open, high, low, close, volume (sorted ascending).
    """
    if token_symbol not in COINBASE_PRODUCTS:
        raise ValueError(f"No Coinbase mapping for {token_symbol}")
    if interval not in COINBASE_GRANULARITY:
        raise ValueError(f"Unsupported interval {interval}")

    product = COINBASE_PRODUCTS[token_symbol]
    granularity = COINBASE_GRANULARITY[interval]
    # Coinbase returns up to 300 rows per request. Request 'limit + buffer'.
    fetch_n = min(300, max(limit + 5, 50))
    end_unix = int(time.time())
    start_unix = end_unix - fetch_n * granularity

    r = requests.get(
        f"{COINBASE_BASE}/products/{product}/candles",
        params={
            "granularity": granularity,
            "start": pd.to_datetime(start_unix, unit="s", utc=True).isoformat(),
            "end": pd.to_datetime(end_unix, unit="s", utc=True).isoformat(),
        },
        timeout=TIMEOUT,
        headers={"User-Agent": "solana-trade-bot/1.0"},
    )
    r.raise_for_status()
    rows = r.json()

    if not rows:
        return pd.DataFrame(columns=["time", "open", "high", "low", "close", "volume"])

    # Coinbase row: [time, low, high, open, close, volume] — descending by time.
    df = pd.DataFrame(rows, columns=["time_unix", "low", "high", "open", "close", "volume"])
    df["time"] = pd.to_datetime(df["time_unix"], unit="s", utc=True)
    df["close_time"] = df["time"] + pd.Timedelta(seconds=granularity)
    for col in ("open", "high", "low", "close", "volume"):
        df[col] = df[col].astype(float)
    df = df.sort_values("time").reset_index(drop=True)

    if closed_only:
        now_utc = pd.Timestamp.now(tz="UTC")
        df = df[df["close_time"] <= now_utc].copy()

    # Trim to the requested limit (keep most recent rows)
    if len(df) > limit:
        df = df.tail(limit).reset_index(drop=True)

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
