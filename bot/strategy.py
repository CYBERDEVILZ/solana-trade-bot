"""Swing trading strategy: EMA cross entry + ATR-only exits.

Designed for low-frequency trades to fit Indian tax regime (1% TDS per sell
makes scalping unprofitable). Target 2-4 trades/week on hourly candles.

Signals:
    LONG_ENTRY:   9-EMA crosses above 21-EMA AND 40 <= RSI(14) <= 70 AND volume > 20-bar avg
    STOP_LOSS:    Price <= entry - 1.5 * ATR(14)        -> realize loss
    TAKE_PROFIT:  Price >= entry + 3.0 * ATR(14)        -> realize win
    EOD_FLATTEN:  Cycle in/after 22:30 IST window       -> exit no matter what (in main.py)

Rationale (from May-2026 backtest on 21 days of real SOL data):
    The earlier variant also used EMA cross-down as an exit. That caused 3 of 6
    winning setups to bail at small profit instead of running to the +3xATR
    target, dragging total return from +6.25% down to +1.85% on the same trades.
    With ATR-only exits, winners are allowed to fully play out — the 2:1 RR
    math actually works.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import Enum
from typing import Optional

import numpy as np
import pandas as pd

from . import config

log = logging.getLogger(__name__)


class Signal(str, Enum):
    HOLD = "HOLD"
    LONG_ENTRY = "LONG_ENTRY"
    LONG_EXIT = "LONG_EXIT"
    STOP_LOSS = "STOP_LOSS"
    TAKE_PROFIT = "TAKE_PROFIT"


@dataclass
class StrategySnapshot:
    signal: Signal
    price: float
    ema_fast: float
    ema_slow: float
    rsi: float
    atr: float
    stop_price: float       # absolute price for stop-loss
    target_price: float     # absolute price for take-profit
    reason: str             # human-readable explanation


def _ema(series: pd.Series, period: int) -> pd.Series:
    return series.ewm(span=period, adjust=False).mean()


def _rsi(close: pd.Series, period: int) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def _atr(df: pd.DataFrame, period: int) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1)
    return tr.ewm(alpha=1 / period, adjust=False).mean()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add ema_fast, ema_slow, rsi, atr columns. Returns same df with extras."""
    out = df.copy()
    out["ema_fast"] = _ema(out["close"], config.EMA_FAST)
    out["ema_slow"] = _ema(out["close"], config.EMA_SLOW)
    out["rsi"] = _rsi(out["close"], config.RSI_PERIOD)
    out["atr"] = _atr(out, config.ATR_PERIOD)
    out["vol_avg20"] = out["volume"].rolling(20).mean()
    return out


def evaluate(
    df: pd.DataFrame,
    position_open: bool,
    entry_price: Optional[float] = None,
) -> StrategySnapshot:
    """Decide what to do given the latest candles and current position state.

    df must have at least max(EMA_SLOW, RSI_PERIOD, ATR_PERIOD, 20) + 2 rows
    of OHLCV data, with the most recent (just-closed) candle as the last row.
    """
    min_rows = max(config.EMA_SLOW, config.RSI_PERIOD, config.ATR_PERIOD, 20) + 2
    if len(df) < min_rows:
        return StrategySnapshot(
            signal=Signal.HOLD, price=float(df["close"].iloc[-1]),
            ema_fast=0, ema_slow=0, rsi=0, atr=0,
            stop_price=0, target_price=0,
            reason=f"Insufficient data: {len(df)}/{min_rows} candles",
        )

    ind = compute_indicators(df)
    last = ind.iloc[-1]
    prev = ind.iloc[-2]

    price = float(last["close"])
    ef, es = float(last["ema_fast"]), float(last["ema_slow"])
    ef_p, es_p = float(prev["ema_fast"]), float(prev["ema_slow"])
    rsi = float(last["rsi"])
    atr = float(last["atr"])
    vol = float(last["volume"])
    vol_avg = float(last["vol_avg20"])

    stop_price = (entry_price or price) - config.ATR_STOP_MULT * atr
    target_price = (entry_price or price) + config.ATR_TARGET_MULT * atr

    cross_up = (ef_p <= es_p) and (ef > es)
    cross_down = (ef_p >= es_p) and (ef < es)

    if position_open:
        if entry_price is not None and price <= entry_price - config.ATR_STOP_MULT * atr:
            return StrategySnapshot(
                Signal.STOP_LOSS, price, ef, es, rsi, atr,
                stop_price, target_price,
                f"Stop hit: price {price:.4f} <= entry {entry_price:.4f} - {config.ATR_STOP_MULT}*ATR",
            )
        if entry_price is not None and price >= entry_price + config.ATR_TARGET_MULT * atr:
            return StrategySnapshot(
                Signal.TAKE_PROFIT, price, ef, es, rsi, atr,
                stop_price, target_price,
                f"Target hit: price {price:.4f} >= entry {entry_price:.4f} + {config.ATR_TARGET_MULT}*ATR",
            )
        # NOTE: previously had an EMA-cross-down exit here. Removed because the
        # 21-day backtest showed it cut winners short, dropping total return
        # from +6.25% to +1.85% on the same trades. Now position rides to
        # either ATR stop or ATR target (or EOD-flatten in main.py).
        cross_warn = " | warning: fast EMA crossed below slow, trend weakening" if cross_down else ""
        return StrategySnapshot(
            Signal.HOLD, price, ef, es, rsi, atr, stop_price, target_price,
            f"Holding long. Distance to stop: {((price - stop_price) / price * 100):.2f}%"
            f", to target: {((target_price - price) / price * 100):.2f}%{cross_warn}",
        )

    # No position open — check for entry
    vol_threshold = vol_avg * config.VOL_FILTER_MULT
    if cross_up and config.RSI_MIN <= rsi <= config.RSI_MAX and vol > vol_threshold:
        return StrategySnapshot(
            Signal.LONG_ENTRY, price, ef, es, rsi, atr, stop_price, target_price,
            f"Entry: EMA cross up ({ef:.4f} > {es:.4f}), RSI {rsi:.1f}, vol {vol:.0f} > {vol_threshold:.0f} ({config.VOL_FILTER_MULT}x avg)",
        )

    blockers = []
    if not cross_up:
        blockers.append(f"no EMA cross (fast={ef:.4f}, slow={es:.4f})")
    if not (config.RSI_MIN <= rsi <= config.RSI_MAX):
        blockers.append(f"RSI {rsi:.1f} outside [{config.RSI_MIN}, {config.RSI_MAX}]")
    if vol <= vol_threshold:
        blockers.append(f"low volume {vol:.0f} <= {vol_threshold:.0f} ({config.VOL_FILTER_MULT}x avg)")
    return StrategySnapshot(
        Signal.HOLD, price, ef, es, rsi, atr, stop_price, target_price,
        "Flat. Waiting: " + "; ".join(blockers),
    )


if __name__ == "__main__":
    import sys
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    logging.basicConfig(level=logging.INFO)

    from . import market

    df = market.get_candles("SOL", "1H", 200)
    print(f"Loaded {len(df)} candles")
    snap = evaluate(df, position_open=False)
    print(f"\nSignal: {snap.signal.value}")
    print(f"Price:  {snap.price:.4f}")
    print(f"EMA9:   {snap.ema_fast:.4f}")
    print(f"EMA21:  {snap.ema_slow:.4f}")
    print(f"RSI14:  {snap.rsi:.2f}")
    print(f"ATR14:  {snap.atr:.4f}")
    print(f"Stop:   {snap.stop_price:.4f}")
    print(f"Target: {snap.target_price:.4f}")
    print(f"Reason: {snap.reason}")
