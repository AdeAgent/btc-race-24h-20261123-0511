"""Two-mode cbBTC race bot for agent-fun/bars-replay/v1.

The dev window this bot was tuned on rises in two sharp jumps and then
chops sideways for hours, so the bot is built to catch a jump and then sit
still: it buys the whole cash balance the moment a fast rise is confirmed,
rides it with a wide trailing stop, and otherwise stays in cash. A second
way in is a dip that has started to climb back. The bot never shorts, never
uses leverage, never reads the clock or the network, and every decision is
integer arithmetic on closes it has already seen, so two runs on the same
bars always send the same orders.

Entry (flat only, at most one open position):
  * Jump ride -- the 15-bar return is at least +40 bps, or the 30-bar
    return is at least +80 bps, while the 120-bar return is above -100 bps
    (the market is not in free fall). A jump this fast is the start of the
    day's main leg, not noise: most one-minute bars move only a few bps.
  * Dip reclaim -- the close sits at least 100 bps under the highest close
    of the last 120 bars, but the 15-bar return has turned back up to at
    least +30 bps, so the dip is bouncing instead of still sliding.
  After any exit the trigger re-arms only once the 15-bar return has faded
  below +20 bps, so one jump cannot fire twice.

Exit (once holding):
  * Take profit at +600 bps against the position's cost.
  * Hard stop at -250 bps against the position's cost.
  * Trailing stop: sell once the close has fallen 200 bps below the
    highest close seen since entry.
  There is no time-based exit inside the window: the 2000-bar cap only
  guards against a run longer than the 1440-bar window itself. A position
  opened after batch 1380 would have no room to develop, so no new entry
  is taken there; the replay flattens whatever is held at the last close.

Costs: a full round trip pays about 26 bps (4 bps fee each way, 2 bps
modelled slippage each way, plus price impact on a ~$590k-deep pool), so
every trigger above is sized well clear of that. Orders cap their
slippage at 100 bps from the latest close the batch carried.

Wire notes: the shim below owns the protocol. It reads hello once, then
one batch per minute; each batch carries that minute's bar plus the fills
of the previous batch's orders and the account. This file answers every
batch with exactly one reply echoing its seq, carrying zero or one order.
An order decided on a batch fills at the next bar's open.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"

# --- entry triggers, all in basis points of close-to-close return ---
JUMP_R15_BPS = 40        # 15-bar return that marks a jump starting
JUMP_R30_BPS = 80        # 30-bar return that marks the same, slower
MIN_R120_BPS = -100      # jump entries need the 120-bar trend above this
DIP_BPS = 100            # dip-reclaim entries need price this far under the 120-bar high
RECLAIM_R15_BPS = 30     # ... and a 15-bar return at least this positive
REARM_R15_BPS = 20       # re-arm only after the 15-bar return fades under this

# --- exits, all in basis points ---
TAKE_PROFIT_BPS = 600
STOP_LOSS_BPS = 250
TRAIL_BPS = 200
MAX_HOLD_BARS = 2000     # safety bound beyond the 1440-bar window; exits are price-driven
LAST_ENTRY_SEQ = 1380    # no fresh entry after this batch
MAX_SLIPPAGE_BPS = 100


class RaceState:
    """Closes seen so far plus the per-position trackers, in one place."""

    def __init__(self):
        self.closes = []
        self.armed = True
        self.peak_close = 0
        self.held_bars = 0

    def ret_bps(self, now, bars_back):
        """Return of the latest close against the close bars_back ago, in bps."""
        if len(self.closes) <= bars_back:
            return 0
        then = self.closes[-1 - bars_back]
        if then <= 0:
            return -10 ** 9
        return now * 10000 // then - 10000

    def note_bar(self, close):
        self.closes.append(close)

    def fresh_jump(self, close):
        """True when a jump is starting: fast rise, trend not collapsing."""
        r15 = self.ret_bps(close, 15)
        r30 = self.ret_bps(close, 30)
        r120 = self.ret_bps(close, 120)
        if r120 <= MIN_R120_BPS:
            return False
        return r15 >= JUMP_R15_BPS or r30 >= JUMP_R30_BPS

    def dip_bouncing(self, close):
        """True when price is well under its 120-bar high but climbing back."""
        window = self.closes[-121:] if len(self.closes) > 121 else self.closes
        high = max(window)
        dip = close * 10000 // high - 10000 if high > 0 else 0
        return dip <= -DIP_BPS and self.ret_bps(close, 15) >= RECLAIM_R15_BPS

    def want_entry(self, close):
        if self.ret_bps(close, 15) < REARM_R15_BPS:
            self.armed = True
        if not self.armed:
            return False
        return self.fresh_jump(close) or self.dip_bouncing(close)

    def want_exit(self, state, held):
        """True when the open position should be closed on this close."""
        price = state.close(SYMBOL)
        if price is None:
            return False
        if price > self.peak_close:
            self.peak_close = price
        self.held_bars += 1
        value = state.value(SYMBOL, held.qty)
        cost = held.cost
        if cost <= 0:
            return True
        pnl_bps = value * 10000 // cost - 10000
        if pnl_bps >= TAKE_PROFIT_BPS:
            return True
        if pnl_bps <= -STOP_LOSS_BPS:
            return True
        if self.peak_close > 0 and price * 10000 <= self.peak_close * (10000 - TRAIL_BPS):
            return True
        if self.held_bars >= MAX_HOLD_BARS:
            return True
        return False


TRACKER = RaceState()


def on_batch(state, batch):
    for bar in batch["bars"]:
        if bar["symbol"] == SYMBOL:
            TRACKER.note_bar(int(bar["c"]))
    if state.warming_up:
        return []
    held = state.positions.get(SYMBOL)
    if held is not None:
        if TRACKER.want_exit(state, held):
            return [{
                "side": "sell",
                "symbol": SYMBOL,
                "qty": held.qty,
                "max_slippage_bps": MAX_SLIPPAGE_BPS,
            }]
        return []
    TRACKER.peak_close = 0
    TRACKER.held_bars = 0
    if state.seq > LAST_ENTRY_SEQ or len(TRACKER.closes) < 30:
        return []
    close = state.close(SYMBOL)
    if close is None:
        # A gap minute with no CBBTC bar yet: no signal to read, stay flat.
        return []
    if not TRACKER.want_entry(close):
        return []
    cash = state.cash
    room = state.weight_room(SYMBOL)
    size = cash if cash < room else room
    if size < int(state.hello["fees"]["min_notional_quote_units"]):
        return []
    TRACKER.armed = False
    TRACKER.peak_close = close
    return [{
        "side": "buy",
        "symbol": SYMBOL,
        "notional_quote_units": size,
        "max_slippage_bps": MAX_SLIPPAGE_BPS,
    }]


makefun_bars.run(on_batch)
