"""Solana wallet operations.

Read-only first (Phase 0): fetch SOL + SPL token balances by public address.
Signing capabilities added later in Phase 4 when we go live.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from solana.rpc.api import Client
from solders.pubkey import Pubkey

from . import config

log = logging.getLogger(__name__)

LAMPORTS_PER_SOL = 1_000_000_000
USDC_DECIMALS = 6


@dataclass
class WalletBalance:
    sol: float
    usdc: float

    def __str__(self) -> str:
        return f"SOL={self.sol:.6f}  USDC={self.usdc:.4f}"


def _client() -> Client:
    return Client(config.SOLANA_RPC_URL)


def get_sol_balance(pubkey_str: str) -> float:
    """Return native SOL balance for a wallet address."""
    client = _client()
    resp = client.get_balance(Pubkey.from_string(pubkey_str))
    lamports = resp.value
    return lamports / LAMPORTS_PER_SOL


def get_usdc_balance(pubkey_str: str) -> float:
    """Return USDC SPL token balance for a wallet address.

    Returns 0.0 if the wallet has no USDC token account yet.
    """
    client = _client()
    owner = Pubkey.from_string(pubkey_str)
    usdc_mint = Pubkey.from_string(config.TOKENS["USDC"])

    # Use getTokenAccountsByOwner with mint filter
    from solana.rpc.types import TokenAccountOpts

    opts = TokenAccountOpts(mint=usdc_mint)
    resp = client.get_token_accounts_by_owner_json_parsed(owner, opts)

    total = 0.0
    for acct in resp.value:
        info = acct.account.data.parsed["info"]
        amount = info["tokenAmount"]["uiAmount"]
        if amount is not None:
            total += float(amount)
    return total


def get_balance(pubkey_str: str) -> WalletBalance:
    """Return both SOL and USDC balances for the given wallet."""
    return WalletBalance(
        sol=get_sol_balance(pubkey_str),
        usdc=get_usdc_balance(pubkey_str),
    )


def get_main_balance() -> Optional[WalletBalance]:
    """Convenience: balance of the main (reference) wallet."""
    if not config.MAIN_WALLET_PUBKEY:
        return None
    return get_balance(config.MAIN_WALLET_PUBKEY)


def get_trading_balance() -> Optional[WalletBalance]:
    """Convenience: balance of the dedicated trading wallet."""
    if not config.TRADING_WALLET_PUBKEY:
        return None
    return get_balance(config.TRADING_WALLET_PUBKEY)


if __name__ == "__main__":
    # Quick smoke test
    import sys

    logging.basicConfig(level=logging.INFO)

    if len(sys.argv) > 1:
        addr = sys.argv[1]
    elif config.MAIN_WALLET_PUBKEY:
        addr = config.MAIN_WALLET_PUBKEY
    else:
        print("Usage: python -m bot.wallet <pubkey>")
        sys.exit(1)

    print(f"Wallet: {addr}")
    print(f"Balance: {get_balance(addr)}")
