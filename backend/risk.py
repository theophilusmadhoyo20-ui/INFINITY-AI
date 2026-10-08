"""Risk manager — the only thing standing between the grid and a blown account.

The reference recording repeatedly ran the margin level down to 80.38%, 86.74%
and 100.13%, i.e. at or below the broker's margin-call line. That account
survived; the same sequence liquidates on a slightly worse tick. This module
makes that state measurable and visible instead of hidden, and can force the
engine flat before it happens.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional


def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    if out != out or out in (float("inf"), float("-inf")):  # NaN / inf
        return default
    return out


LEVELS = ("safe", "caution", "danger", "critical")

#: How far above ``min_margin_level_pct`` a *newly sized* position must land.
#:
#: ``lots_allowed`` and ``evaluate`` are two halves of one risk manager and they
#: used to disagree at the boundary by construction: sizing solved for margin
#: level *exactly* equal to the floor, while ``evaluate`` force-closes at
#: ``margin_level <= floor``. The only thing keeping them apart was the
#: incidental headroom left by truncating lots down to a whole ``volume_step`` —
#: so whenever headroom/per_lot happened to land on an exact step multiple, the
#: engine opened a position its own next tick was obliged to close.
#:
#: 1.10 sizes against a 10%-higher floor, which means equity can float ~9% against
#: the position before the real floor is reached. That is far more than the spread
#: the entry immediately costs, at any size this buffer permits. The price is a
#: slightly larger minimum viable account: at a 300% floor and gold at 5% margin,
#: 0.01 lots needs about $725 of equity rather than $659.
SIZING_MARGIN_BUFFER = 1.10


class RiskManager:
    """Stateful across calls: it remembers peak equity for drawdown."""

    def __init__(self, config: dict, logger: Optional[logging.Logger] = None) -> None:
        self.config = config or {}
        self.log = logger or logging.getLogger("infinity.risk")
        self._lock = threading.Lock()
        self.peak_equity = 0.0

    # ------------------------------------------------------------------ #

    def _risk(self) -> dict:
        return dict(self.config.get("risk") or {})

    def seed_peak(self, equity: float) -> None:
        """Prime peak equity from stored history so drawdown survives a restart."""
        with self._lock:
            self.peak_equity = max(self.peak_equity, _f(equity))

    def evaluate(self, account: dict, positions: list, day_start_equity: float = 0.0) -> dict:
        account = dict(account or {})
        positions = list(positions or [])
        risk = self._risk()

        equity = _f(account.get("equity"))
        balance = _f(account.get("balance"), equity)
        margin = _f(account.get("margin"))
        raw_margin_level = _f(account.get("margin_level"))
        so_so = _f(account.get("so_so"), 50.0) or 50.0
        so_call = _f(account.get("so_call"), 100.0) or 100.0
        open_lots = sum(_f(p.get("volume")) for p in positions)

        with self._lock:
            if equity > self.peak_equity:
                self.peak_equity = equity
            peak = self.peak_equity or equity

        # --- the flat-account trap ------------------------------------- #
        # MT5 reports margin_level 0.0 when nothing is open. Treating that as a
        # breach would force-close a flat account on every single tick.
        flat = not positions or margin <= 0.0
        if flat:
            margin_level = float("inf")
            margin_level_out = 0.0  # honest: there is no ratio to report
        else:
            margin_level = raw_margin_level if raw_margin_level > 0 else (
                (equity / margin * 100.0) if margin > 0 else float("inf")
            )
            margin_level_out = round(margin_level, 2)

        # headroom above the stop-out line, as a percentage of the way there
        if flat or margin_level == float("inf"):
            stop_out_distance = 100.0
        elif margin_level <= so_so:
            stop_out_distance = 0.0
        else:
            stop_out_distance = max(
                0.0, min(100.0, (margin_level - so_so) / max(margin_level, 1e-9) * 100.0)
            )

        drawdown_pct = 0.0 if peak <= 0 else max(0.0, (peak - equity) / peak * 100.0)
        start = _f(day_start_equity) or balance or equity
        daily_pl_pct = 0.0 if start <= 0 else (equity - start) / start * 100.0

        min_margin = _f(risk.get("min_margin_level_pct"), 0.0)
        max_dd = _f(risk.get("max_drawdown_pct"), 0.0)
        daily_limit = _f(risk.get("daily_loss_limit_pct"), 0.0)
        equity_floor = _f(risk.get("equity_floor"), 0.0)
        max_layers = int(_f(risk.get("max_basket_layers"), 0.0))
        max_lots = _f(risk.get("max_total_lots"), 0.0)
        kill_switch = bool(risk.get("kill_switch"))

        reasons: list = []
        force_close = False
        allow_open = True

        # --- hard breaches -------------------------------------------- #
        if not flat and min_margin > 0 and margin_level <= min_margin:
            force_close = True
            reasons.append(
                "Margin level %.2f%% is at or below the %.0f%% floor — closing to protect the account."
                % (margin_level, min_margin)
            )
        if not flat and margin_level <= so_call:
            force_close = True
            reasons.append(
                "Margin level %.2f%% has reached the broker's margin-call line (%.0f%%). "
                "One adverse tick from here starts forced liquidation at %.0f%%."
                % (margin_level, so_call, so_so)
            )
        if max_dd > 0 and drawdown_pct >= max_dd:
            force_close = True
            reasons.append(
                "Drawdown %.2f%% from the %.2f peak breached the %.0f%% limit."
                % (drawdown_pct, peak, max_dd)
            )
        if daily_limit > 0 and daily_pl_pct <= -daily_limit:
            force_close = True
            reasons.append(
                "Down %.2f%% today, past the %.0f%% daily loss limit."
                % (abs(daily_pl_pct), daily_limit)
            )
        if equity_floor > 0 and equity <= equity_floor:
            force_close = True
            reasons.append(
                "Equity %.2f is at or below the %.2f floor." % (equity, equity_floor)
            )
        if not bool(account.get("trade_allowed", True)):
            allow_open = False
            reasons.append("The broker has trading disabled on this account right now.")

        # --- soft limits: stop adding, keep what is open --------------- #
        if max_layers > 0 and len(positions) >= max_layers:
            allow_open = False
            reasons.append(
                "%d open positions has reached the %d-layer cap — no new layers."
                % (len(positions), max_layers)
            )
        if max_lots > 0 and open_lots >= max_lots:
            allow_open = False
            reasons.append(
                "%.2f lots open has reached the %.2f cap — no new layers." % (open_lots, max_lots)
            )
        if not flat and min_margin > 0 and margin_level <= min_margin * 1.25:
            allow_open = False
            if not force_close:
                reasons.append(
                    "Margin level %.2f%% is within 25%% of the %.0f%% floor — holding off new entries."
                    % (margin_level, min_margin)
                )

        if kill_switch:
            reasons.insert(0, "Kill switch is on: closing everything and stopping the engine.")

        if force_close or kill_switch:
            allow_open = False

        # --- level ----------------------------------------------------- #
        if kill_switch or force_close or (not flat and margin_level <= so_so * 1.2):
            level = "critical"
        elif not flat and min_margin > 0 and margin_level <= min_margin:
            level = "danger"
        elif (
            (not flat and min_margin > 0 and margin_level <= min_margin * 1.25)
            or (max_dd > 0 and drawdown_pct >= max_dd * 0.75)
            or (daily_limit > 0 and daily_pl_pct <= -daily_limit * 0.75)
        ):
            level = "caution"
        else:
            level = "safe"

        if not reasons:
            if flat:
                reasons.append("Flat — no exposure.")
            else:
                reasons.append(
                    "Margin level %.2f%%, %.0f%% clear of the %.0f%% stop-out."
                    % (margin_level, stop_out_distance, so_so)
                )

        return {
            "allow_open": bool(allow_open),
            "force_close": bool(force_close or kill_switch),
            "kill": bool(kill_switch),
            "margin_level": margin_level_out,
            "stop_out_distance_pct": round(stop_out_distance, 2),
            "drawdown_pct": round(drawdown_pct, 2),
            "daily_pl_pct": round(daily_pl_pct, 2),
            "level": level,
            "reasons": reasons,
            # extras the UI shows but the contract does not require
            "peak_equity": round(peak, 2),
            "open_lots": round(open_lots, 2),
            "so_call": so_call,
            "so_so": so_so,
            "flat": flat,
        }

    # ------------------------------------------------------------------ #

    def lots_allowed(self, account: dict, symbol: dict, config: dict = None) -> float:
        """Additional lots that still leave margin level above the floor.

        Returns ``0.0`` for "no room at all" and ``-1.0`` for "cannot measure".
        Collapsing those two into one value is what let the margin trim be skipped
        on exactly the order that had no headroom left: quantizing headroom down to
        a whole ``volume_step`` returns 0.0, and a caller reading 0.0 as "no cap
        information" would then send the order at full size.

        The size returned is deliberately clear of the floor by
        :data:`SIZING_MARGIN_BUFFER`, not merely at it — see that constant.
        """
        account = dict(account or {})
        symbol = dict(symbol or {})
        risk = dict((config or self.config).get("risk") or {})
        floor = _f(risk.get("min_margin_level_pct"), 0.0)
        equity = _f(account.get("equity"))
        margin = _f(account.get("margin"))
        if equity <= 0:
            return 0.0        # no equity is no room, not missing information
        if floor <= 0:
            floor = 100.0

        # Margin required per lot, best source first.
        #
        # 1. What the broker says. The bridge asks the terminal via
        #    order_calc_margin, which already knows the per-symbol margin rate.
        # 2. What the account is currently being charged. Right for the open
        #    basket, but `margin` is account-wide while `_open_lots` is only the
        #    engine's, so a manual position on another symbol inflates it. Wrong
        #    in the safe direction, which is why it stays ahead of the formula.
        # 3. price * contract_size / leverage. Textbook, and wrong on any broker
        #    that charges a per-symbol rate: measured 1.500x too cheap on gold
        #    here (5% of notional charged against a 1:30 account). Kept only so a
        #    terminal that will not answer degrades instead of refusing to trade.
        open_lots = 0.0
        try:
            open_lots = _f(symbol.get("_open_lots"))
        except Exception:
            open_lots = 0.0
        per_lot = _f(symbol.get("margin_per_lot"))
        if per_lot <= 0 and margin > 0 and open_lots > 0:
            per_lot = margin / open_lots
        if per_lot <= 0:
            contract = _f(symbol.get("trade_contract_size"), 100.0) or 100.0
            price = _f(symbol.get("ask")) or _f(symbol.get("bid")) or 0.0
            leverage = _f(account.get("leverage"), 100.0) or 100.0
            per_lot = (contract * price / leverage) if price else 0.0
        if per_lot <= 0:
            return -1.0       # no price or leverage to work from: unmeasurable

        # equity / (margin + x * per_lot) * 100 >= floor * buffer
        max_margin = equity * 100.0 / (floor * SIZING_MARGIN_BUFFER)
        headroom = max_margin - margin
        if headroom <= 0:
            return 0.0
        step = _f(symbol.get("volume_step"), 0.01) or 0.01
        lots = headroom / per_lot
        lots = int(lots / step) * step
        vmax = _f(symbol.get("volume_max"), 100.0) or 100.0
        return round(max(0.0, min(lots, vmax)), 2)


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import os
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logging.basicConfig(level=logging.WARNING)

    from backend.config import load_config

    cfg = load_config()
    rm = RiskManager(cfg)
    passed = failed = 0

    def check(label, cond, detail=""):
        global passed, failed
        if cond:
            passed += 1
            print("[ok  ] %s %s" % (label, detail))
        else:
            failed += 1
            print("[FAIL] %s %s" % (label, detail))

    def acct(**kw):
        base = {"equity": 1000.0, "balance": 1000.0, "margin": 0.0, "margin_free": 1000.0,
                "margin_level": 0.0, "so_call": 100.0, "so_so": 50.0, "trade_allowed": True,
                "leverage": 500}
        base.update(kw)
        return base

    def p(volume=0.10, profit=0.0):
        return {"ticket": 1, "volume": volume, "profit": profit, "type": "buy",
                "price_open": 2400.0, "time": 0}

    print("=" * 72)
    print("INFINITY.100 AI - risk.py self-test")
    print("=" * 72)

    # the trap: flat account, MT5 reports margin_level 0
    r = rm.evaluate(acct(), [], 1000.0)
    check("flat account (margin_level 0, no positions) is SAFE",
          r["level"] == "safe" and not r["force_close"] and r["allow_open"],
          "%s force_close=%s" % (r["level"], r["force_close"]))

    # healthy open basket
    r = rm.evaluate(acct(margin=100.0, margin_level=900.0), [p()], 1000.0)
    check("margin level 900% is safe", r["level"] == "safe" and r["allow_open"], str(r["margin_level"]))

    # the reference recording's real numbers
    r = rm.evaluate(acct(equity=1155.07, balance=1488.97, margin=1437.0, margin_level=80.38),
                    [p(0.5, -333.90)], 1488.97)
    check("the recording's 80.38% margin level is CRITICAL and forces a close",
          r["level"] == "critical" and r["force_close"],
          "%s %s | %s" % (r["level"], r["force_close"], r["reasons"][0][:70]))

    r = rm.evaluate(acct(equity=1200.0, margin=1383.0, margin_level=86.74), [p(0.5, -280.0)], 1488.97)
    check("86.74% is critical", r["level"] == "critical" and r["force_close"], str(r["margin_level"]))

    r = rm.evaluate(acct(equity=1400.0, margin=1398.0, margin_level=100.13), [p(0.5, -80.0)], 1488.97)
    check("100.13% (at the margin-call line) forces a close",
          r["force_close"], "%s %s" % (r["level"], r["margin_level"]))

    # just above the configured 300% floor -> caution, no new entries
    r = rm.evaluate(acct(equity=1000.0, margin=280.0, margin_level=357.0), [p()], 1000.0)
    check("357% (within 25% of the 300% floor) -> caution, entries paused",
          r["level"] == "caution" and not r["allow_open"] and not r["force_close"],
          "%s allow_open=%s" % (r["level"], r["allow_open"]))

    # drawdown breach
    rm2 = RiskManager(cfg)
    rm2.evaluate(acct(equity=10000.0, margin=100.0, margin_level=9000.0), [p()], 10000.0)
    r = rm2.evaluate(acct(equity=6500.0, margin=100.0, margin_level=6000.0), [p()], 10000.0)
    check("35% drawdown from peak breaches the 30% limit",
          r["force_close"] and r["drawdown_pct"] >= 30.0, "dd=%.2f%%" % r["drawdown_pct"])

    # daily loss breach
    r = rm.evaluate(acct(equity=780.0, margin=100.0, margin_level=780.0), [p()], 1000.0)
    check("-22% on the day breaches the 20% daily limit",
          r["force_close"] and r["daily_pl_pct"] <= -20.0, "%.2f%%" % r["daily_pl_pct"])

    # kill switch
    cfg2 = load_config()
    cfg2["risk"]["kill_switch"] = True
    r = RiskManager(cfg2).evaluate(acct(margin=100.0, margin_level=900.0), [p()], 1000.0)
    check("kill switch -> kill + force_close + critical",
          r["kill"] and r["force_close"] and r["level"] == "critical", r["reasons"][0][:50])

    # layer cap
    r = rm.evaluate(acct(margin=100.0, margin_level=900.0), [p() for _ in range(12)], 1000.0)
    check("12 positions hits the max_basket_layers cap",
          not r["allow_open"] and not r["force_close"], r["reasons"][0][:60])

    # trading disabled by broker
    r = rm.evaluate(acct(trade_allowed=False), [], 1000.0)
    check("broker trade_allowed=False blocks opening", not r["allow_open"], r["reasons"][0][:50])

    # contract key completeness
    need = {"allow_open", "force_close", "kill", "margin_level", "stop_out_distance_pct",
            "drawdown_pct", "daily_pl_pct", "level", "reasons"}
    r = rm.evaluate(acct(), [], 1000.0)
    check("every contract key present", need.issubset(r), str(sorted(need - set(r))))

    # lots_allowed
    lots = rm.lots_allowed(acct(equity=1000.0, margin=0.0, leverage=500),
                           {"trade_contract_size": 100.0, "ask": 2400.0, "volume_step": 0.01,
                            "volume_max": 100.0})
    check("lots_allowed returns a sane positive size", 0 < lots < 100, "%.2f lots" % lots)

    # --- the loss loop: sizing must not approve what evaluate will force-close --- #
    # Live on Deriv-Demo this cost 22 losing baskets out of 23, each held one second.
    # lots_allowed sized from account leverage (1:30) while the broker charged a
    # per-symbol rate on gold (5% of notional = 1:20), so it under-charged by 1.5x,
    # opened a position at ~260% margin level and evaluate closed it on the next tick
    # against the 300% floor. Repeat every cooldown, paying the spread each time.
    GOLD = {"trade_contract_size": 100.0, "ask": 4392.76, "bid": 4392.49,
            "volume_step": 0.01, "volume_min": 0.01, "volume_max": 50.0}
    BROKER_PER_LOT = 21951.0     # measured: mt5.order_calc_margin at 1.00 lot
    live = acct(equity=2858.43, margin=0.0, leverage=30)

    def survives(size: float) -> tuple:
        """Margin level the broker will really report for *size*, and the verdict."""
        used = size * BROKER_PER_LOT
        level = (live["equity"] / used * 100.0) if used else 0.0
        closed = rm.evaluate(acct(equity=live["equity"], margin=used, leverage=30),
                             [{"volume": size, "profit": 0.0}],
                             live["equity"])["force_close"]
        return level, closed

    sized = rm.lots_allowed(live, dict(GOLD, margin_per_lot=BROKER_PER_LOT), cfg)
    level, closed = survives(sized)
    check("a size taken from the broker's own margin figure survives the floor",
          sized > 0 and not closed,
          "%.2f lots -> margin level %.1f%% (floor %.0f%%)"
          % (sized, level, cfg["risk"]["min_margin_level_pct"]))
    check("...and it clears the floor by the sizing buffer, not by luck",
          level >= cfg["risk"]["min_margin_level_pct"] * SIZING_MARGIN_BUFFER,
          "%.1f%% vs %.1f%% required"
          % (level, cfg["risk"]["min_margin_level_pct"] * SIZING_MARGIN_BUFFER))

    # the regression itself: the leverage formula still mis-sizes, which is *why*
    # margin_per_lot has to win. If this ever stops force-closing, the broker and
    # the formula have converged and the priority order stopped mattering.
    guessed = rm.lots_allowed(live, dict(GOLD), cfg)
    _, guess_closed = survives(guessed)
    check("the leverage formula alone would still have been force-closed",
          guess_closed and guessed > sized,
          "formula %.2f lots vs broker-sized %.2f" % (guessed, sized))

    check("margin_per_lot is preferred over the leverage formula",
          rm.lots_allowed(live, dict(GOLD, margin_per_lot=BROKER_PER_LOT), cfg)
          < rm.lots_allowed(live, dict(GOLD), cfg),
          "broker figure must produce the smaller, survivable size")

    # the 0.0 / -1.0 contract, which a past bug collapsed
    check("no price and no margin_per_lot is unmeasurable (-1.0), not 'no room'",
          rm.lots_allowed(acct(equity=1000.0, margin=0.0, leverage=0),
                          {"volume_step": 0.01}) == -1.0,
          "%.2f" % rm.lots_allowed(acct(equity=1000.0, margin=0.0, leverage=0),
                                   {"volume_step": 0.01}))
    check("a fully committed account has no room (0.0), not unmeasurable",
          rm.lots_allowed(acct(equity=100.0, margin=9000.0, leverage=30),
                          dict(GOLD, margin_per_lot=BROKER_PER_LOT), cfg) == 0.0,
          "%.2f" % rm.lots_allowed(acct(equity=100.0, margin=9000.0, leverage=30),
                                   dict(GOLD, margin_per_lot=BROKER_PER_LOT), cfg))

    print("-" * 72)
    print("PASS %d   FAIL %d" % (passed, failed))
    raise SystemExit(1 if failed else 0)
