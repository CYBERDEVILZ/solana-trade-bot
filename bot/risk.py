"""Risk management: position sizing, kill switches, EOD flatten check."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from typing import Optional

from . import config
from .state import BotState

log = logging.getLogger("bot.risk")

IST_OFFSET = timedelta(hours=5, minutes=30)


def now_ist() -> datetime:
    return datetime.now(timezone.utc) + IST_OFFSET


def today_ist_str() -> str:
    return now_ist().strftime("%Y-%m-%d")


def is_eod_flatten_window() -> bool:
    """True if we're within the EOD flatten window (after EOD_FLATTEN_HOUR:MINUTE IST)."""
    n = now_ist()
    eod_minutes = config.EOD_FLATTEN_HOUR_IST * 60 + config.EOD_FLATTEN_MINUTE_IST
    cur_minutes = n.hour * 60 + n.minute
    return cur_minutes >= eod_minutes


@dataclass
class SizingDecision:
    notional_inr: float       # how much INR worth to trade
    qty: float                # amount of base token to buy
    quote_qty: float          # amount of quote (USDC) to spend
    reason: str


def size_long_entry(
    equity_inr: float,
    price_usd: float,
    atr_usd: float,
    usd_inr: float,
) -> SizingDecision:
    """Size a long entry based on % risk and ATR stop distance.

    risk_inr = equity * RISK_PER_TRADE_PCT / 100
    stop_distance_usd = ATR_STOP_MULT * atr_usd
    qty = risk_inr / (stop_distance_usd * usd_inr)
    """
    risk_inr = equity_inr * config.RISK_PER_TRADE_PCT / 100.0
    stop_dist_usd = config.ATR_STOP_MULT * atr_usd

    if stop_dist_usd <= 0:
        return SizingDecision(0, 0, 0, "ATR is zero — cannot size")

    qty = risk_inr / (stop_dist_usd * usd_inr)
    notional_inr = qty * price_usd * usd_inr
    quote_qty = qty * price_usd       # USDC needed

    # Cap at min(equity, 80% of equity per position) — on tiny capital
    # we don't fragment into multiple positions, so a single position
    # can use most of equity. But never go above equity itself.
    max_notional = min(equity_inr * 0.95, config.MAX_CAPITAL_INR * 0.95)
    if notional_inr > max_notional:
        scale = max_notional / notional_inr
        qty *= scale
        notional_inr *= scale
        quote_qty *= scale
        reason = (f"Capped at 95% of equity: risk_inr={risk_inr:.2f}, "
                  f"stop_dist=${stop_dist_usd:.4f}, qty={qty:.6f}")
    else:
        reason = (f"Risk-based: risk_inr={risk_inr:.2f}, "
                  f"stop_dist=${stop_dist_usd:.4f}, qty={qty:.6f}")

    return SizingDecision(notional_inr, qty, quote_qty, reason)


def check_halts(state: BotState, current_equity_inr: float) -> Optional[str]:
    """Return halt reason if any kill-switch fires, else None.

    Side effects:
    - Updates peak_equity_inr (high-water mark)
    - Rolls daily_date + daily_start_equity_inr on new IST day, and
      AUTO-CLEARS a daily_loss halt at the same time (so the bot can trade
      again the next day). Max-drawdown halts are NOT auto-cleared — those
      need manual intervention.
    """
    today = today_ist_str()
    new_day = state.daily_date != today

    # Auto-clear daily-loss halts on a new IST day.
    if state.halted and new_day and state.halt_reason.startswith("Daily loss"):
        log.info(f"Auto-clearing daily halt on new day rollover ({state.daily_date} -> {today})")
        state.halted = False
        state.halt_reason = ""

    if state.halted:
        return state.halt_reason

    # Update peak equity high-water mark
    if current_equity_inr > state.peak_equity_inr:
        state.peak_equity_inr = current_equity_inr

    # Drawdown halt (long-term, NOT auto-cleared)
    if state.peak_equity_inr > 0:
        dd_pct = (state.peak_equity_inr - current_equity_inr) / state.peak_equity_inr * 100
        if dd_pct >= config.MAX_DRAWDOWN_PCT:
            return f"Max drawdown hit: {dd_pct:.2f}% >= {config.MAX_DRAWDOWN_PCT}%"

    # Daily-loss halt
    if new_day:
        state.daily_date = today
        state.daily_start_equity_inr = current_equity_inr
    elif state.daily_start_equity_inr > 0:
        daily_loss_pct = (state.daily_start_equity_inr - current_equity_inr) \
                         / state.daily_start_equity_inr * 100
        if daily_loss_pct >= config.DAILY_LOSS_HALT_PCT:
            return (f"Daily loss halt: {daily_loss_pct:.2f}% "
                    f">= {config.DAILY_LOSS_HALT_PCT}%")

    return None
