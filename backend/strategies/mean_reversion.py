"""MEAN REVERSION (class B) — fades Bollinger band extremes back to the mid."""

from __future__ import annotations

from .base import Ctx, Intent, Strategy, bollinger, register


@register
class MeanReversion(Strategy):
    key = "mean_reversion"
    title = "MEAN REVERSION"
    klass = "B"
    stats = {"attack": 120, "defence": 250, "accuracy": 190, "dexterity": 140}
    skills = ["Bollinger fade", "Mid-band exit", "Percent risk"]
    blurb = (
        "Buys the lower band and sells the upper one on M5, expecting price to "
        "return to the mean. Quietest of the four."
    )

    def _targets(self, equity: float) -> tuple:
        tp = max(0.0, equity * float(self.p("tp_pct_equity", 1.0) or 0.0) / 100.0)
        sl = max(0.0, equity * float(self.p("sl_pct_equity", 1.5) or 0.0) / 100.0)
        return tp, sl

    def decide(self, ctx: Ctx) -> Intent:
        positions = list(ctx.positions or [])
        tp, sl = self._targets(ctx.equity)
        closes = ctx.closes()
        bands = bollinger(closes, int(self.p("bb_period", 20) or 20),
                          float(self.p("bb_dev", 2.0) or 2.0))

        if positions:
            profit = float((ctx.basket or {}).get("profit")
                           or sum(float(p.get("profit") or 0.0) for p in positions))
            if tp > 0 and profit >= tp:
                return Intent("close_basket", reason="reverted to mean, +%.2f" % profit)
            if sl > 0 and profit <= -sl:
                return Intent("close_basket", reason="reversion stop %.2f hit (%.2f)" % (sl, profit))
            side = str((ctx.basket or {}).get("side") or positions[0].get("type") or "")
            if bands and closes:
                lower, mid, upper = bands
                reached = (side == "buy" and closes[-1] >= mid) or (side == "sell" and closes[-1] <= mid)
                if reached and profit > 0:
                    return Intent("close_basket", reason="price reached the mid band at %.2f" % mid)
            return Intent("hold", reason="waiting for the mean, P/L %.2f" % profit)

        if not self.cooldown_ok(ctx.now):
            return Intent("hold", reason="cooldown")
        if not self.spread_ok(ctx):
            return Intent("hold", reason="spread %.2f too wide" % ctx.spread)
        if not ctx.bid or not ctx.ask or not closes:
            return Intent("hold", reason="no quote")
        if bands is None:
            return Intent("hold", reason="building Bollinger history")

        lower, mid, upper = bands
        price = closes[-1]
        if price <= lower:
            side = "buy"
        elif price >= upper:
            side = "sell"
        else:
            return Intent("hold", reason="price %.2f inside the bands (%.2f-%.2f)"
                                        % (price, lower, upper))

        volume = self.lot_for_equity(ctx.equity)
        if volume <= 0:
            return Intent("hold", reason="lot ladder resolved to zero")
        self._last_side = side
        return Intent(
            "open",
            side=side,
            volume=volume,
            reason="%s the %s band at %.2f, mid %.2f"
            % (side, "lower" if side == "buy" else "upper", price, mid),
        )
