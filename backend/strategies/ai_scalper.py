"""AI SCALPER (class A) — burst-entry basket scalps sized to measured volatility.

Rebuilt from the reference recording. What that robot actually does, frame by
frame, is four things — and only the fourth is what the old version here did:

1. **Burst entry.** One signal becomes several same-side positions at the *same*
   entry price, uniform lot, opened together. Not a ladder spread across a move.
2. **No noise-tight stop.** Nothing is stopped out seconds after opening. The
   floating loss on an individual leg is allowed to breathe.
3. **Whole-basket exit.** The group closes together on *aggregate* profit, not
   leg by leg.
4. Same fixed lot for every rung — no martingale.

The old version got (4) right and inverted (2), which is the whole story of this
strategy's record. Its per-position stop was ``sl_usd`` 0.80 converted to price,
i.e. 0.80 of gold, against a measured M1 ATR(14) of 3.806 — a stop at **0.21x
ATR**, one fifth of a typical one-minute candle. Measured over 41 closed baskets
on Deriv-Demo: 5 wins, 36 losses, -$77.66, a 12.2% win rate against the 38.7%
that bracket needed, and ``avg_layers`` of exactly 1.0 across all 41 — the stack
added for multiple positions never fired even once, because ``min_spacing_usd``
0.60 vetoed every layer that a burst would have opened at the same price.

So the fix is not a new entry signal. The entry was never the problem; the exit
geometry was. This module keeps the EMA cross and changes where the money sits:

* the **basket** money bracket (``tp_usd`` / ``sl_usd``, per 0.01 lot) is the
  primary exit, exactly as before — that is mechanic 3, and it is what
  ``_expectancy`` reads, so the number the UI judges stays the number that
  trades;
* the **broker-side** bracket becomes a wide ATR-scaled catastrophe net that is
  deliberately placed *outside* the basket exit, so the basket decides ordinary
  outcomes and the broker only decides what happens if this app dies mid-trade;
* entry volume is one order of ``unit x layers``, which ``mt5_bridge`` splits
  into ``layers`` orders that all fill at one price — mechanic 1, using the
  splitter that already exists for it rather than a new engine contract.
"""

from __future__ import annotations

from .base import Ctx, Intent, StopMove, Strategy, adx, atr, ema, register


