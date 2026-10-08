"""Measure AI SCALPER bracket geometry against the seeded walk.

    python tools/scalper_grid.py [--ticks 6000] [--seeds 3] [--stack N]

Drives the *real* TradingEngine, RiskManager and store with
:class:`tools.dry_run.WalkingBridge` in place of MetaTrader 5, one engine tick
per four price ticks — the same 1s cadence ``engine.poll_ms`` uses live — and
reports what each candidate bracket did.

Two overrides are applied to every arm so the measurement is of the bracket and
nothing else, and they are printed in the banner so nobody mistakes them for
proposed settings:

* ``cooldown_s = 0``. Cooldowns are wall-clock (``time.time()``), and a headless
  loop burns 6000 ticks in seconds, so the real 45s gate would never elapse and
  the sample would be one trade deep. Cooldown changes how *often* the strategy
  trades, not what a single trade is worth.
* ``max_daily_loss_pct = 90``. The live 20% cap halts trading partway through,
  which is exactly what it is for, but it truncates every arm at the same -$280
  and hides the per-trade difference underneath.

What this can and cannot show: a driftless random walk pays the spread on every
round trip no matter where the brackets sit, so expectancy here is bounded above
by whatever the walk's regimes make available. Read it as a comparison between
arms on identical price paths, and as a check that a bracket is reachable at all.
It is not a forecast, and a profit here is not evidence of a profit anywhere.
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_config  # noqa: E402
from backend.engine import TradingEngine  # noqa: E402
from backend.persona import AIPersona  # noqa: E402
from backend.risk import RiskManager  # noqa: E402
from backend.store import Store  # noqa: E402
from tools.dry_run import WalkingBridge  # noqa: E402

STEPS_PER_TICK = 4  # 4 price ticks of 0.25s == the 1s live poll interval


def run_one(params, seed, ticks, balance=1400.0):
    cfg = load_config()
    cfg["account"]["password"] = ""
    cfg["account"]["allow_live_real"] = False
    cfg["account"]["balance"] = balance
    cfg["engine"]["active_strategy"] = "ai_scalper"
    cfg["risk"]["max_daily_loss_pct"] = 90.0
    cfg["strategies"]["ai_scalper"].update(params)
    cfg["strategies"]["ai_scalper"]["cooldown_s"] = 0

    db = os.path.join(tempfile.gettempdir(), "grid_%d_%d.db" % (seed, int(time.time() * 1e6)))
    store = Store(db)
    bridge = WalkingBridge(balance=balance, price=2400.0, seed=seed, history=300)
    engine = TradingEngine(cfg, bridge, RiskManager(cfg), store, AIPersona(cfg, store))
    engine.manual = True
    engine.begin()
    engine.start()
    peak_positions = 0
    for _ in range(ticks):
        for _ in range(STEPS_PER_TICK):
            bridge._step()
        engine._tick()
        peak_positions = max(peak_positions, len(bridge.positions(engine.magic)))
    rows = [r for r in store.recent_baskets(limit=200000) if r.get("closed_ts")]
    store.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(db + suffix)
        except OSError:
            pass
    return rows, bridge.balance, peak_positions


def summarise(label, params, seeds, ticks):
    wins, losses, n, net, layers, peak = [], [], 0, 0.0, [], 0
    for seed in seeds:
        rows, bal, pk = run_one(params, seed, ticks)
        peak = max(peak, pk)
        n += len(rows)
        net += sum(r["profit"] for r in rows)
        wins += [r["profit"] for r in rows if r["profit"] > 0]
        losses += [r["profit"] for r in rows if r["profit"] <= 0]
        layers += [int(r.get("layers") or 1) for r in rows]
        print("    seed %-7d %4d baskets  net %+9.2f  win %5.1f%%  end %.2f"
              % (seed, len(rows), sum(r["profit"] for r in rows),
                 100.0 * sum(1 for r in rows if r["profit"] > 0) / max(1, len(rows)), bal))
    aw = sum(wins) / len(wins) if wins else 0.0
    al = sum(losses) / len(losses) if losses else 0.0
    need = 100.0 * abs(al) / (abs(al) + aw) if (aw and al) else float("nan")
    got = 100.0 * len(wins) / n if n else 0.0
    print("  %-26s %4d baskets  net %+9.2f  %+.4f/basket" % (label, n, net, net / n if n else 0))
    print("    avg win %+.3f  avg loss %+.3f  need %.1f%%  got %.1f%%  edge %+.1f pts"
          % (aw, al, need, got, got - need))
    print("    layers avg %.2f max %d   peak concurrent positions %d\n"
          % (sum(layers) / len(layers) if layers else 0, max(layers) if layers else 0, peak))
    return {"label": label, "n": n, "net": net, "per": net / n if n else 0.0,
            "need": need, "got": got}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ticks", type=int, default=6000)
    ap.add_argument("--seeds", type=int, default=3)
    ap.add_argument("--stack", type=int, default=1,
                    help="max_positions for every arm (1 disables stacking)")
    ap.add_argument("--arms", default="ABCDE",
                    help="which candidate letters to run, e.g. ACDE")
    args = ap.parse_args()
    logging.basicConfig(level=logging.CRITICAL)

    seeds = [100100, 220220, 330330, 440440, 550550][:max(1, args.seeds)]
    print("=" * 72)
    print("AI SCALPER bracket grid - synthetic walk, not a forecast")
    print("  %d engine ticks/arm (%d simulated minutes), seeds %s, max_positions %d"
          % (args.ticks, args.ticks * STEPS_PER_TICK // 240, seeds, args.stack))
    print("  overrides for measurement only: cooldown_s=0, max_daily_loss_pct=90")
    print("=" * 72)

    # tp/sl are per 0.01 lot, so on gold they read directly as price distance
    candidates = [
        # Z reproduces the shipped-until-now behaviour: tp_usd/sl_usd were read as
        # a flat amount of currency, so at the 0.05-lot tier $0.40/$0.80 bought a
        # target 0.08 of price wide and a stop 0.16 wide. Expressed per 0.01 lot
        # that is 0.08/0.16 — the arm to beat, and the reason for the rewrite.
        ("Z tp0.08/sl0.16 (shipped)", {"tp_usd": 0.08, "sl_usd": 0.16}),
        ("A tp0.40/sl0.80 (was)", {"tp_usd": 0.40, "sl_usd": 0.80}),
        ("B tp0.80/sl0.80", {"tp_usd": 0.80, "sl_usd": 0.80}),
        ("C tp1.20/sl0.80", {"tp_usd": 1.20, "sl_usd": 0.80}),
        ("D tp1.60/sl1.00", {"tp_usd": 1.60, "sl_usd": 1.00}),
        ("E tp2.40/sl1.20", {"tp_usd": 2.40, "sl_usd": 1.20}),
    ]
    out = []
    picked = set(args.arms.upper())
    for label, params in candidates:
        if label[0] not in picked:
            continue
        params = dict(params)
        params["max_spread_usd"] = 0.35
        params["max_positions"] = args.stack
        params["min_spacing_usd"] = 0.60
        out.append(summarise(label, params, seeds, args.ticks))

    print("=" * 72)
    out.sort(key=lambda r: -r["per"])
    for r in out:
        print("  %-26s %+.4f/basket  net %+9.2f  need %5.1f%% got %5.1f%%"
              % (r["label"], r["per"], r["net"], r["need"], r["got"]))
    print("=" * 72)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
