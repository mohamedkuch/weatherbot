#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
live_trader.py — places REAL Polymarket CLOB orders.

Only used when bot_v2 runs with LIVE=1. Reads the wallet key from the
environment (loaded from .env by bot_v2). The client is created lazily on the
first order, so the paper bot runs fine without py-clob-client installed.

Requires:  pip install py-clob-client
Env:       POLYMARKET_PRIVATE_KEY   (required)
           POLYMARKET_ADDRESS       (your Polymarket / funder address)
           POLYMARKET_SIG_TYPE      (0 = EOA key wallet [default], 2 = browser proxy)
Prereq:    deposit USDC + enable trading once on polymarket.com so the on-chain
           allowances are set, otherwise every order is rejected.
"""

import os
import threading

HOST     = "https://clob.polymarket.com"
CHAIN_ID = 137  # Polygon

_client = None
_lock   = threading.Lock()


def _get_client():
    global _client
    if _client is not None:
        return _client
    with _lock:
        if _client is None:
            from py_clob_client.client import ClobClient
            pk = os.environ.get("POLYMARKET_PRIVATE_KEY")
            if not pk:
                raise RuntimeError("POLYMARKET_PRIVATE_KEY not set in environment/.env")
            # signature_type=1 (Polymarket proxy) + funder=POLYMARKET_ADDRESS —
            # matches the working polymarket-mcp config for this wallet.
            sig_type = int(os.environ.get("POLYMARKET_SIGNATURE_TYPE")
                           or os.environ.get("POLYMARKET_SIG_TYPE") or "1")
            funder   = os.environ.get("POLYMARKET_ADDRESS")
            c = ClobClient(HOST, key=pk, chain_id=CHAIN_ID,
                           signature_type=sig_type, funder=funder)
            # Derive (or create) the API credentials used to authenticate orders.
            c.set_api_creds(c.create_or_derive_api_creds())
            _client = c
    return _client


def place_buy(token_id, price, shares):
    """Place a fill-or-kill BUY for `shares` of `token_id` at ~`price`.

    FOK means it fills at your price or cancels — no partial fills, no slippage
    past the limit. Returns the raw API response (contains the order id / status).
    """
    from py_clob_client.clob_types import OrderArgs, OrderType
    from py_clob_client.order_builder.constants import BUY

    client = _get_client()
    # Snap the price to the market's tick size, else the order is rejected.
    tick = float(client.get_tick_size(token_id))
    px   = round(round(float(price) / tick) * tick, 6)

    order = client.create_order(OrderArgs(
        token_id=token_id,
        price=px,
        size=float(shares),
        side=BUY,
    ))
    return client.post_order(order, OrderType.FOK)


if __name__ == "__main__":
    # Tiny manual smoke test — fill in a real YES token_id and a small size,
    # then run:  LIVE check ->  python live_trader.py <token_id> <price> <shares>
    import sys
    if len(sys.argv) == 4:
        tid, price, shares = sys.argv[1], float(sys.argv[2]), float(sys.argv[3])
        print(place_buy(tid, price, shares))
    else:
        print("usage: python live_trader.py <token_id> <price> <shares>")
