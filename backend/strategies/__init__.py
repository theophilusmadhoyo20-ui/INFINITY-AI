"""Strategy registry. Importing this package populates :data:`REGISTRY`."""

from __future__ import annotations

# the same ratio the config migration enforces, so the number the UI complains
# about and the number that triggers a rewrite can never drift apart
from backend.config import MIN_TARGET_OVER_SPREAD

from .base import (  # noqa: F401  (re-exported)
    REGISTRY,
    Ctx,
    Intent,
    Strategy,
    adx,
    atr,
    bollinger,
    ema,
    ema_series,
    register,
    sma,
    stdev,
    trend_direction,
)

# importing each module runs its @register decorator
from . import recovery_grid  # noqa: F401,E402
from . import ai_scalper  # noqa: F401,E402
from . import trend_ai  # noqa: F401,E402
from . import mean_reversion  # noqa: F401,E402

#: display order for the UI roster: class S first, then A, then B
ORDER = ["recovery_grid", "ai_scalper", "trend_ai", "mean_reversion"]

__all__ = [
    "REGISTRY",
    "ORDER",
    "Ctx",
    "Intent",
    "Strategy",
    "get_strategy",
    "build",
    "roster",
]


def get_strategy(key: str):
    """Return the strategy class for *key*, or None."""
    return REGISTRY.get(str(key or "").strip())


def build(key: str, params: dict = None, logger=None):
    """Instantiate a strategy, falling back to recovery_grid for an unknown key."""
    cls = get_strategy(key) or REGISTRY.get("recovery_grid")
    if cls is None:  # pragma: no cover - registry is never empty
        raise KeyError("no strategies registered")
    return cls(params or {}, logger)


def keys() -> list:
    """Registered keys in display order."""
    ordered = [k for k in ORDER if k in REGISTRY]
    ordered += [k for k in sorted(REGISTRY) if k not in ordered]
    return ordered


#: Closed baskets before a measured win rate is worth quoting back. Below this a
#: coin flip swings far enough to look like either a winner or a disaster: at 30
#: samples a true 50% strategy still shows anywhere from about 32% to 68%.
MIN_SAMPLE = 30

#: Which pair of parameters forms each strategy's bracket, plus the spread-cap key
#: when the bracket is cash per 0.01 lot and the two are therefore comparable. The
#: grid brackets its whole basket rather than a single trade, which is still a
#: reward/risk pair — and the fact that its risk leg is zero is the most important
#: thing about it, so it belongs here rather than being left out as "no bracket".
_BRACKETS = {
    "recovery_grid": ("basket_tp_pct_equity", "basket_sl_pct_equity", ""),
    "ai_scalper": ("tp_usd", "sl_usd", "max_spread_usd"),
    "trend_ai": ("tp_pct_equity", "sl_pct_equity", ""),
    "mean_reversion": ("tp_pct_equity", "sl_pct_equity", ""),
}


def _bracket_maths(key: str, params: dict):
    """The win rate a bracket needs to break even, from config alone.

    A fixed reward/risk bracket has exactly one break-even hit rate,
    ``risk / (risk + reward)``, and it can be read off the settings before the
    strategy has ever traded. That makes it the one honest thing this app can say
    about a configuration in advance — and it is how the shipped scalper was
    caught: it needed 69% of trades to win.

    ``cost_pct_of_target`` is the other half, and only means anything when target
    and spread cap are in the same units. The spread is paid on entry out of the
    same prize, so a target the spread eats most of is not a trade, it is a fee.

    A configured target with no stop behind it gets ``no_stop`` instead of a
    required win rate, because there isn't one: the arithmetic has nothing to
    divide by, and no hit rate is high enough to survive a loss of unbounded size.
    """
    names = _BRACKETS.get(key)
    if not names:
        return None
    try:
        reward = float(params.get(names[0]) or 0.0)
        risk = float(params.get(names[1]) or 0.0)
    except (TypeError, ValueError):
        return None
    if reward <= 0:
        return None
    if risk <= 0:
        return {"no_stop": True}
    out = {
        "required_win_pct": round(100.0 * risk / (risk + reward), 1),
        "reward_risk": round(reward / risk, 2),
    }
    if names[2]:
        try:
            cap = float(params.get(names[2]) or 0.0)
        except (TypeError, ValueError):
            cap = 0.0
        if cap > 0:
            out["cost_pct_of_target"] = round(100.0 * cap / reward, 1)
    return out


