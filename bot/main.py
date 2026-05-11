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
    return sol, getattr(state, "_paper_usdc", 0.0)


def run_cycle() -> int:
    """Run one strategy cycle. Returns exit code (0 ok, 1 error, 2 halted)."""
    setup_logging()
    log.info("=" * 60)
    log.info(f"Starting cycle. PAPER_MODE={config.PAPER_MODE}")

    state = BotState.load()
    state.cycle_count += 1
    state.last_cycle_iso = datetime.now(timezone.utc).isoformat()

    try:
        # 1. Get FX rates
        sol_inr = market.get_sol_inr_price()
        usd_inr = market.get_usd_inr_price()
        sol_usd = market.get_spot_price_usd("SOL")
        log.info(f"Prices: SOL/USD=${sol_usd:.4f}  SOL/INR=Rs {sol_inr:.2f}  USD/INR=Rs {usd_inr:.4f}")

        # 2. Get balances
        if config.PAPER_MODE:
            # Initialize paper balances on first cycle
            if state.cycle_count == 1 and state.starting_equity_inr == 0:
                state.starting_equity_inr = config.MAX_CAPITAL_INR
                state.peak_equity_inr = config.MAX_CAPITAL_INR
                state.daily_start_equity_inr = config.MAX_CAPITAL_INR
                # Seed paper wallet with starting capital entirely in USDC
                state.__dict__["_paper_usdc"] = config.MAX_CAPITAL_INR / usd_inr
                log.info(f"Paper mode initialized with Rs {config.MAX_CAPITAL_INR} = {state.__dict__['_paper_usdc']:.4f} USDC")
            sol_bal = state.position.qty if state.position else 0.0
            usdc_bal = state.__dict__.get("_paper_usdc", 0.0)
        else:
            sol_bal, usdc_bal = get_balances()

        equity_inr = sol_bal * sol_inr + usdc_bal * usd_inr
        log.info(f"Balances: SOL={sol_bal:.6f} ({sol_bal*sol_inr:.2f} INR), "
                 f"USDC={usdc_bal:.4f} ({usdc_bal*usd_inr:.2f} INR), "
                 f"Equity=Rs {equity_inr:.2f}")

        # 3. Check kill switches
        halt = risk.check_halts(state, equity_inr)
        if halt:
            state.halted = True
            state.halt_reason = halt
            state.save()
            log.warning(f"HALTED: {halt}")
            notify.notify_halt(halt, equity_inr)
            return 2

        # 4. Candles + signal
        df = market.get_candles("SOL", "1H", 200)
        snap = strategy.evaluate(
            df,
            position_open=state.position is not None,
            entry_price=state.position.entry_price if state.position else None,
        )
        log.info(f"Signal: {snap.signal.value} | Price=${snap.price:.4f} "
                 f"EMA9=${snap.ema_fast:.4f} EMA21=${snap.ema_slow:.4f} "
                 f"RSI={snap.rsi:.2f} ATR=${snap.atr:.4f}")
        log.info(f"Reason: {snap.reason}")

        # 5. EOD flatten override — if we have a position AND it's EOD window, exit
        if state.position and risk.is_eod_flatten_window():
            log.info("EOD flatten window — closing position to USDC")
            _do_exit(state, snap, "EOD_FLATTEN", sol_inr, usd_inr, sol_usd)
        elif snap.signal == Signal.LONG_ENTRY and not state.position:
            _do_entry(state, snap, equity_inr, sol_inr, usd_inr, sol_usd)
        elif snap.signal in (Signal.LONG_EXIT, Signal.STOP_LOSS, Signal.TAKE_PROFIT) and state.position:
            _do_exit(state, snap, snap.signal.value, sol_inr, usd_inr, sol_usd)
        else:
            log.info("No action this cycle.")

        # 6. Final summary + telegram
        pos_str = (f"{state.position.qty:.6f} SOL @ ${state.position.entry_price:.4f}"
                   if state.position else "FLAT (all USDC)")
        notify.notify_cycle(equity_inr, snap.signal.value, snap.reason, pos_str)

        state.save()
        log.info(f"Cycle complete. Equity=Rs {equity_inr:.2f}  Position={pos_str}")
        return 0

    except Exception as e:
        err = traceback.format_exc()
        log.error(f"Cycle failed: {err}")
        notify.notify_error("cycle", str(e))
        state.save()
        return 1


