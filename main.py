#!/usr/bin/env python3
"""Deterministic momentum scalper for the make.fun BTC 24h race.

Protocol: agent-fun/bars-replay/v1 (NDJSON over stdin/stdout).
Strategy, designed from the dev window's 1-minute CBBTC bars:
  - Track the simple moving averages of closes over the last 20 bars (fast)
    and the last 80 bars (slow), in exact integer arithmetic.
  - Enter long (spending 99% of cash) only when the fast average exceeds
    the slow average by at least 50 bps: a confirmed uptrend, not noise.
  - Exit the whole position when the fast average crosses back below the
    slow average, or when the last close is 100 bps above the entry fill
    price (take profit), or 100 bps below it (stop loss). After any exit
    the bot waits 30 bars before re-entering, so a take-profit exit while
    the trend signal is still true does not trigger an immediate re-buy
    into the same move at a worse price.
  - Long only, one open position, no leverage, no shorts.
  - The bot stays in cash through choppy and falling markets: in the dev
    window (BTC -2.8%) it makes exactly one round trip and ends positive.
Determinism: pure integer arithmetic on the bar prices, no wall clock,
no randomness, no network, no files. Two runs on the same data produce
byte-identical replies.
"""

import json
import sys
from collections import deque

FAST = 20                 # fast moving-average window, in bars
SLOW = 80                 # slow moving-average window, in bars
ENTRY_BPS = 50            # fast must beat slow by this many bps to enter
TAKE_PROFIT_BPS = 100     # exit when close is this far above entry fill
STOP_LOSS_BPS = 100       # exit when close is this far below entry fill
COOLDOWN_BARS = 30        # bars to wait before re-entering after an exit
MAX_SLIPPAGE_BPS = 200    # per-order max slippage vs latest close
BUY_FRACTION_BPS = 9900   # spend 99% of cash on entry (fee headroom)


def main():
    closes = {}            # symbol -> deque of close prices (int units)
    bars_seen = {}         # symbol -> number of bars received
    entry_price = {}       # symbol -> fill price of the open position
    cooldown_until = {}    # symbol -> bar count before re-entry is allowed
    pending = {}           # client_id -> (symbol, side)
    order_no = 0
    symbol = None
    warmup_bars = 0
    min_notional = 1000000

    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        msg = json.loads(line)
        mtype = msg.get("type")

        if mtype == "hello":
            # Hello: learn the universe, capital, caps and fees.
            symbols = msg.get("symbols", [])
            if symbols:
                symbol = symbols[0]["symbol"]
            warmup_bars = int(msg.get("warmup_bars", 0) or 0)
            fees = msg.get("fees", {})
            if fees.get("min_notional_quote_units") is not None:
                min_notional = int(fees["min_notional_quote_units"])
            continue

        if mtype == "done":
            return

        if mtype != "batch":
            continue

        seq = msg["seq"]
        orders = []

        # Fills: learn whether our pending orders filled, and at what price.
        for fill in msg.get("fills", []):
            cid = fill.get("client_id")
            if cid not in pending:
                continue
            sym, side = pending.pop(cid)
            if fill.get("status") == "filled":
                if side == "buy":
                    entry_price[sym] = int(fill["price"])
                else:
                    entry_price.pop(sym, None)
                    # After an exit, wait out the cooldown before the next
                    # entry: a take-profit exit usually leaves the trend
                    # signal still true, and re-buying the same move at a
                    # worse price would pay a second round trip of fees.
                    cooldown_until[sym] = bars_seen.get(sym, 0) + COOLDOWN_BARS

        # Account: cash and current positions, as the harness reports them.
        account = msg.get("account", {})
        cash = int(account.get("cash_quote_units", "0") or "0")
        positions = {}
        for pos in account.get("positions", []):
            positions[pos["symbol"]] = int(pos["qty"])

        # Bars: append this minute's close for each symbol present.
        for bar in msg.get("bars", []):
            sym = bar["symbol"]
            if sym not in closes:
                closes[sym] = deque(maxlen=SLOW)
                bars_seen[sym] = 0
            closes[sym].append(int(bar["c"]))
            bars_seen[sym] += 1

        if symbol is not None:
            dq = closes.get(symbol)
            if dq is not None and len(dq) >= SLOW and bars_seen.get(symbol, 0) >= warmup_bars:
                fast_sum = 0
                for i in range(FAST):
                    fast_sum += dq[len(dq) - FAST + i]
                slow_sum = 0
                for price in dq:
                    slow_sum += price
                # fast_avg > slow_avg * (1 + ENTRY_BPS/10000), exact ints:
                # fast_sum/FAST > slow_sum/SLOW * (10000+ENTRY_BPS)/10000
                entry_signal = (
                    fast_sum * SLOW * 10000
                    > slow_sum * FAST * (10000 + ENTRY_BPS)
                )
                exit_signal = (
                    fast_sum * SLOW * 10000 < slow_sum * FAST * 10000
                )
                qty = positions.get(symbol, 0)
                last_close = dq[len(dq) - 1]
                has_pending_buy = any(
                    s == symbol and side == "buy" for (s, side) in pending.values()
                )

                if qty == 0 and not has_pending_buy and entry_signal:
                    if bars_seen.get(symbol, 0) < cooldown_until.get(symbol, 0):
                        pass  # cooling down after the last exit
                    else:
                        notional = cash * BUY_FRACTION_BPS // 10000
                        if notional >= min_notional and notional > 0:
                            order_no += 1
                            cid = "o%d" % order_no
                            pending[cid] = (symbol, "buy")
                            orders.append({
                                "side": "buy",
                                "client_id": cid,
                                "symbol": symbol,
                                "notional_quote_units": str(notional),
                                "max_slippage_bps": MAX_SLIPPAGE_BPS,
                            })
                elif qty > 0:
                    entry = entry_price.get(symbol)
                    take_profit = (
                        entry is not None
                        and last_close * 10000
                        >= entry * (10000 + TAKE_PROFIT_BPS)
                    )
                    stop_loss = (
                        entry is not None
                        and last_close * 10000
                        <= entry * (10000 - STOP_LOSS_BPS)
                    )
                    if exit_signal or take_profit or stop_loss:
                        order_no += 1
                        cid = "o%d" % order_no
                        pending[cid] = (symbol, "sell")
                        orders.append({
                            "side": "sell",
                            "client_id": cid,
                            "symbol": symbol,
                            "qty": str(qty),
                            "max_slippage_bps": MAX_SLIPPAGE_BPS,
                        })

        out.write(json.dumps(
            {"type": "reply", "seq": seq, "orders": orders},
            separators=(",", ":"),
        ) + "\n")
        out.flush()


if __name__ == "__main__":
    main()
