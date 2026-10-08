"""Dry run — the real app, driven by the repo's simulated broker.

    python tools/dry_run.py [--port 8130] [--balance 1400] [--open]

Everything here is the shipping code: the real TradingEngine, RiskManager,
AIPersona, Api and Server, serving the real frontend. The only substitution is
the broker — instead of the MetaTrader 5 bridge it uses FakeBridge from
backend/engine_test.py, with a seeded random walk on top so prices move and the
engine actually opens, layers and closes baskets.

Nothing here touches a broker, config.json or data/: the store and the config
write path are both temporary files. Use it to see and exercise the interface
without MetaTrader 5 running. It is not a backtest and it is not a forecast —
the prices are synthetic, so the equity curve means nothing beyond "the wiring
works".
"""

from __future__ import annotations

import argparse
import logging
import math
import os
import random
import sys
import tempfile
import threading
import time
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_config  # noqa: E402
from backend.engine import TradingEngine  # noqa: E402
from backend.engine_test import FakeBridge  # noqa: E402
from backend.persona import AIPersona  # noqa: E402
from backend.risk import RiskManager  # noqa: E402
from backend.server import Api, Server  # noqa: E402
from backend.store import Store  # noqa: E402

BANNER = "=" * 72

#: minutes per MetaTrader timeframe code, for aggregating the base one-minute bars
_TF_MINUTES = {"M1": 1, "M2": 2, "M3": 3, "M4": 4, "M5": 5, "M6": 6, "M10": 10,
               "M12": 12, "M15": 15, "M20": 20, "M30": 30, "H1": 60, "H2": 120,
               "H3": 180, "H4": 240, "H6": 360, "H8": 480, "H12": 720, "D1": 1440}


