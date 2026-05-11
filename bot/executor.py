"""Swap executor.

PAPER_MODE=true: simulates fills at current market price + slippage assumption.
PAPER_MODE=false: real on-chain swap via Jupiter (Phase 4+).

The live signing path is stubbed for Phase 0-3 — it's safe to call execute_swap()
with PAPER_MODE=true throughout paper trading. The live path is enabled only
when TRADING_WALLET_PRIVKEY is set AND PAPER_MODE=false.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from . import config, market

log = logging.getLogger(__name__)

# Conservative slippage assumption for paper fills (10 bps = 0.1%)
PAPER_SLIPPAGE_PCT = 0.10


@dataclass
class SwapResult:
    success: bool
    in_token: str
    out_token: str
    in_amount: float          # UI units
    out_amount: float         # UI units
    price: float              # out per in
    fee_inr: float            # estimated network fee in INR
    tx_signature: str         # "" in paper mode
    route_summary: str
    paper: bool
    error: Optional[str] = None


def _route_summary(quote) -> str:
    """One-line summary of the Jupiter route, e.g. 'SOL->USDC via Orca'."""
    if not quote.route_plan:
        return f"{quote.in_token}->{quote.out_token} (direct)"
    hops = []
    for step in quote.route_plan:
        info = step.get("swapInfo", {})
        label = info.get("label", "?")
        hops.append(label)
    return f"{quote.in_token}->{quote.out_token} via {' > '.join(hops)}"


def execute_swap(
    in_token: str,
    out_token: str,
    in_amount_ui: float,
    slippage_bps: int = 50,
) -> SwapResult:
    """Execute a swap. Paper or live based on PAPER_MODE flag."""
    if in_amount_ui <= 0:
        return SwapResult(False, in_token, out_token, 0, 0, 0, 0, "", "", config.PAPER_MODE,
                          error="in_amount_ui must be positive")

    try:
        quote = market.get_quote(in_token, out_token, in_amount_ui, slippage_bps)
    except Exception as e:
        log.error(f"Quote failed: {e}")
        return SwapResult(False, in_token, out_token, in_amount_ui, 0, 0, 0, "", "",
                          config.PAPER_MODE, error=str(e))

    if config.PAPER_MODE:
        return _paper_fill(quote)
    return _live_swap(quote)


def _paper_fill(quote) -> SwapResult:
    """Simulate a fill — apply slippage haircut, no signing."""
    out_decimals = 9 if quote.out_token == "SOL" else 6
    in_decimals = 9 if quote.in_token == "SOL" else 6

    out_ui = quote.out_amount_raw / (10 ** out_decimals)
    in_ui = quote.in_amount_raw / (10 ** in_decimals)
    # Apply paper slippage haircut to be conservative
    out_ui_after_slippage = out_ui * (1 - PAPER_SLIPPAGE_PCT / 100)

    # Solana priority + base fee is tiny — about $0.0005 per tx
    fee_inr = 0.05

    return SwapResult(
        success=True,
        in_token=quote.in_token,
        out_token=quote.out_token,
        in_amount=in_ui,
        out_amount=out_ui_after_slippage,
        price=out_ui_after_slippage / in_ui,
        fee_inr=fee_inr,
        tx_signature="",
        route_summary=_route_summary(quote) + " [PAPER]",
        paper=True,
    )


def _live_swap(quote) -> SwapResult:
    """Real on-chain swap via Jupiter. Implemented in Phase 4."""
    return SwapResult(
        success=False,
        in_token=quote.in_token,
        out_token=quote.out_token,
        in_amount=0, out_amount=0, price=0, fee_inr=0,
        tx_signature="", route_summary="",
        paper=False,
        error="Live execution not yet wired (Phase 4 — needs trading wallet private key)",
    )
