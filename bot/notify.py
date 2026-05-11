"""Telegram alerts. Silent no-op if token/chat_id are unset."""
from __future__ import annotations

import logging

import requests

from . import config

log = logging.getLogger(__name__)

TELEGRAM_URL = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str, parse_mode: str = "Markdown") -> bool:
    """Send a Telegram message. Returns True on success."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured; skipping send")
        return False
    try:
        url = TELEGRAM_URL.format(token=config.TELEGRAM_BOT_TOKEN)
        r = requests.post(
            url,
            json={
                "chat_id": config.TELEGRAM_CHAT_ID,
                "text": text,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            },
            timeout=10,
        )
        r.raise_for_status()
        return True
    except Exception as e:
        log.error(f"Telegram send failed: {e}")
        return False


def notify_cycle(equity_inr: float, signal: str, reason: str, position_summary: str) -> None:
    text = (
        f"*Cycle {signal}*\n"
        f"Equity: Rs {equity_inr:.2f}\n"
        f"Position: {position_summary}\n"
        f"Reason: {reason}"
    )
    send(text)


def notify_trade(action: str, token: str, qty: float, price_usd: float,
                 inr_value: float, paper: bool) -> None:
    tag = "[PAPER]" if paper else "[LIVE]"
    text = (
        f"*{tag} {action}*\n"
        f"{qty:.6f} {token} @ ${price_usd:.4f}\n"
        f"Notional: Rs {inr_value:.2f}"
    )
    send(text)


def notify_halt(reason: str, equity_inr: float) -> None:
    text = (
        f"🚨 *BOT HALTED*\n"
        f"Reason: {reason}\n"
        f"Equity: Rs {equity_inr:.2f}\n"
        f"Manual intervention required."
    )
    send(text)


def notify_error(stage: str, error: str) -> None:
    send(f"⚠️ *Error in {stage}*\n```\n{error[:500]}\n```")
