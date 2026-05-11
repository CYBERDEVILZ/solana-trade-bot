"""Persistent state: positions, equity, halts, daily P&L.

Read/written every cron cycle. Lives in data/state.json.
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Optional

from . import config

log = logging.getLogger(__name__)


@dataclass
class Position:
    token: str                  # e.g. "SOL"
    quote_token: str            # e.g. "USDC"
    qty: float                  # amount of token held
    entry_price: float          # quote per token at entry
    entry_ts_iso: str           # ISO timestamp
    entry_cost_inr: float       # total cost in INR at entry (for tax)
    stop_price: float
    target_price: float


@dataclass
class BotState:
    starting_equity_inr: float = 0.0
    peak_equity_inr: float = 0.0          # high-water mark for drawdown
    daily_start_equity_inr: float = 0.0   # for daily-loss halt
    daily_date: str = ""                  # YYYY-MM-DD IST
    position: Optional[Position] = None
    halted: bool = False
    halt_reason: str = ""
    last_cycle_iso: str = ""
    cycle_count: int = 0
    start_date_iso: str = ""              # date of first cycle (paper-trading start)
    paper_usdc: float = 0.0               # paper-mode simulated USDC balance

    @classmethod
    def load(cls) -> "BotState":
        if not config.STATE_FILE.exists():
            return cls()
        try:
            raw = json.loads(config.STATE_FILE.read_text())
            pos_raw = raw.pop("position", None)
            state = cls(**raw)
            if pos_raw:
                state.position = Position(**pos_raw)
            return state
        except Exception as e:
            log.error(f"Failed to load state, starting fresh: {e}")
            return cls()

    def save(self) -> None:
        d = asdict(self)
        config.STATE_FILE.write_text(json.dumps(d, indent=2, default=str))

    def now_iso(self) -> str:
        return datetime.now(timezone.utc).isoformat()