class WalkingBridge(FakeBridge):
    """FakeBridge with a price that moves like a market and a real candle history.

    The walk this replaces got two things wrong, and between them they flattered
    one strategy family while punishing the other — which is the kind of
    measurement error that makes a config look hopeless for no reason at all:

    * The trend reversed on a fixed 90-second timer. Any entry signal with lag is
      then systematically late, and a mean-reverting grid is systematically right,
      because price always came back inside 90 seconds.
    * :meth:`FakeBridge.candles` builds history as a straight line drawn backwards
      from the *current* price using the *current* trend flag. An EMA cross over
      that is not detecting anything — it is reading ``self.trend`` directly, the
      fast/slow separation is a constant (0.9 per bar × 6.5 bars of EMA lag ≈
      5.85), and a separation gate sized in spreads can never block. A win rate
      measured against that says nothing about a strategy's entries.

    So here regimes switch on a random schedule with random strength, including
    genuinely directionless ones; volatility is scaled to gold's real per-minute
    movement; the spread spikes now and then so spread filters are actually
    exercised; and candles are aggregated from the ticks that really happened.

    It is still synthetic. It can show that the wiring works and that a bracket's
    geometry behaves as intended. It cannot forecast anything, and a profit here
    is not evidence of a profit anywhere else.
    """

    def __init__(self, balance=1400.0, price=2400.0, seed=100100,
                 bar_s=60.0, tick_s=0.25, sigma_bar=0.38, history=260,
                 swap_per_lot_bar=0.10):
        super().__init__(balance=balance, price=price)
        self._rng = random.Random(seed)
        self._tick_s = max(0.01, float(tick_s))
        self._ticks_per_bar = max(1, int(round(max(0.5, float(bar_s)) / self._tick_s)))
        self._swap_per_lot_bar = max(0.0, float(swap_per_lot_bar))
        # gold moves about 0.38 USD in a minute; a tick is one bar's worth of that
        # divided by root-N, so the bar keeps the intended standard deviation
        self._sigma_bar = max(0.0, float(sigma_bar))
        self._sigma_tick = self._sigma_bar / math.sqrt(self._ticks_per_bar)

        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._walk, name="demo-prices", daemon=True)

        self._bars = []
        self._bar = None
        self._tick_in_bar = 0
        self._regime_ticks = 0
        self._drift_tick = 0.0
        self._spike_ticks = 0
        self.spread = round(self._rng.uniform(0.16, 0.30), 2)
        # bar stamps advance a minute at a time whatever --bar-s does, so the
        # history reads like minutes even when the walk is compressed
        self._bar_clock = time.time() - max(0, int(history)) * 60.0
        self._seed_history(int(history))

    # -- lifecycle -------------------------------------------------------- #

    def begin(self):
        self._thread.start()

    def shutdown(self):
        self._stop.set()

    def _walk(self):
        while not self._stop.wait(self._tick_s):
            with self._lock:
                self._step()

    # -- the process ------------------------------------------------------ #

    def _step(self) -> None:
        """One tick. Caller holds the lock."""
        if self._regime_ticks <= 0:
            self._new_regime()
        self._regime_ticks -= 1
        self.price = max(1.0, self.price + self._drift_tick
                         + self._rng.gauss(0.0, self._sigma_tick))
        self._step_spread()
        self._accrue_swap()
        self._fold_into_bar()

    def _accrue_swap(self) -> None:
        """Charge carry on every open position.

        Gold is negative-carry on both sides at most brokers. FakeBridge left swap
        pinned at 0.0, which meant nothing downstream ever exercised it — and
        Engine._basket() sums profit + swap while a leg row was showing profit
        alone, so the legs could disagree with the basket total and no harness
        would notice.

        The default rate is about ten times a real broker's (a real one charges
        roughly a cent per lot per minute on gold). That is deliberate: at a
        realistic rate a 0.1-lot leg accrues 0.002 per bar, which renders as
        -0.00 in a two-decimal leg cell and moves the basket total by less than
        the rounding step — so the reconciliation this exists to exercise would
        pass whether the sum were right or wrong. Ten times realistic puts a
        visible number in the cell within a couple of bars while still being
        small against the take-profit target. Pass --swap 0.01 for realistic
        carry when the run is about P/L rather than about wiring.
        """
        per_lot_tick = self._swap_per_lot_bar / self._ticks_per_bar
        for p in self.positions_list:
            # accumulate at full precision. Rounding the running total each tick
            # quantised an 8e-6 increment to 0.0000 and the charge never grew --
            # swap stayed at -0.0 for the life of every basket.
            p["swap"] = (float(p.get("swap") or 0.0)
                         - per_lot_tick * float(p.get("volume") or 0.0))

    def _new_regime(self) -> None:
        """Start a fresh leg: sometimes up, sometimes down, often neither.

        Length is exponential with a mean of eight bars, so nothing in this market
        lines up with a strategy's timers the way a fixed 90-second flip did. Two
        legs in five have no drift at all — a strategy that only makes money in a
        trend should be allowed to sit in a range and show that.
        """
        bars = max(1.0, self._rng.expovariate(1.0 / 8.0))
        self._regime_ticks = max(1, int(bars * self._ticks_per_bar))
        if self._rng.random() < 0.40:
            direction, drift_bar = 0, 0.0
        else:
            direction = 1 if self._rng.random() < 0.5 else -1
            # 0.15-0.65 sigma per bar: over an eight-bar leg that is 1.2-5.2 sigma
            # of drift against 2.8 sigma of noise, so the trend is real but not
            # a ramp anyone could trade blindfolded
            drift_bar = direction * self._rng.uniform(0.15, 0.65) * self._sigma_bar
        # the parent's fallback candles() divides by trend, so keep it non-zero
        self.trend = direction or 1
        self._drift_tick = drift_bar / self._ticks_per_bar

    def _step_spread(self) -> None:
        """Normally tight, occasionally awful. A spread filter that is never
        tested is not a filter, it is a comment."""
        if self._spike_ticks > 0:
            self._spike_ticks -= 1
            return
        if self._rng.random() < 0.004 / self._ticks_per_bar:
            self._spike_ticks = max(2, int(self._ticks_per_bar * self._rng.uniform(0.2, 1.5)))
            self.spread = round(self._rng.uniform(0.45, 1.10), 2)
            return
        self.spread = round(self._rng.uniform(0.16, 0.30), 2)

    def _fold_into_bar(self) -> None:
        price = self.price
        spread_points = int(round(self.spread * 100))
        if self._bar is None:
            self._bar = {"time": int(self._bar_clock), "open": price, "high": price,
                         "low": price, "close": price, "tick_volume": 1,
                         "spread": spread_points, "real_volume": 0}
            self._tick_in_bar = 1
            return
        bar = self._bar
        bar["high"] = max(bar["high"], price)
        bar["low"] = min(bar["low"], price)
        bar["close"] = price
        bar["tick_volume"] += 1
        bar["spread"] = max(bar["spread"], spread_points)
        self._tick_in_bar += 1
        if self._tick_in_bar >= self._ticks_per_bar:
            self._bars.append(bar)
            if len(self._bars) > 6000:
                del self._bars[:len(self._bars) - 6000]
            self._bar = None
            self._tick_in_bar = 0
            self._bar_clock += 60.0

    def _seed_history(self, bars: int) -> None:
        """Run the process before anyone is watching.

        Without this, an M1 EMA21 needs twenty-one minutes of wall clock before
        the scalper is allowed its first opinion, and the run says nothing.
        """
        for _ in range(max(0, bars) * self._ticks_per_bar):
            self._step()

    # -- what the engine reads -------------------------------------------- #

    def candles(self, timeframe="M5", count=200):
        minutes = _TF_MINUTES.get(str(timeframe or "M5").upper().strip(), 5)
        with self._lock:
            bars = list(self._bars)
        if minutes > 1:
            grouped, out = [], []
            # group forward from a boundary so each bucket holds `minutes` bars
            for start in range(0, len(bars) - len(bars) % minutes, minutes):
                grouped.append(bars[start:start + minutes])
            for chunk in grouped:
                out.append({
                    "time": chunk[0]["time"],
                    "open": chunk[0]["open"],
                    "high": max(c["high"] for c in chunk),
                    "low": min(c["low"] for c in chunk),
                    "close": chunk[-1]["close"],
                    "tick_volume": sum(c["tick_volume"] for c in chunk),
                    "spread": max(c["spread"] for c in chunk),
                    "real_volume": 0,
                })
            bars = out
        bars = bars[-int(count):] if count else bars
        if len(bars) < 2:
            # not enough of the real walk yet; the parent's straight line is a
            # poor substitute but it is better than starving the strategy
            return super().candles(timeframe, count)
        return bars