@register
class AIScalper(Strategy):
    key = "ai_scalper"
    title = "AI SCALPER"
    klass = "A"
    stats = {"attack": 180, "defence": 190, "accuracy": 220, "dexterity": 280}
    skills = ["EMA cross", "Burst entry", "Basket exit", "ATR net"]
    blurb = (
        "Opens a burst of same-size positions at one price when the fast EMA "
        "crosses the slow one on M1, lets them breathe behind a wide "
        "volatility-scaled net, and closes the whole group together on its "
        "aggregate cash target."
    )

    #: brackets are quoted per 0.01 lot — the smallest position anyone trades
    LOT_UNIT = 0.01

    # -- money -------------------------------------------------------------- #

    def _open_lots(self, ctx: Ctx, positions: list) -> float:
        lots = 0.0
        try:
            lots = float((ctx.basket or {}).get("lots") or 0.0)
        except (TypeError, ValueError):
            lots = 0.0
        if lots > 0:
            return lots
        return sum(float(p.get("volume") or 0.0) for p in positions)

    def _basket_profit(self, ctx: Ctx, positions: list) -> float:
        try:
            booked = (ctx.basket or {}).get("profit")
        except AttributeError:
            booked = None
        if booked is not None:
            try:
                return float(booked)
            except (TypeError, ValueError):
                pass
        return sum(float(p.get("profit") or 0.0) for p in positions)

    def _bracket(self, name: str, default: float, lots: float) -> float:
        """The money bracket for *lots*, from a parameter quoted per 0.01 lot.

        ``tp_usd``/``sl_usd`` used to be read as a flat amount of account
        currency while :meth:`lot_for_equity` scaled the position twentyfold up
        the ladder, and nothing reconciled the two. On gold a 0.05-lot position
        is 5 ounces, so $1 of P/L is 0.20 of price: an $0.80 stop sits 0.16 away,
        which is *narrower than the spread*. The position therefore opened
        already past its own stop and every single trade was a guaranteed loss —
        measured at a 2% win rate across 727 baskets, and the whole of the
        "always in loss" report.

        Quoting the bracket per 0.01 lot turns it back into a fixed distance in
        price, so it means the same thing at every rung of the ladder and at
        every stack depth.
        """
        per_unit = float(self.p(name, default) or 0.0)
        if per_unit <= 0:
            return 0.0
        units = (float(lots) / self.LOT_UNIT) if lots > 0 else 1.0
        return per_unit * max(units, 0.0)

    def _money_to_price(self, ctx: Ctx, money: float) -> float:
        """Convert a per-0.01-lot cash figure into a distance in price.

        One 0.01 lot of a contract of ``contract_size`` units moves
        ``0.01 * contract_size`` of account currency per 1.0 of price. On gold
        (contract 100) the divisor is 1.0 and the numbers look identical, which
        is exactly the coincidence that would hide a missing division on a forex
        pair, where the contract is 100 000 and the divisor is 1000.
        """
        per_price = self.LOT_UNIT * (ctx.contract_size or 100.0)
        if per_price <= 0:
            return 0.0
        try:
            return max(0.0, float(money) / per_price)
        except (TypeError, ValueError):
            return 0.0

    # -- the broker-side net ------------------------------------------------ #

    def _distances(self, ctx: Ctx) -> tuple:
        """``(sl, tp)`` for the broker, as distances in price.

        This is a *net*, not the strategy's exit. Two separate jobs used to share
        one number and the tight one won:

        * the basket bracket below decides ordinary outcomes, on aggregate cash;
        * this one exists for the case the basket logic never gets to run — the
          app is closed, the machine sleeps, the connection drops. A broker-side
          stop is enforced tick by tick and survives all three.

        So it is scaled to volatility (``sl_atr_mult`` x ATR14) and then floored
        at ``broker_net_mult`` x the basket's own bracket — *both* legs. The floor
        is not redundant: ATR moves, and on a quiet session ``1.60 x ATR`` can fall
        *inside* a basket cap that is fixed in cash, which would have the broker
        closing single legs out from under a basket that was still working. The
        target leg needs the same treatment for the same reason — a broker target
        inside the basket's own would bank a part-win and break up a group whose
        whole premise is that it closes together. The floors make the ordering —
        basket first, broker last — hold at every volatility instead of only at the
        one that happened to be measured.

        Both legs are compared against the basket bracket per 0.01 lot, and that
        comparison is volume-independent: a basket of N rungs closes at N x its
        cash bracket, and N rungs of 0.01 lots reach N x that cash at exactly the
        same distance in price as one rung reaches one. So the ordering proved for
        a single rung holds for a burst of any depth.

        With every multiplier at 0 both legs fall back to the per-0.01-lot cash
        quote, which is the pre-rebuild behaviour and what the bracket-plumbing
        test pins.
        """
        band = atr(ctx.candles, 14)
        sl_mult = float(self.p("sl_atr_mult", 0.0) or 0.0)
        tp_mult = float(self.p("tp_atr_mult", 0.0) or 0.0)

        if band and sl_mult > 0:
            sl = sl_mult * float(band)
        else:
            sl = self._money_to_price(ctx, self.p("sl_usd", 0.0))
        if band and tp_mult > 0:
            tp = tp_mult * float(band)
        else:
            tp = self._money_to_price(ctx, self.p("tp_usd", 0.0))

        slack = float(self.p("broker_net_mult", 0.0) or 0.0)
        if slack > 0:
            sl = max(sl, self._money_to_price(ctx, self.p("sl_usd", 0.0)) * slack)
            tp = max(tp, self._money_to_price(ctx, self.p("tp_usd", 0.0)) * slack)
        return (max(0.0, sl), max(0.0, tp))

    # -- how big a burst this account can carry ----------------------------- #

    @staticmethod
    def _quantize(volume: float, step: float) -> float:
        if step <= 0:
            return round(max(0.0, volume), 2)
        return round(round(volume / step) * step, 6)

    def _fit(self, ctx: Ctx, unit: float) -> int:
        """How many rungs of *unit* lots the margin floor leaves room for.

        The video's eight-position burst is a property of its *broker*, not of
        its strategy: at 1:500 on a small account eight rungs cost almost
        nothing. Hard-coding eight here would produce eight orders on a
        high-leverage broker and a stream of rejections on this one, so the count
        is derived instead.

        Deriv charges a per-symbol 5% margin rate on gold — $217.68 per 0.01 lot
        measured, not the $72.56 the account's 1:30 leverage implies. At $2,714
        equity the 300% floor in ``risk.min_margin_level_pct`` therefore leaves
        room for three rungs, not eight, and the same code opens eight on a
        broker that allows it.

        ``margin_per_lot`` off the symbol is the broker's own
        ``order_calc_margin`` answer and is preferred over
        :attr:`Ctx.margin_per_lot`, which is the textbook formula and
        under-estimates by exactly 1.5x here. 0 rungs is a real answer — "the
        floor leaves no room" — and is distinct from "cannot measure", which
        returns a single rung rather than a burst.
        """
        per_lot = 0.0
        try:
            per_lot = float(ctx.symbol.get("margin_per_lot") or 0.0)
        except (TypeError, ValueError, AttributeError):
            per_lot = 0.0
        per_lot = per_lot or ctx.margin_per_lot
        if per_lot <= 0:
            return 1  # cannot price margin: one rung, never a burst

        try:
            floor = float((ctx.risk or {}).get("min_margin_level_pct") or 0.0)
        except (TypeError, ValueError, AttributeError):
            floor = 0.0
        if floor <= 0:
            return max(1, int(self.p("max_positions", 5) or 1))

        # RiskManager stops *opening* at min_margin x 1.25 and force-closes at
        # min_margin itself, so sizing against the bare floor builds a basket its
        # own risk manager refuses to extend and then liquidates. Size against
        # the band it actually enforces.
        band = float(self.p("entry_margin_band", 1.25) or 1.25)
        if band <= 0:
            band = 1.25
        ceiling = ctx.equity * 100.0 / (floor * band)
        room = ceiling - ctx.margin_used
        per_rung = per_lot * unit
        if per_rung <= 0 or room <= 0:
            return 0
        return max(0, int(room / per_rung))

    def _plan(self, ctx: Ctx) -> tuple:
        """``(unit lot, layers)`` for a fresh burst, or ``(0.0, 0)``.

        The rung is ``base_lot``, floored at the broker minimum — deliberately
        *not* :meth:`lot_for_equity`. Growth adds rungs here, it does not inflate
        them, which is both what the recording shows (eight identical small
        positions, never one large one) and the only version of this that stays
        internally consistent: ``volume_split_max_lot`` is static config, so a
        ladder that moved the rung to 0.05 while the splitter still cut at 0.01
        would turn a 3-rung plan into 15 tickets of the same total volume, and 15
        trips the ``risk.max_basket_layers`` ceiling of 12 on a basket the
        strategy thinks is three deep. Fixing the rung keeps layer count and
        position count the same number.

        The ladder's 0.05 tier could not be afforded on this account anyway: 0.05
        lots of gold costs $1,088 of margin against the $723 the 300% floor
        allows, which is the same arithmetic that had ``lots_allowed`` trimming
        0.50 to 0.06. ``lot_tiers`` stays in config because ``roster()`` reads it
        for the card's lot-range label.
        """
        step = ctx.volume_step or self.LOT_UNIT
        vmin = ctx.volume_min or self.LOT_UNIT
        cap = max(1, int(self.p("max_positions", 5) or 1))
        try:
            hard = int((ctx.risk or {}).get("max_basket_layers") or 0)
        except (TypeError, ValueError, AttributeError):
            hard = 0
        if hard > 0:
            cap = min(cap, hard)

        unit = self._quantize(max(float(self.p("base_lot", 0.01) or 0.0), vmin), step)
        if unit <= 0:
            return (0.0, 0)
        layers = self._fit(ctx, unit)
        if layers < 1:
            return (0.0, 0)
        return (unit, min(layers, cap))

    # -- topping up a burst that did not fully fill ------------------------- #

    def _top_up(self, ctx: Ctx, positions: list, fast: float, slow: float):
        """The rest of a burst the broker or the risk manager did not fill.

        A burst is one order that ``market_order`` splits, so a partial fill is
        normal: chunks are sent in sequence and any of them can be rejected, and
        ``lots_allowed`` can trim the total on the way through. Reading the
        shortfall back off the broker's own position list rather than remembering
        what was asked for means a rung that was refused cannot leave this
        strategy believing it exists.

        The whole basket stays inside ``max_positions`` rungs of ``base_lot``,
        enforced twice — once on the count and once on the volume — for the reason
        given at the ceiling itself.

        ``min_spacing_usd`` still applies when it is set. It defaults to 0 now,
        because a burst is *supposed* to share one price and that veto is exactly
        what held ``avg_layers`` at 1.0 across 41 baskets — but a user who wants
        rungs spread across a move can still ask for that and get it.
        """
        cap = max(1, int(self.p("max_positions", 5) or 1))
        if len(positions) >= cap:
            return None
        if not self.spread_ok(ctx) or not ctx.bid or not ctx.ask:
            return None

        sides = {str(p.get("type") or "").lower() for p in positions}
        if len(sides) != 1:
            return None  # hedged both ways: not this strategy's doing, leave it alone
        side = sides.pop()
        if side not in ("buy", "sell"):
            return None
        if (side == "buy") != (fast > slow):
            return None

        price = ctx.ask if side == "buy" else ctx.bid
        gap = float(self.p("min_spacing_usd", 0.0) or 0.0)
        if gap > 0:
            entries = [float(p.get("price_open") or 0.0) for p in positions]
            entries = [e for e in entries if e > 0]
            if entries and min(abs(price - e) for e in entries) < gap:
                return None

        # Every rung of a burst is the same size and that size comes from the plan,
        # not from averaging the broker's position list. On a NETTING account
        # MetaTrader merges the whole burst into ONE position, so
        # ``open_lots / len(positions)`` reads a five-rung 0.05 basket as a single
        # 0.05 rung — which both misreports the burst and lifts the ceiling below
        # to five times where it belongs. Config gives the same answer as the
        # position list on a hedging account and the right one on a netting.
        unit, fit = self._plan(ctx)
        if unit <= 0 or fit < 1:
            return None

        # Two ceilings, and the second is not redundant: the count checked at the
        # top of this method never binds on a netting account, where
        # ``len(positions)`` stays 1 however many times this fires. Volume is the
        # quantity that actually decides what the basket costs, so volume is the
        # one that is capped — without it the stack grows without bound and this
        # becomes a martingale by accident.
        room = cap * unit - self._open_lots(ctx, positions)
        want = min(cap - len(positions), int((room + 1e-9) / unit), fit)
        if want < 1:
            return None
        volume = self._quantize(unit * want, ctx.volume_step or self.LOT_UNIT)
        if volume <= 0:
            return None

        sl_dist, tp_dist = self._distances(ctx)
        return Intent(
            "add_layer", side=side, volume=volume,
            sl_distance=sl_dist, tp_distance=tp_dist,
            reason="topping burst to %d/%d rungs of %.2f, %s still %s" % (
                len(positions) + want, cap, unit,
                "EMA%d" % int(self.p("ema_fast", 8) or 8),
                "above" if side == "buy" else "below",
            ),
        )

    # -- bracket management on open positions ------------------------------- #

    def manage(self, ctx: Ctx) -> list:
        """Pull the broker net up to break-even once the basket is mostly won.

        The basket target is the exit, so this is not a trailing stop chasing
        price — it is insurance against the specific way a basket bracket fails:
        the group runs to within a fraction of its cash target, reverses, and
        gives all of it back because the only thing behind it was a net placed
        1.6x ATR away at entry.

        Ratcheting is decided here rather than relied on downstream.
        ``Strategy.manage``'s docstring says the engine drops any move that does
        not improve on the current stop; ``TradingEngine._manage`` does not — it
        forwards every move with a ticket verbatim — so a strategy that trusted
        that sentence would be free to send a stop backwards. Each move below is
        compared against the position's own live ``sl`` first, and skipped when
        it is not an improvement, which also makes this idempotent across the
        once-a-second call it actually gets.
        """
        positions = [p for p in (ctx.positions or []) if p]
        if not positions:
            return []
        frac = float(self.p("breakeven_at_frac", 0.0) or 0.0)
        if frac <= 0:
            return []
        lots = self._open_lots(ctx, positions)
        target = self._bracket("tp_usd", 0.0, lots)
        if target <= 0:
            return []
        if self._basket_profit(ctx, positions) < target * frac:
            return []

        lock = self._money_to_price(ctx, self.p("breakeven_lock_usd", 0.0))
        moves = []
        for pos in positions:
            try:
                ticket = int(pos.get("ticket") or 0)
            except (TypeError, ValueError):
                continue
            if ticket <= 0:
                continue
            side = str(pos.get("type") or "").lower()
            try:
                entry = float(pos.get("price_open") or 0.0)
                current = float(pos.get("sl") or 0.0)
            except (TypeError, ValueError):
                continue
            if entry <= 0:
                continue

            if side == "buy":
                want = entry + lock
                if current > 0 and want <= current + 1e-9:
                    continue  # ratchet: never sideways, never backwards
                if ctx.bid and want >= ctx.bid:
                    continue  # a stop through the market is a market order
            elif side == "sell":
                want = entry - lock
                if current > 0 and want >= current - 1e-9:
                    continue
                if ctx.ask and want <= ctx.ask:
                    continue
            else:
                continue

            moves.append(StopMove(
                ticket=ticket, sl=round(want, 5), tp=0.0,
                reason="basket %.0f%% of target: locking rung at break-even"
                       % (frac * 100.0),
            ))
        return moves

    # -- decision ----------------------------------------------------------- #

    def decide(self, ctx: Ctx) -> Intent:
        positions = [p for p in (ctx.positions or []) if p]
        closes = ctx.closes()
        fast = ema(closes, int(self.p("ema_fast", 8) or 8))
        slow = ema(closes, int(self.p("ema_slow", 21) or 21))

        if positions:
            lots = self._open_lots(ctx, positions)
            profit = self._basket_profit(ctx, positions)
            tp = self._bracket("tp_usd", 1.20, lots)
            sl = self._bracket("sl_usd", 0.80, lots)
            if tp > 0 and profit >= tp:
                return Intent("close_basket",
                              reason="basket target %.2f hit (%.2f)" % (tp, profit))
            if sl > 0 and profit <= -sl:
                return Intent("close_basket",
                              reason="basket max loss %.2f hit (%.2f)" % (sl, profit))
            if fast is not None and slow is not None:
                add = self._top_up(ctx, positions, fast, slow)
                if add is not None:
                    return add
            return Intent("hold",
                          reason="%d rung%s open, P/L %.2f (tp %.2f / max loss %.2f on "
                                 "%.2f lots)" % (len(positions),
                                                 "" if len(positions) == 1 else "s",
                                                 profit, tp, sl, lots))

        if not self.cooldown_ok(ctx.now):
            return Intent("hold", reason="cooldown")
        if not self.spread_ok(ctx):
            return Intent("hold", reason="spread %.2f too wide for scalping" % ctx.spread)
        if not ctx.bid or not ctx.ask:
            return Intent("hold", reason="no quote")

        if fast is None or slow is None:
            return Intent("hold", reason="building EMA history")

        # How far apart the EMAs must be before the cross counts. Sizing that in
        # spreads ties the signal to the broker's pricing rather than to the
        # market: a wide-spread broker demands more separation than a tight one
        # for the identical chart. A fraction of ATR asks the same question of
        # every broker, and rises with volatility, which is when a near-flat
        # cross is least worth trusting. Spread stays as the floor — a separation
        # smaller than the cost of entry is not a signal at any volatility.
        sep = abs(fast - slow)
        floor = ctx.spread or 0.01
        band = atr(ctx.candles, 14)
        if band:
            floor = max(floor, float(self.p("min_sep_atr", 0.30) or 0.0) * band)
        if sep < floor:
            return Intent("hold", reason="EMAs %.2f apart, need %.2f" % (sep, floor))

        # A burst commits several rungs at one price, so it is far more exposed
        # to a cross that is really just chop than a single position was. ADX is
        # the cheapest available answer to "is there a trend here at all", and it
        # needs no extra history beyond what ATR already asks for.
        min_adx = float(self.p("min_adx", 0.0) or 0.0)
        if min_adx > 0:
            strength = adx(ctx.candles, 14)
            if strength is not None and float(strength) < min_adx:
                return Intent("hold", reason="ADX %.1f below %.1f — chop, not a trend"
                                             % (float(strength), min_adx))

        unit, layers = self._plan(ctx)
        if unit <= 0 or layers < 1:
            return Intent("hold", reason="margin floor leaves no room for a rung")
        volume = self._quantize(unit * layers, ctx.volume_step or self.LOT_UNIT)
        if volume <= 0:
            return Intent("hold", reason="lot ladder resolved to zero")

        side = "buy" if fast > slow else "sell"
        self._last_side = side
        sl_dist, tp_dist = self._distances(ctx)
        return Intent(
            "open",
            side=side,
            volume=volume,
            sl_distance=sl_dist,
            tp_distance=tp_dist,
            reason="EMA%d %.2f %s EMA%d %.2f — burst %dx%.2f" % (
                int(self.p("ema_fast", 8) or 8), fast,
                "above" if side == "buy" else "below",
                int(self.p("ema_slow", 21) or 21), slow,
                layers, unit,
            ),
        )
