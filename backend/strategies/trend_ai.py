"""SMART TREND TRADER (class A) — trend following, only in a measurably trending market."""

from __future__ import annotations

from .base import Ctx, Intent, Strategy, adx, ema, register


@register
class TrendAI(Strategy):
    key = "trend_ai"
    title = "SMART TREND"
    klass = "A"
    stats = {"attack": 170, "defence": 230, "accuracy": 210, "dexterity": 150}
    skills = ["EMA trend", "ADX filter", "Percent risk"]
    blurb = (
        "Follows the M15 trend but only when ADX confirms one exists, sizing its "
        "target and stop as a percentage of equity."
    )

    def _targets(self, equity: float) -> tuple:
        tp = max(0.0, equity * float(self.p("tp_pct_equity", 2.5) or 0.0) / 100.0)
        sl = max(0.0, equity * float(self.p("sl_pct_equity", 1.2) or 0.0) / 100.0)
        return tp, sl

    def decide(self, ctx: Ctx) -> Intent:
        positions = list(ctx.positions or [])
        tp, sl = self._targets(ctx.equity)

        if positions:
            profit = float((ctx.basket or {}).get("profit")
                           or sum(float(p.get("profit") or 0.0) for p in positions))
            if tp > 0 and profit >= tp:
                return Intent("close_basket", reason="trend target %.2f hit (%.2f)" % (tp, profit))
            if sl > 0 and profit <= -sl:
                return Intent("close_basket", reason="trend stop %.2f hit (%.2f)" % (sl, profit))
            return Intent("hold", reason="riding trend, P/L %.2f of %.2f" % (profit, tp))

        if not self.cooldown_ok(ctx.now):
            return Intent("hold", reason="cooldown")
        if not self.spread_ok(ctx):
            return Intent("hold", reason="spread %.2f too wide" % ctx.spread)
        if not ctx.bid or not ctx.ask:
            return Intent("hold", reason="no quote")

        closes = ctx.closes()
        fast = ema(closes, int(self.p("ema_fast", 21) or 21))
        slow = ema(closes, int(self.p("ema_slow", 55) or 55))
        if fast is None or slow is None:
            return Intent("hold", reason="building trend history")

        strength = adx(ctx.candles, 14)
        floor = float(self.p("adx_min", 20.0) or 0.0)
        if strength is None:
            return Intent("hold", reason="building ADX history")
        if strength < floor:
            return Intent("hold", reason="ADX %.1f below %.1f — no trend to follow"
                                        % (strength, floor))

        side = "buy" if fast > slow else "sell"
        volume = self.lot_for_equity(ctx.equity)
        if volume <= 0:
            return Intent("hold", reason="lot ladder resolved to zero")
        self._last_side = side
        return Intent(
            "open",
            side=side,
            volume=volume,
            reason="%s trend confirmed, ADX %.1f" % (side, strength),
        )