def main() -> int:
    ap = argparse.ArgumentParser(description="Run INFINITY.100 AI against a simulated broker.")
    ap.add_argument("--port", type=int, default=8130)
    ap.add_argument("--balance", type=float, default=1400.0)
    ap.add_argument("--price", type=float, default=2400.0)
    ap.add_argument("--seed", type=int, default=100100,
                    help="price-walk seed; same seed tells the same story twice")
    ap.add_argument("--bar-s", type=float, default=60.0, dest="bar_s",
                    help="wall-clock seconds per one-minute bar. Lower it to compress "
                         "the price process (--bar-s 6 gives ten bars a minute) when "
                         "you want many baskets out of a short run")
    ap.add_argument("--tick-s", type=float, default=0.25, dest="tick_s",
                    help="wall-clock seconds between price ticks")
    ap.add_argument("--sigma", type=float, default=0.38,
                    help="standard deviation of one bar's move, in price units "
                         "(0.38 is about right for gold on M1)")
    ap.add_argument("--history", type=int, default=260,
                    help="bars of walk to generate before serving, so strategies "
                         "have real history from the first tick")
    ap.add_argument("--swap", type=float, default=0.10,
                    help="carry charged per lot per one-minute bar. The default is "
                         "about ten times a real broker's gold swap, so the charge "
                         "is visible at two decimals within a couple of bars; pass "
                         "0.01 for realistic carry, or 0 for none")
    ap.add_argument("--open", action="store_true", help="open a browser at the URL")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING,
                        format="%(asctime)s  %(levelname)-7s %(message)s")

    config = load_config()
    config["account"]["password"] = ""          # never in play — there is no broker here
    config["account"]["allow_live_real"] = False

    stamp = int(time.time() * 1000)
    tmp_db = os.path.join(tempfile.gettempdir(), "infinity_dryrun_%d.db" % stamp)
    tmp_cfg = os.path.join(tempfile.gettempdir(), "infinity_dryrun_%d.json" % stamp)

    store = Store(tmp_db)
    bridge = WalkingBridge(balance=args.balance, price=args.price, seed=args.seed,
                           bar_s=args.bar_s, tick_s=args.tick_s, sigma_bar=args.sigma,
                           history=args.history, swap_per_lot_bar=args.swap)
    risk = RiskManager(config)
    persona = AIPersona(config, store)
    engine = TradingEngine(config, bridge, risk, store, persona)
    api = Api(config, bridge, engine, risk, store, persona, config_path=tmp_cfg)
    server = Server(api, port=args.port)

    url = server.start()
    bridge.begin()
    engine.begin()
    engine.start()  # automation on, so the roster and basket panels have something to show

    print(BANNER)
    print("INFINITY.100 AI - dry run (simulated broker, no MetaTrader 5)")
    print(BANNER)
    print("  %s" % url)
    print("  balance   %.2f %s" % (args.balance, config.get("account", {}).get("currency", "USD")))
    print("  store     %s (temporary)" % tmp_db)
    print("  prices    synthetic seeded walk (seed %d, sigma %.2f/bar, %.0fs per bar,"
          " %d bars pre-seeded) - not market data"
          % (args.seed, args.sigma, args.bar_s, args.history))
    print("  swap      %.3f per lot per bar%s"
          % (args.swap, " (about 10x a real broker's - see --swap)"
             if args.swap > 0.02 else ""))
    print("  Ctrl-C to stop.")
    print(BANNER)

    if args.open:
        webbrowser.open(url)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nstopping...")
    finally:
        bridge.shutdown()
        server.stop()
        engine.shutdown()
        store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(tmp_db + suffix)
            except OSError:
                pass
        try:
            os.remove(tmp_cfg)
        except OSError:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