def _do_entry(state, snap, equity_inr, sol_inr, usd_inr, sol_usd):
    sizing = risk.size_long_entry(equity_inr, sol_usd, snap.atr, usd_inr)
    log.info(f"Sizing: {sizing.reason}")
    if sizing.qty <= 0:
        log.warning("Sizing returned zero — skipping entry")
        return

    # Cap by available USDC
    usdc_available = state.__dict__.get("_paper_usdc", 0.0) if config.PAPER_MODE else None
    if usdc_available is not None and sizing.quote_qty > usdc_available:
        log.info(f"Capping by available USDC: {usdc_available:.4f}")
        sizing.quote_qty = usdc_available * 0.99
        sizing.qty = sizing.quote_qty / sol_usd

    result = execute_swap("USDC", "SOL", sizing.quote_qty)
    if not result.success:
        log.error(f"Entry swap failed: {result.error}")
        notify.notify_error("entry", str(result.error))
        return

    state.position = Position(
        token="SOL", quote_token="USDC",
        qty=result.out_amount,
        entry_price=sol_usd,
        entry_ts_iso=datetime.now(timezone.utc).isoformat(),
        entry_cost_inr=result.in_amount * usd_inr,
        stop_price=snap.stop_price,
        target_price=snap.target_price,
    )
    if config.PAPER_MODE:
        state.__dict__["_paper_usdc"] -= result.in_amount

    tax_log.log_swap(
        action="BUY_SOL", token_in="USDC", amount_in=result.in_amount,
        token_out="SOL", amount_out=result.out_amount,
        sol_inr_price=sol_inr, usd_inr_price=usd_inr,
        fee_inr=result.fee_inr,
        cost_basis_inr=result.in_amount * usd_inr,
        realized_pnl_inr=0.0,
        tx_signature=result.tx_signature, route_summary=result.route_summary,
        paper_mode=config.PAPER_MODE,
    )
    notify.notify_trade("BUY", "SOL", result.out_amount, sol_usd,
                        result.out_amount * sol_inr, config.PAPER_MODE)
    log.info(f"ENTERED: {result.out_amount:.6f} SOL @ ${sol_usd:.4f}")


def _do_exit(state, snap, action_name, sol_inr, usd_inr, sol_usd):
    if not state.position:
        return
    pos = state.position
    result = execute_swap("SOL", "USDC", pos.qty)
    if not result.success:
        log.error(f"Exit swap failed: {result.error}")
        notify.notify_error("exit", str(result.error))
        return

    proceeds_inr = result.out_amount * usd_inr
    pnl_inr = proceeds_inr - pos.entry_cost_inr

    if config.PAPER_MODE:
        state.__dict__["_paper_usdc"] = state.__dict__.get("_paper_usdc", 0.0) + result.out_amount

    tax_log.log_swap(
        action=action_name, token_in="SOL", amount_in=result.in_amount,
        token_out="USDC", amount_out=result.out_amount,
        sol_inr_price=sol_inr, usd_inr_price=usd_inr,
        fee_inr=result.fee_inr,
        cost_basis_inr=pos.entry_cost_inr,
        realized_pnl_inr=pnl_inr,
        tx_signature=result.tx_signature, route_summary=result.route_summary,
        paper_mode=config.PAPER_MODE,
    )
    notify.notify_trade(f"SELL ({action_name})", "SOL", result.in_amount, sol_usd,
                        proceeds_inr, config.PAPER_MODE)
    log.info(f"EXITED: {result.in_amount:.6f} SOL -> {result.out_amount:.4f} USDC "
             f"P&L Rs {pnl_inr:+.2f}")
    state.position = None


if __name__ == "__main__":
    sys.exit(run_cycle())
