"""Strategy self-test — synthetic contexts, no broker.

    python -m backend.strategies
"""

from __future__ import annotations

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from backend.config import load_config  # noqa: E402
from backend.strategies import REGISTRY, Ctx, build, roster  # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASS, FAIL
    if condition:
        PASS += 1
        print("[ok  ] %s %s" % (label, detail))
    else:
        FAIL += 1
        print("[FAIL] %s %s" % (label, detail))


def candles(prices: list) -> list:
    now = int(time.time())
    return [
        {"time": now - (len(prices) - i) * 300, "open": p, "high": p + 0.4,
         "low": p - 0.4, "close": p}
        for i, p in enumerate(prices)
    ]


def ctx_for(strategy, equity=1000.0, price=2400.0, positions=None, bars=None, spread=0.20, now=None):
    positions = positions or []
    lots = sum(float(p["volume"]) for p in positions)
    profit = sum(float(p["profit"]) for p in positions)
    avg = (sum(float(p["price_open"]) * float(p["volume"]) for p in positions) / lots) if lots else 0.0
    return Ctx(
        account={"equity": equity, "balance": equity, "margin_level": 900.0, "currency": "USD"},
        symbol={"name": "XAUUSD", "bid": price, "ask": price + spread, "spread_usd": spread,
                "volume_min": 0.01, "volume_max": 100.0, "volume_step": 0.01},
        tick={"bid": price, "ask": price + spread, "spread_usd": spread, "time": int(time.time())},
        positions=positions,
        candles=bars if bars is not None else candles([2380 + i * 0.9 for i in range(80)]),
        basket={
            "open": bool(positions),
            "side": positions[0]["type"] if positions else "",
            "layers": len(positions),
            "lots": lots,
            "profit": profit,
            "avg_price": avg,
        },
        params=strategy.params,
        now=now if now is not None else time.time(),
    )


def pos(ticket, side, volume, price_open, profit, age=0):
    return {"ticket": ticket, "symbol": "XAUUSD", "type": side, "volume": volume,
            "price_open": price_open, "price_current": price_open, "profit": profit,
            "swap": 0.0, "magic": 100100, "time": int(time.time()) - age, "comment": ""}


def main() -> int:
    cfg = load_config()
    print("=" * 72)
    print("INFINITY.100 AI - strategy self-test")
    print("=" * 72)
    check("registry has 4 strategies", len(REGISTRY) == 4, sorted(REGISTRY))

    grid = build("recovery_grid", cfg["strategies"]["recovery_grid"])

    # 1. the equity lot ladder observed in the reference recording
    ladder = [(2.44, 0.01), (99.0, 0.01), (100.0, 0.02), (150.0, 0.02), (200.0, 0.05),
              (499.0, 0.05), (500.0, 0.10), (1400.0, 0.50), (19999.0, 0.50),
              (20000.0, 2.0), (150000.0, 3.0), (200000.0, 4.0), (329438.0, 4.0)]
    bad = [(e, grid.lot_for_equity(e), want) for e, want in ladder
           if abs(grid.lot_for_equity(e) - want) > 1e-9]
    check("lot ladder 0.01->4.00 across 13 equity points", not bad, str(bad))

    # 2. flat -> open, direction from momentum
    rising = candles([2380 + i * 0.9 for i in range(80)])
    intent = grid.decide(ctx_for(grid, equity=1400.0, bars=rising))
    check("flat + rising market -> open buy",
          intent.action == "open" and intent.side == "buy" and abs(intent.volume - 0.50) < 1e-9,
          "%s %s %.2f" % (intent.action, intent.side, intent.volume))

    falling = candles([2450 - i * 0.9 for i in range(80)])
    intent = grid.decide(ctx_for(grid, equity=250.0, bars=falling, price=2360.0))
    check("flat + falling market -> open sell",
          intent.action == "open" and intent.side == "sell" and abs(intent.volume - 0.05) < 1e-9,
          "%s %s %.2f" % (intent.action, intent.side, intent.volume))

    # 3. wide spread blocks entry
    intent = grid.decide(ctx_for(grid, equity=1400.0, bars=rising, spread=1.20))
    check("wide spread blocks entry", intent.action == "hold", intent.reason)

    # 4. adverse move of one grid step -> add_layer
    held = [pos(1, "buy", 0.50, 2400.0, -1.8)]
    intent = grid.decide(ctx_for(grid, equity=1400.0, price=2399.60, positions=held, bars=rising))
    check("price 0.40 against a buy basket -> add_layer",
          intent.action == "add_layer" and intent.side == "buy",
          "%s: %s" % (intent.action, intent.reason))

    # ...but not before the step is reached
    intent = grid.decide(ctx_for(grid, equity=1400.0, price=2399.90, positions=held, bars=rising))
    check("price 0.10 against -> hold", intent.action == "hold", intent.reason)

    # 5. basket profit at 1.5% of equity -> close every position
    winners = [pos(i, "sell", 2.00, 4394.558, 8.0, age=60 - i) for i in range(1, 9)]
    intent = grid.decide(ctx_for(grid, equity=4000.0, price=4390.0, positions=winners, bars=falling))
    check("8-layer basket at +64 vs 60 target -> close_basket",
          intent.action == "close_basket", "%s: %s" % (intent.action, intent.reason))

    # 6. no stop loss by default: a deep loss still holds
    losers = [pos(i, "buy", 0.10, 2400.0 - i * 0.35, -60.0, age=60 - i) for i in range(1, 6)]
    intent = grid.decide(ctx_for(grid, equity=1489.0, price=2396.0, positions=losers, bars=rising))
    check("deep drawdown with no SL -> never close_basket",
          intent.action in ("add_layer", "hold"), "%s: %s" % (intent.action, intent.reason))

    # 7. max_layers caps the ladder
    many = [pos(i, "buy", 0.10, 2400.0 - i * 0.35, -20.0, age=60 - i) for i in range(1, 10)]
    intent = grid.decide(ctx_for(grid, equity=1489.0, price=2390.0, positions=many, bars=rising))
    check("9 layers with max_layers=9 -> hold", intent.action == "hold", intent.reason)

    # 8. cooldown after a close
    grid.on_basket_closed(time.time(), 12.0)
    intent = grid.decide(ctx_for(grid, equity=1400.0, bars=rising))
    check("cooldown blocks re-entry", intent.action == "hold", intent.reason)
    grid.reset()

    # 9. every other strategy decides without raising, on rich and empty context
    for key in ("ai_scalper", "trend_ai", "mean_reversion"):
        s = build(key, cfg["strategies"][key])
        try:
            rich = s.decide(ctx_for(s, equity=5000.0, bars=candles(
                [2400 + (12.0 if i % 2 else -12.0) + i * 0.2 for i in range(120)])))
            empty = s.decide(Ctx(params=s.params, now=time.time()))
            check("%s decides on rich + empty ctx" % key,
                  rich.action in ("open", "hold", "close_basket") and empty.action == "hold",
                  "%s / %s" % (rich.action, empty.action))
        except Exception as exc:  # noqa: BLE001
            check("%s decides without raising" % key, False, repr(exc))

    # 10. roster payload for the UI
    cards = roster(cfg)
    check("roster has 4 cards with stats and level",
          len(cards) == 4 and all(c["level"] > 0 and len(c["stats"]) == 4 for c in cards),
          ", ".join("%s L%d %s" % (c["title"], c["level"], c["klass"]) for c in cards))

    print("-" * 72)
    print("PASS %d   FAIL %d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
