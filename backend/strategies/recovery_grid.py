"""RECOVERY GRID (class S) — the strategy from the reference recording.

Frame-by-frame analysis of the reference session established seven behaviours,
all reproduced here:

1. every engine position on the symbol is one **basket**, managed as a unit;
2. **open** when flat, cooldown elapsed and the spread is inside ``max_spread_usd``;
3. a target volume above ``volume_split_max_lot`` is **split** into several orders,
   which is why the recording shows groups sharing one entry price
   (8 x sell 2.00 @ 4394.558) — the split itself lives in the bridge;
4. **add a layer** when price moves ``grid_step_usd`` against the newest entry;
5. **close the whole basket** at once when floating P/L reaches the target — the
   recording's baskets all share a single exit price;
6. **no stop loss by default**: the basket absorbs drawdown (the recording held
   -333.90 against a 1,488.97 balance). risk.py owns the hard guards, not this file;
7. the lot comes from an equity **ladder**, not a fixed size.

This module returns Intents only. It never touches the broker.
"""

from __future__ import annotations

from .base import Ctx, Intent, Strategy, register, trend_direction


@register
class RecoveryGrid(Strategy):
    key = "recovery_grid"
    title = "RECOVERY GRID"
    klass = "S"
    stats = {"attack": 260, "defence": 80, "accuracy": 150, "dexterity": 220}
    skills = ["Martingale recovery", "Basket close", "Equity lot scaling"]
    blurb = (
        "Averages into adverse moves and exits the whole basket on a profit target. "
        "Highest return, highest ruin risk — it holds drawdown instead of cutting it."
    )

    # -- target maths ------------------------------------------------------- #

    def tp_target(self, equity: float) -> float:
        """Currency profit that closes the basket."""
        fixed = float(self.p("basket_tp_usd", 0.0) or 0.0)
        if fixed > 0:
            return fixed
        pct = float(self.p("basket_tp_pct_equity", 0.0) or 0.0)
        try:
            equity = float(equity)
        except (TypeError, ValueError):
            equity = 0.0
        return max(0.0, equity * pct / 100.0)

    def sl_target(self, equity: float) -> float:
        """Currency loss that abandons the basket. 0 means no stop (the default)."""
        pct = float(self.p("basket_sl_pct_equity", 0.0) or 0.0)
        if pct <= 0:
            return 0.0
        try:
            equity = float(equity)
        except (TypeError, ValueError):
            equity = 0.0
        return max(0.0, equity * pct / 100.0)

    def layer_volume(self, equity: float, layers: int) -> float:
        """Lot for the next layer: ladder lot scaled by the layer multiplier."""
        base = self.lot_for_equity(equity)
        mult = float(self.p("layer_multiplier", 1.0) or 1.0)
        if mult <= 0:
            mult = 1.0
        try:
            volume = base * (mult ** max(0, int(layers)))
        except (OverflowError, ValueError):
            volume = base
        return round(volume, 2)

    # -- direction ---------------------------------------------------------- #

    def direction(self, ctx: Ctx) -> str:
        mode = str(self.p("direction_mode", "momentum") or "momentum").lower()
        if mode in ("buy", "long"):
            return "buy"
        if mode in ("sell", "short"):
            return "sell"
        if mode == "alternate":
            return "sell" if self._last_side == "buy" else "buy"
        period = int(self.p("momentum_period", 20) or 20)
        side = trend_direction(ctx.candles, period)
        return side  # "" when there is not enough history yet

    # -- decision ----------------------------------------------------------- #

    def decide(self, ctx: Ctx) -> Intent:
        equity = ctx.equity
        positions = list(ctx.positions or [])

        # ---------------- flat: consider opening ---------------- #
        if not positions:
            if not self.cooldown_ok(ctx.now):
                wait = float(self.p("cooldown_s", 0) or 0)
                left = max(0.0, wait - (ctx.now - self._last_close_ts))
                return Intent("hold", reason="cooldown %.0fs left" % left)
            if not self.spread_ok(ctx):
                return Intent(
                    "hold",
                    reason="spread %.2f above limit %.2f"
                    % (ctx.spread, float(self.p("max_spread_usd", 0) or 0)),
                )
            if not ctx.bid or not ctx.ask:
                return Intent("hold", reason="no quote")
            side = self.direction(ctx)
            if not side:
                return Intent("hold", reason="waiting for %d candles of history"
                                            % int(self.p("momentum_period", 20) or 20))
            volume = self.lot_for_equity(equity)
            if volume <= 0:
                return Intent("hold", reason="lot ladder resolved to zero")
            self._last_side = side
            self._last_layer_price = ctx.ask if side == "buy" else ctx.bid
            return Intent(
                "open",
                side=side,
                volume=volume,
                reason="open %s %.2f lots (equity %.2f, ladder tier)" % (side, volume, equity),
            )

        # ---------------- basket open: manage it ---------------- #
        basket = dict(ctx.basket or {})
        side = str(basket.get("side") or positions[0].get("type") or "").lower()
        self._last_side = side or self._last_side
        layers = int(basket.get("layers") or len(positions))
        profit = float(basket.get("profit") or sum(float(p.get("profit") or 0.0) for p in positions))

        target = self.tp_target(equity)
        if target > 0 and profit >= target:
            return Intent(
                "close_basket",
                side=side,
                reason="basket profit %.2f reached target %.2f across %d layers"
                % (profit, target, layers),
            )

        stop = self.sl_target(equity)
        if stop > 0 and profit <= -stop:
            return Intent(
                "close_basket",
                side=side,
                reason="basket loss %.2f breached stop %.2f" % (profit, stop),
            )

        max_layers = int(self.p("max_layers", 9) or 9)
        if layers >= max_layers:
            return Intent("hold", reason="holding %d layers (max %d), waiting for %.2f"
                                        % (layers, max_layers, target))

        # newest entry drives the grid — derived from positions so a restart is safe
        try:
            newest = max(positions, key=lambda p: (int(p.get("time") or 0), int(p.get("ticket") or 0)))
            last_entry = float(newest.get("price_open") or 0.0)
        except (ValueError, TypeError):
            last_entry = self._last_layer_price
        if last_entry <= 0:
            return Intent("hold", reason="no reference entry price")

        price = ctx.bid if side == "buy" else ctx.ask
        if not price:
            return Intent("hold", reason="no quote")
        adverse = (last_entry - price) if side == "buy" else (price - last_entry)
        step = float(self.p("grid_step_usd", 0.35) or 0.35)

        if adverse >= step:
            if not self.spread_ok(ctx):
                return Intent("hold", reason="layer due but spread %.2f too wide" % ctx.spread)
            volume = self.layer_volume(ctx.ladder_equity, layers)
            if volume <= 0:
                return Intent("hold", reason="layer volume resolved to zero")
            self._last_layer_price = price
            return Intent(
                "add_layer",
                side=side,
                volume=volume,
                reason="price moved %.2f against the basket (step %.2f) — layer %d of %d"
                % (adverse, step, layers + 1, max_layers),
            )

        return Intent(
            "hold",
            reason="%d layers, P/L %.2f of %.2f target, %.2f to next layer"
            % (layers, profit, target, max(0.0, step - adverse)),
        )