def _expectancy(key: str, params: dict, measured: dict = None):
    """What this configuration needs, what it has managed, and whether it pays.

    Two independent readings, kept separate on purpose. The arithmetic one needs
    no history and cannot be argued with. The measured one is the only evidence
    that the entry logic finds anything, and it is worthless until the sample is
    big enough — so below :data:`MIN_SAMPLE` closed baskets it is reported but not
    judged.
    """
    maths = _bracket_maths(key, params) or {}
    out = dict(maths)
    measured = dict(measured or {})
    closed = int(measured.get("baskets_closed") or 0)
    out["baskets_closed"] = closed
    out["win_pct"] = measured.get("win_pct")
    out["net_pl"] = measured.get("net_pl")

    # a measured bracket beats a configured one: it includes spread, swap and
    # every partial fill the broker actually gave
    avg_win = float(measured.get("avg_win") or 0.0)
    avg_loss = abs(float(measured.get("avg_loss") or 0.0))
    if avg_win > 0 and avg_loss > 0:
        out["required_win_pct"] = round(100.0 * avg_loss / (avg_loss + avg_win), 1)
        out["required_from"] = "trades"
    elif "required_win_pct" in out:
        out["required_from"] = "settings"

    need = out.get("required_win_pct")
    got = out.get("win_pct")
    cost = out.get("cost_pct_of_target")

    if cost is not None and cost >= 100.0 / MIN_TARGET_OVER_SPREAD:
        out["verdict"] = "cost-heavy"
        out["note"] = ("The spread is %.0f%% of the target, so most of the prize goes "
                       "on the entry. Widen the target or tighten the spread filter."
                       % cost)
    elif out.get("no_stop"):
        # not a criticism, a disclosure: this is how recovery grids work, and the
        # win rate they post is near 100% right up until the one basket that isn't
        out["verdict"] = "no-stop"
        out["note"] = ("No stop loss — it adds to a losing basket until price comes "
                       "back. Wins often, and one bad run costs more than many wins "
                       "made. %s"
                       % ("%d closed, %+.2f net so far." % (closed, float(out.get("net_pl") or 0.0))
                          if closed else "Nothing closed yet."))
    elif need is None:
        out["verdict"] = "unproven"
        out["note"] = "No target and stop configured, so there is no arithmetic to do."
    elif closed < MIN_SAMPLE:
        out["verdict"] = "unproven"
        out["note"] = ("Needs %.0f%% of trades to win. %d closed so far — too few to "
                       "tell whether it does." % (need, closed))
    elif got is not None and float(got) + 0.05 < need:
        out["verdict"] = "losing"
        out["note"] = ("Needs %.0f%% of trades to win and is managing %.0f%% over %d "
                       "closed. On these numbers it loses money."
                       % (need, float(got), closed))
    else:
        out["verdict"] = "working"
        out["note"] = ("Needs %.0f%% of trades to win and is managing %.0f%% over %d "
                       "closed." % (need, float(got or 0.0), closed))
    return out


def roster(config: dict = None, measured: dict = None) -> list:
    """Roster payload for the UI: one describe() dict per strategy.

    *measured* maps a strategy key to its own closed-basket record (see
    ``Store.strategy_stats``). It is optional so this module stays a pure
    function of config with no database of its own; the server passes it in.
    """
    config = config or {}
    measured = dict(measured or {})
    params_all = dict(config.get("strategies") or {})
    engine = dict(config.get("engine") or {})
    active = str(engine.get("active_strategy") or "")
    magic = engine.get("magic", 100100)
    out = []
    for key in keys():
        cls = REGISTRY[key]
        params = dict(params_all.get(key) or {})
        info = cls(params, None).describe()
        tiers = params.get("lot_tiers") or []
        try:
            lot_range = "%.2f - %.2f lots" % (float(tiers[0][1]), float(tiers[-1][1]))
        except (IndexError, TypeError, ValueError):
            lot_range = "%.2f lots" % float(params.get("base_lot") or 0.01)
        info["active"] = key == active
        info["equity_tag"] = "Engine %s · %s" % (magic, lot_range)
        info["params"] = params
        info["expectancy"] = _expectancy(key, params, measured.get(key))
        out.append(info)
    return out

