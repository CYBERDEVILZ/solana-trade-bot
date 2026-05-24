"""Bot entry point — one cron cycle.

Called by the scheduler every hour. Each invocation:
    1. Loads state
    2. Computes current equity (in INR)
    3. Checks kill switches
    4. Fetches latest candles
    5. Asks strategy for a signal
    6. Maybe executes a swap (paper or live)
    7. Optionally EOD-flattens to USDC
    8. Logs everything to data/trades.csv + data/tax_log.csv
    9. Sends Telegram cycle summary
    10. Saves state
"""
from __future__ import annotations

import logging
import sys
import traceback
from datetime import datetime, timezone

from . import config, market, notify, risk, strategy, tax_log
from .executor import execute_swap
from .state import BotState, Position
from .strategy import Signal

LOG_FORMAT = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"


def setup_logging() -> None:
    log_file = config.LOGS_DIR / f"bot_{datetime.now(timezone.utc).strftime('%Y-%m-%d')}.log"
    handlers = [
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
    logging.basicConfig(level=logging.INFO, format=LOG_FORMAT, handlers=handlers)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass


log = logging.getLogger("bot.main")


def compute_equity_inr(state: BotState, sol_inr: float, usdc_inr: float,
                      sol_balance: float, usdc_balance: float) -> float:
    """Equity = SOL value + USDC value, both converted to INR."""
    return sol_balance * sol_inr + usdc_balance * usdc_inr


def get_balances() -> tuple[float, float]:
    """Return (sol, usdc) balances of the trading wallet (or paper-mode equivalents)."""
    if config.PAPER_MODE:
        # Paper mode: state.json carries the simulated balances.
        # On first run we seed with full capital in USDC.
        # This is read back by the caller via state.
        return 0.0, 0.0  # placeholder, caller uses paper_balances()
    if not config.TRADING_WALLET_PUBKEY:
        return 0.0, 0.0
    from . import wallet
    bal = wallet.get_balance(config.TRADING_WALLET_PUBKEY)
    return bal.sol, bal.usdc


def paper_balances(state: BotState, sol_price_usd: float, usdc_inr: float) -> tuple[float, float]:
    """Reconstruct (sol, usdc) for paper mode from state."""
    if state.position:
        sol = state.position.qty
    else:
        sol = 0.0
    # Paper-mode USDC = total starting capital minus what we spent on SOL,
    # plus realized P&L from completed trades.
    # Simpler: track paper_usdc_balance on state directly.
    return sol, state.paper_usdc


def run_cycle() -> int:
    """Run one strategy cycle. Returns exit code (0 ok, 1 error, 2 halted)."""
    setup_logging()
    log.info("=" * 60)
    log.info(f"Starting cycle. PAPER_MODE={config.PAPER_MODE}")

    state = BotState.load()
    state.cycle_count += 1
    state.last_cycle_iso = datetime.now(timezone.utc).isoformat()

    try:
        # 1. Get FX rate and spot prices for the full universe
        usd_inr = market.get_usd_inr_price()
        spot = {tok: market.get_spot_price_usd(tok) for tok in config.UNIVERSE}
        spot_str = "  ".join(f"{t}=${p:.4f}" for t, p in spot.items())
        log.info(f"USD/INR=Rs {usd_inr:.4f}  Spot: {spot_str}")

        # 2. Initialize paper balances on first cycle if needed
        if config.PAPER_MODE:
            if state.starting_equity_inr == 0 and state.paper_usdc == 0:
                state.starting_equity_inr = config.MAX_CAPITAL_INR
                state.peak_equity_inr = config.MAX_CAPITAL_INR
                state.daily_start_equity_inr = config.MAX_CAPITAL_INR
                state.paper_usdc = config.MAX_CAPITAL_INR / usd_inr
                log.info(f"Paper mode initialized with Rs {config.MAX_CAPITAL_INR} = {state.paper_usdc:.4f} USDC")
            usdc_bal = state.paper_usdc
            pos_qty = state.position.qty if state.position else 0.0
            pos_tok = state.position.token if state.position else None
        else:
            # Live mode: read on-chain balances (Phase 4+).
            from . import wallet
            usdc_bal = wallet.get_usdc_balance(config.TRADING_WALLET_PUBKEY) if config.TRADING_WALLET_PUBKEY else 0.0
            pos_qty = state.position.qty if state.position else 0.0
            pos_tok = state.position.token if state.position else None

        # Compute equity using the position's current spot price (if any)
        pos_value_inr = (pos_qty * spot[pos_tok] * usd_inr) if (pos_tok and pos_tok in spot) else 0.0
        equity_inr = usdc_bal * usd_inr + pos_value_inr
        if pos_tok:
            log.info(f"Balances: {pos_tok}={pos_qty:.6f} ({pos_value_inr:.2f} INR), USDC={usdc_bal:.4f} ({usdc_bal*usd_inr:.2f} INR), Equity=Rs {equity_inr:.2f}")
        else:
            log.info(f"Balances: FLAT, USDC={usdc_bal:.4f} ({usdc_bal*usd_inr:.2f} INR), Equity=Rs {equity_inr:.2f}")

        # 3. Check kill switches
        halt = risk.check_halts(state, equity_inr)
        if halt:
            state.halted = True
            state.halt_reason = halt
            state.save()
            log.warning(f"HALTED: {halt}")
            notify.notify_halt(halt, equity_inr)
            return 2

        # 4. Strategy — depends on whether we hold a position
        signal_value = "HOLD"
        signal_reason = ""
        if state.position:
            # In a position: only evaluate that token's data for exit conditions.
            df = market.get_candles(state.position.token, "1H", 200)
            snap = strategy.evaluate(df, position_open=True, entry_price=state.position.entry_price)
            signal_value, signal_reason = snap.signal.value, snap.reason
            log.info(f"[{state.position.token}] Signal: {signal_value} | Price=${snap.price:.4f} EMA9=${snap.ema_fast:.4f} EMA21=${snap.ema_slow:.4f} RSI={snap.rsi:.2f} ATR=${snap.atr:.4f}")
            log.info(f"Reason: {snap.reason}")

            if risk.is_eod_flatten_window():
                log.info("EOD flatten window — closing position to USDC")
                _do_exit(state, snap, "EOD_FLATTEN", spot[state.position.token], usd_inr)
            elif snap.signal in (Signal.LONG_EXIT, Signal.STOP_LOSS, Signal.TAKE_PROFIT):
                _do_exit(state, snap, snap.signal.value, spot[state.position.token], usd_inr)
            else:
                log.info("Holding position. No action.")
        else:
            # Flat: scan universe in order, take first valid entry.
            entered = False
            for tok in config.UNIVERSE:
                df = market.get_candles(tok, "1H", 200)
                snap = strategy.evaluate(df, position_open=False, entry_price=None)
                log.info(f"[{tok}] Signal: {snap.signal.value} | Price=${snap.price:.4f} EMA9=${snap.ema_fast:.4f} EMA21=${snap.ema_slow:.4f} RSI={snap.rsi:.2f} ATR=${snap.atr:.4f}")
                log.info(f"[{tok}] Reason: {snap.reason}")
                if snap.signal == Signal.LONG_ENTRY:
                    signal_value, signal_reason = f"LONG_ENTRY ({tok})", snap.reason
                    _do_entry(state, tok, snap, equity_inr, spot[tok], usd_inr)
                    entered = True
                    break
            if not entered:
                signal_value = "HOLD"
                signal_reason = f"No valid entries across {config.UNIVERSE}"
                log.info("No entry signal in any universe coin.")

        # 6. Day 7 milestone banner
        if not state.start_date_iso:
            state.start_date_iso = datetime.now(timezone.utc).isoformat()
        try:
            start_dt = datetime.fromisoformat(state.start_date_iso)
            days_elapsed = (datetime.now(timezone.utc) - start_dt).days
        except Exception:
            days_elapsed = 0

        # 7. Final summary + telegram
        pos_str = (f"{state.position.qty:.6f} {state.position.token} @ ${state.position.entry_price:.4f}"
                   if state.position else "FLAT (all USDC)")

        review_banner = ""
        if days_elapsed >= 7:
            review_banner = (
                f"\n\n🎯 *DAY {days_elapsed} — REVIEW TIME*\n"
                f"Open Claude Code in the bot directory and ask: \"review the bot\""
            )
        notify.notify_cycle(equity_inr, signal_value, signal_reason, pos_str + review_banner)

        state.save()
        log.info(f"Cycle complete. Equity=Rs {equity_inr:.2f}  Position={pos_str}")
        return 0

    except Exception as e:
        err = traceback.format_exc()
        log.error(f"Cycle failed: {err}")
        notify.notify_error("cycle", str(e))
        state.save()
        return 1


def _do_entry(state, token, snap, equity_inr, token_usd, usd_inr):
    """Open a long position in `token` (paid in USDC)."""
    sizing = risk.size_long_entry(equity_inr, token_usd, snap.atr, usd_inr)
    log.info(f"[{token}] Sizing: {sizing.reason}")
    if sizing.qty <= 0:
        log.warning("Sizing returned zero — skipping entry")
        return

    # Cap by available USDC (paper mode)
    usdc_available = state.paper_usdc if config.PAPER_MODE else None
    if usdc_available is not None and sizing.quote_qty > usdc_available:
        log.info(f"Capping by available USDC: {usdc_available:.4f}")
        sizing.quote_qty = usdc_available * 0.99
        sizing.qty = sizing.quote_qty / token_usd

    result = execute_swap("USDC", token, sizing.quote_qty)
    if not result.success:
        log.error(f"Entry swap failed: {result.error}")
        notify.notify_error("entry", str(result.error))
        return

    token_inr = token_usd * usd_inr
    state.position = Position(
        token=token, quote_token="USDC",
        qty=result.out_amount,
        entry_price=token_usd,
        entry_ts_iso=datetime.now(timezone.utc).isoformat(),
        entry_cost_inr=result.in_amount * usd_inr,
        stop_price=snap.stop_price,
        target_price=snap.target_price,
    )
    if config.PAPER_MODE:
        state.paper_usdc -= result.in_amount

    tax_log.log_swap(
        action=f"BUY_{token}", token_in="USDC", amount_in=result.in_amount,
        token_out=token, amount_out=result.out_amount,
        sol_inr_price=token_inr, usd_inr_price=usd_inr,
        fee_inr=result.fee_inr,
        cost_basis_inr=result.in_amount * usd_inr,
        realized_pnl_inr=0.0,
        tx_signature=result.tx_signature, route_summary=result.route_summary,
        paper_mode=config.PAPER_MODE,
    )
    notify.notify_trade("BUY", token, result.out_amount, token_usd,
                        result.out_amount * token_inr, config.PAPER_MODE)
    log.info(f"ENTERED: {result.out_amount:.6f} {token} @ ${token_usd:.4f}  stop=${snap.stop_price:.4f} target=${snap.target_price:.4f}")


def _do_exit(state, snap, action_name, token_usd, usd_inr):
    """Close the current position back to USDC."""
    if not state.position:
        return
    pos = state.position
    result = execute_swap(pos.token, "USDC", pos.qty)
    if not result.success:
        log.error(f"Exit swap failed: {result.error}")
        notify.notify_error("exit", str(result.error))
        return

    proceeds_inr = result.out_amount * usd_inr
    pnl_inr = proceeds_inr - pos.entry_cost_inr
    token_inr = token_usd * usd_inr

    if config.PAPER_MODE:
        state.paper_usdc += result.out_amount

    tax_log.log_swap(
        action=f"{action_name}_{pos.token}", token_in=pos.token, amount_in=result.in_amount,
        token_out="USDC", amount_out=result.out_amount,
        sol_inr_price=token_inr, usd_inr_price=usd_inr,
        fee_inr=result.fee_inr,
        cost_basis_inr=pos.entry_cost_inr,
        realized_pnl_inr=pnl_inr,
        tx_signature=result.tx_signature, route_summary=result.route_summary,
        paper_mode=config.PAPER_MODE,
    )
    notify.notify_trade(f"SELL ({action_name})", pos.token, result.in_amount, token_usd,
                        proceeds_inr, config.PAPER_MODE)
    log.info(f"EXITED: {result.in_amount:.6f} {pos.token} -> {result.out_amount:.4f} USDC  P&L Rs {pnl_inr:+.2f}")
    state.position = None


if __name__ == "__main__":
    sys.exit(run_cycle())
