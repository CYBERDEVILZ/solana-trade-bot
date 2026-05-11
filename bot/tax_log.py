"""Tax-compliant trade ledger.

Every swap is logged in tax_log.csv with INR cost basis at time of trade.
Hand this file to your CA at year-end for Section 115BBH filing.

Columns:
    timestamp_ist, action, token_in, amount_in, token_out, amount_out,
    sol_inr_price, usd_inr_price, gross_inr_value, fee_inr,
    cost_basis_inr, realized_pnl_inr, tx_signature, route_summary, paper_mode
"""
from __future__ import annotations

import csv
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from . import config

log = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

HEADERS = [
    "timestamp_ist", "action", "token_in", "amount_in",
    "token_out", "amount_out", "sol_inr_price", "usd_inr_price",
    "gross_inr_value", "fee_inr", "cost_basis_inr", "realized_pnl_inr",
    "tx_signature", "route_summary", "paper_mode",
]


def _ensure_header() -> None:
    if not config.TAX_LOG_FILE.exists():
        with config.TAX_LOG_FILE.open("w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(HEADERS)


def log_swap(
    action: str,                      # "BUY_SOL" | "SELL_SOL" | "EOD_FLATTEN"
    token_in: str,
    amount_in: float,
    token_out: str,
    amount_out: float,
    sol_inr_price: float,
    usd_inr_price: float,
    fee_inr: float = 0.0,
    cost_basis_inr: float = 0.0,
    realized_pnl_inr: float = 0.0,
    tx_signature: str = "",
    route_summary: str = "",
    paper_mode: bool = True,
) -> None:
    """Append one swap row to tax_log.csv."""
    _ensure_header()

    # Compute gross INR value of the swap (use input side)
    if token_in == "SOL":
        gross_inr = amount_in * sol_inr_price
    elif token_in == "USDC":
        gross_inr = amount_in * usd_inr_price
    else:
        gross_inr = 0.0

    ts = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")

    row = [
        ts, action, token_in, f"{amount_in:.8f}",
        token_out, f"{amount_out:.8f}",
        f"{sol_inr_price:.4f}", f"{usd_inr_price:.4f}",
        f"{gross_inr:.2f}", f"{fee_inr:.2f}",
        f"{cost_basis_inr:.2f}", f"{realized_pnl_inr:.2f}",
        tx_signature, route_summary, str(paper_mode).lower(),
    ]
    with config.TAX_LOG_FILE.open("a", newline="", encoding="utf-8") as f:
        csv.writer(f).writerow(row)
    log.info(f"tax_log: {action} {amount_in} {token_in} -> {amount_out} {token_out} "
             f"(Rs {gross_inr:.2f}, pnl Rs {realized_pnl_inr:.2f})")
