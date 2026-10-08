"""Engine self-test — a simulated broker, no MetaTrader 5 required.

    python -m backend.engine_test

Drives TradingEngine._tick() by hand so the whole video sequence is
deterministic: open -> layer -> layer -> basket close at target -> cooldown,
then a risk override, then a disconnect.
"""

from __future__ import annotations

import logging
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_config  # noqa: E402
from backend.engine import RECONNECT_EVERY, TradingEngine  # noqa: E402
from backend.mt5_bridge import BridgeReadError  # noqa: E402
from backend.persona import AIPersona  # noqa: E402
from backend.risk import RiskManager  # noqa: E402
from backend.store import Store  # noqa: E402
from backend.strategies.ai_scalper import AIScalper  # noqa: E402
from backend.strategies.base import Ctx  # noqa: E402

PASS = FAIL = 0
CONTRACT = 100.0  # 1 lot of gold = 100 oz


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("[ok  ] %s %s" % (label, detail))
    else:
        FAIL += 1
        print("[FAIL] %s %s" % (label, detail))


class FakeBridge:
    """Just enough MT5 to exercise the engine: fills, floats, closes, balances."""

    def __init__(self, balance=1400.0, price=2400.0, spread=0.35):
        self.symbol_name = "XAUUSDm"
        self.balance = balance
        self.price = price
        self.spread = spread
        self.positions_list = []
        self.connected = True
        self.disconnects = 0
        self.last_stage = "ok"
        self.last_error = ""
        self._blocked = False
        self._block_reason = ""
        self._ticket = 5000
        self.orders = []
        #: (sl_distance, tp_distance) per fill, so a test can assert the bracket
        #: actually reached the broker rather than only that a trade happened.
        self.brackets = []
        #: (ticket, sl, tp) per accepted bracket move, so a test can assert the
        #: broker took the new level rather than only that the strategy asked
        self.stop_moves = []
        self.closes = 0
        self.margin_level_override = None
        self.trend = 1  # +1 rising candles, -1 falling
        self.reject_next = False

    # -- plumbing the engine calls -- #
    def is_connected(self):
        return self.connected

    def blocked(self):
        # same 2-tuple the real MT5Bridge returns — a fake that returns a bare bool
        # hides the fact that callers must unpack rather than truth-test
        return self._blocked, self._block_reason

    def connect(self):
        self.connected = True
        return {"ok": True, "account": {"login": 12345, "server": "Fake-Demo",
                                        "trade_mode": "DEMO"}}

    def disconnect(self):
        self.connected = False
        self.disconnects += 1

    def _floating(self):
        total = 0.0
        for p in self.positions_list:
            if p["type"] == "buy":
                p["profit"] = (self.bid() - p["price_open"]) * p["volume"] * CONTRACT
            else:
                p["profit"] = (p["price_open"] - self.ask()) * p["volume"] * CONTRACT
            p["price_current"] = self.bid()
            # profit + swap, the way MT5 reports floating P/L and the way
            # Engine._basket() sums it. Identical while swap is 0.0, which it is
            # for every test here; it matters for WalkingBridge, which accrues.
            total += p["profit"] + float(p.get("swap") or 0.0)
        return total

    def bid(self):
        return round(self.price, 2)

    def ask(self):
        return round(self.price + self.spread, 2)

    def account(self):
        floating = self._floating()
        lots = sum(p["volume"] for p in self.positions_list)
        margin = lots * self.ask() * CONTRACT / 500.0
        equity = self.balance + floating
        if self.margin_level_override is not None:
            level = self.margin_level_override
        else:
            level = (equity / margin * 100.0) if margin > 0 else 0.0
        return {"login": 12345, "server": "Fake-Demo", "currency": "USD", "name": "Test",
                "balance": round(self.balance, 2), "equity": round(equity, 2),
                "margin": round(margin, 2), "margin_free": round(equity - margin, 2),
                "margin_level": round(level, 2), "leverage": 500, "trade_allowed": True,
                "is_real": False, "trade_mode": "DEMO", "trade_mode_code": 0,
                "so_call": 100.0, "so_so": 50.0, "profit": round(floating, 2)}

    def symbol_info(self):
        return {"name": self.symbol_name, "bid": self.bid(), "ask": self.ask(),
                "spread_usd": self.spread, "digits": 2, "volume_min": 0.01,
                "volume_max": 100.0, "volume_step": 0.01, "trade_contract_size": CONTRACT,
                "trade_allowed": True}

    def tick(self):
        return {"bid": self.bid(), "ask": self.ask(), "spread_usd": self.spread,
                "time": int(time.time())}

    def positions(self, magic=None):
        self._floating()
        return [dict(p) for p in self.positions_list]

    def candles(self, timeframe="M5", count=200):
        now = int(time.time())
        base = self.price - self.trend * 0.9 * count
        out = []
        for i in range(count):
            p = base + self.trend * 0.9 * i
            out.append({"time": now - (count - i) * 300, "open": p, "high": p + 0.4,
                        "low": p - 0.4, "close": p, "tick_volume": 100, "spread": 35,
                        "real_volume": 0})
        return out

    def market_order(self, side, volume, comment="", sl_distance=0.0, tp_distance=0.0):
        if self.reject_next:
            self.reject_next = False
            return {"ok": False, "message": "No prices (simulated rejection)", "retcode": 10021}
        self._ticket += 1
        entry = self.ask() if side == "buy" else self.bid()
        # A real broker holds sl/tp on the position, so the fake records them the
        # same way. Keeping the signature in step with the bridge is the point: when
        # market_order grew the bracket arguments and this did not, every order in
        # both suites failed with a TypeError the engine swallowed, and the tests
        # reported "no trade" for a reason that had nothing to do with trading.
        sl = tp = 0.0
        if sl_distance > 0:
            sl = round(entry - sl_distance if side == "buy" else entry + sl_distance, 2)
        if tp_distance > 0:
            tp = round(entry + tp_distance if side == "buy" else entry - tp_distance, 2)
        self.positions_list.append({
            "ticket": self._ticket, "symbol": self.symbol_name, "type": side,
            "volume": round(volume, 2), "price_open": entry, "price_current": entry,
            "profit": 0.0, "swap": 0.0, "magic": 100100, "time": int(time.time() * 1000) / 1000.0,
            "sl": sl, "tp": tp,
            "comment": comment})
        self.orders.append((side, round(volume, 2), entry))
        self.brackets.append((sl_distance, tp_distance))
        return {"ok": True, "tickets": [self._ticket], "volume": round(volume, 2),
                "price": entry, "message": "filled"}

    def modify_stops(self, ticket, sl=0.0, tp=0.0):
        """Move a broker-held bracket, the way MT5Bridge.modify_stops does.

        Four details of the real one are reproduced, because each of them can hide
        a defect in the caller:

        * it writes to the broker's own record — ``positions()`` hands out copies,
          so a fake that mutated those would let a test pass against a stop the
          broker never actually took;
        * ``0.0`` on a side means *leave that side alone*, not *remove it*. MT5
          reads a zero in an SLTP request as a removal, so a strategy that sends
          ``tp=0.0`` to move only the stop must not lose its target;
        * a level through the market is clamped, because that is what the terminal
          does with one;
        * a request that lands on the level already set comes back ``ok=False``
          with ``retcode 0`` — the "already at that level" answer the engine
          deliberately does not narrate, since it is the normal reply on most of
          the ticks in a position's life.
        """
        held = next((p for p in self.positions_list if p["ticket"] == int(ticket)), None)
        if held is None:
            return {"ok": False, "retcode": -1,
                    "comment": "position %s is not open" % ticket}
        was_sl, was_tp = float(held.get("sl") or 0.0), float(held.get("tp") or 0.0)
        new_sl = float(sl) or was_sl
        new_tp = float(tp) or was_tp
        if new_sl > 0:
            new_sl = min(new_sl, self.bid()) if held["type"] == "buy" \
                else max(new_sl, self.ask())
        new_sl, new_tp = round(new_sl, 2), round(new_tp, 2)
        if abs(new_sl - was_sl) < 0.005 and abs(new_tp - was_tp) < 0.005:
            return {"ok": False, "retcode": 0, "comment": "already at that level",
                    "sl": new_sl, "tp": new_tp}
        held["sl"], held["tp"] = new_sl, new_tp
        self.stop_moves.append((int(ticket), new_sl, new_tp))
        return {"ok": True, "retcode": 0, "comment": "modified",
                "sl": new_sl, "tp": new_tp}

    def close_position(self, ticket):
        for p in list(self.positions_list):
            if p["ticket"] == ticket:
                self.balance += p["profit"]
                self.positions_list.remove(p)
                return {"ok": True}
        return {"ok": False, "message": "not found"}

    def close_all(self, magic=None):
        self._floating()
        # go through positions() exactly as MT5Bridge.close_all does, and answer a
        # read failure the same way it does, so that path is reachable from a test.
        # Reading positions_list directly is what made the "already flat" bug
        # invisible: nothing in the fake could ever fail to look.
        try:
            open_now = self.positions(magic=magic)
        except BridgeReadError as exc:
            return {"ok": False, "retcode": -1, "comment": "could not read positions",
                    "tickets": [], "detail": str(exc), "profit": 0.0}
        tickets = [p["ticket"] for p in open_now]
        self.balance += sum(p["profit"] for p in open_now)
        self.positions_list = [p for p in self.positions_list if p["ticket"] not in tickets]
        self.closes += 1
        # CONTRACT.md: {ok, retcode, comment, tickets, detail}. This fake used to
        # answer {closed, message} instead, and because the engine read those same
        # invented keys the tests passed while the real bridge silently returned a
        # successful flatten as "Nothing was open."
        return {"ok": True, "retcode": 0, "tickets": tickets,
                "comment": "closed %d of %d positions" % (len(tickets), len(tickets)),
                "detail": "all positions closed", "profit": 0.0}


def build_engine(balance=1400.0, price=2400.0):
    cfg = load_config()
    # load_config() reads the real config.json on this machine, so this suite would
    # otherwise inherit whatever strategy the operator last selected in the UI — and
    # since POST /api/engine/strategy now persists that choice, a click in the app
    # silently rewrites what these tests are testing. The sections below assert
    # RECOVERY GRID's behaviour by name (0.50 at the 14000 ladder tier, a layer per
    # 0.40 adverse, a basket money target), so pin it rather than letting six checks
    # fail for a reason that has nothing to do with the code.
    cfg["engine"]["active_strategy"] = "recovery_grid"
    tmp = os.path.join(tempfile.gettempdir(), "infinity_engine_%d.db" % int(time.time() * 1000))
    store = Store(tmp)
    bridge = FakeBridge(balance=balance, price=price)
    engine = TradingEngine(cfg, bridge, RiskManager(cfg), store,
                           AIPersona(cfg, store), logging.getLogger("test.engine"))
    engine.manual = True  # we drive _tick() ourselves, no background thread
    return cfg, engine, bridge, store, tmp


def cleanup(store, tmp):
    store.close()
    for suffix in ("", "-wal", "-shm"):
        try:
            os.remove(tmp + suffix)
        except OSError:
            pass


def main() -> int:
    logging.basicConfig(level=logging.CRITICAL)
    print("=" * 72)
    print("INFINITY.100 AI - engine self-test (simulated broker)")
    print("=" * 72)

    cfg, engine, bridge, store, tmp = build_engine(balance=14000.0)

    # --- 1. status before anything happens is contract-shaped ---------- #
    snap = engine.status()
    need = {"ts", "connected", "engine", "account", "symbol", "basket", "risk", "stats",
            "equity_curve", "notice"}
    check("status() has every contract key before the first tick", need.issubset(snap),
          str(sorted(need - set(snap))))
    check("empty status is not running and not connected",
          not snap["engine"]["running"] and not snap["connected"])

    # --- 2. idle tick: live numbers, no trading ------------------------ #
    engine._tick()
    snap = engine.status()
    check("idle tick publishes live account numbers",
          snap["connected"] and abs(snap["account"]["equity"] - 14000.0) < 0.01,
          "equity=%.2f" % snap["account"]["equity"])
    check("idle tick opens nothing", not bridge.orders and snap["engine"]["last_intent"] == "idle",
          snap["engine"]["last_reason"])

    # --- 3. deploy, rising market -> buy at the 1400 ladder tier ------- #
    result = engine.start()
    check("start() succeeds when connected", result["ok"], result["message"])
    bridge.trend = 1
    engine._tick()
    check("rising market opens BUY 0.50 (ladder tier for this equity)",
          len(bridge.orders) == 1 and bridge.orders[0][0] == "buy"
          and abs(bridge.orders[0][1] - 0.50) < 1e-9, str(bridge.orders))
    snap = engine.status()
    check("basket appears in the snapshot with a TP target",
          snap["basket"]["open"] and snap["basket"]["layers"] == 1
          and snap["basket"]["tp_target"] > 0,
          "tp_target=%.2f" % snap["basket"]["tp_target"])
    check("a store basket row was opened", store.open_basket_id() > 0)
    check("the fill was persisted", len(store.fills_for(store.open_basket_id())) == 1)

    # --- 4. price falls one grid step -> layer 2 ----------------------- #
    bridge.price -= 0.40
    engine._tick()
    check("0.40 adverse adds layer 2", len(bridge.orders) == 2, str(bridge.orders[-1]))
    check("layer size stays 0.50 even though equity fell",
          abs(bridge.orders[1][1] - 0.50) < 1e-9,
          "floating=%.2f equity=%.2f" % (bridge._floating(), bridge.account()["equity"]))
    bridge.price -= 0.40
    engine._tick()
    check("another 0.40 adds layer 3", len(bridge.orders) == 3, str(bridge.orders[-1]))
    snap = engine.status()
    check("snapshot shows 3 layers and 1.50 lots",
          snap["basket"]["layers"] == 3 and abs(snap["basket"]["lots"] - 1.5) < 1e-9,
          "%d layers %.2f lots" % (snap["basket"]["layers"], snap["basket"]["lots"]))
    live = store._rows("SELECT layers FROM baskets WHERE closed_ts IS NULL")
    check("layer count reached the store row",
          bool(live) and live[0]["layers"] == 3, str(live))

    # --- 5. price recovers past the basket target -> close everything -- #
    bridge.price += 0.40  # no new layer, we are moving in favour now
    engine._tick()
    check("favourable move adds no layer", len(bridge.orders) == 3)

    target = engine.status()["basket"]["tp_target"]
    bridge.price += target / (1.5 * CONTRACT) + 0.60  # enough to clear the target
    engine._tick()
    check("basket profit past target closes every position",
          not bridge.positions_list and bridge.closes == 1,
          "target was %.2f" % target)
    closed = store.recent_baskets(5)
    check("the closed basket was written with a realised profit",
          len(closed) == 1 and closed[0]["profit"] > 0,
          "profit=%.2f layers=%d" % (closed[0]["profit"], closed[0]["layers"]))
    check("realised profit matches the balance delta",
          abs(closed[0]["profit"] - (bridge.balance - 14000.0)) < 0.05,
          "%.2f vs %.2f" % (closed[0]["profit"], bridge.balance - 14000.0))
    check("closing was narrated to the activity feed",
          any("Banked" in a["text"] for a in engine.status()["activity"]),
          engine.status()["activity"][0]["text"] if engine.status()["activity"] else "")

    # --- 6. cooldown after a close ------------------------------------- #
    engine._tick()
    check("cooldown blocks immediate re-entry",
          len(bridge.orders) == 3 and "cooldown" in engine.status()["engine"]["last_reason"],
          engine.status()["engine"]["last_reason"])

    # --- 7. stats and equity curve ------------------------------------- #
    snap = engine.status()
    check("stats show one closed winning basket",
          snap["stats"]["baskets_closed"] == 1 and snap["stats"]["wins"] == 1,
          "net=%.2f" % snap["stats"]["net_pl"])
    check("equity curve has points", len(snap["equity_curve"]) >= 1,
          "%d points" % len(snap["equity_curve"]))

    cleanup(store, tmp)

    # --- 7b. the margin floor trims the video's ladder on a small account #
    # The reference recording ran its margin level down to 80%. This engine
    # refuses to: at 1400 equity a second 0.50-lot layer would breach the 300%
    # floor, so it is cut to what margin actually allows and the user is told.
    cfg, engine, bridge, store, tmp = build_engine(balance=1400.0)
    engine.start()
    bridge.trend = 1
    engine._tick()
    bridge.price -= 0.40
    engine._tick()
    check("small account: the second layer is trimmed, not blindly sent",
          len(bridge.orders) == 2 and bridge.orders[1][1] < 0.50,
          "layer 2 = %.2f lots" % bridge.orders[1][1])
    check("the trim was reported to the user",
          any("Trimmed" in a["text"] for a in engine.status()["activity"]),
          [a["text"] for a in engine.status()["activity"] if "Trimmed" in a["text"]][:1])
    bridge.price -= 0.40
    engine._tick()
    # Drive price against the basket until the floor fires, rather than assuming a
    # fixed number of ticks. What is under test is that the floor *does* fire before
    # the reference recording's 80% is approached — not how much adverse movement it
    # takes to get there. That distance is a function of how conservatively the
    # entry was sized, so writing it as a literal made this test fail whenever
    # sizing improved: adding SIZING_MARGIN_BUFFER trimmed layer 2 from 0.40 to 0.36
    # lots, which is the fix working, and moved the force-close from tick 3 to tick 5.
    for _ in range(40):
        if not bridge.positions_list:
            break
        bridge.price -= 0.40
        engine._tick()
    check("the margin floor force-closes before the video's 80% is reached",
          not bridge.positions_list and engine.status()["risk"]["level"] in ("danger", "critical"),
          "%s | %s" % (engine.status()["risk"]["level"], engine.status()["notice"][:50]))
    cleanup(store, tmp)

    # --- 8. risk override beats the strategy --------------------------- #
    cfg, engine, bridge, store, tmp = build_engine(balance=1488.97, price=2400.0)
    engine.start()
    bridge.trend = 1
    engine._tick()
    check("a basket is open before the risk test", len(bridge.positions_list) == 1)
    bridge.margin_level_override = 80.38  # the reference recording's worst reading
    engine._tick()
    check("margin level 80.38% force-closes the basket regardless of strategy",
          not bridge.positions_list, "closes=%d" % bridge.closes)
    snap = engine.status()
    check("risk level reads critical and the notice explains why",
          snap["risk"]["level"] == "critical" and "argin" in snap["notice"],
          snap["notice"][:70])
    check("the forced close was recorded as a risk event",
          any(e["kind"] == "risk" for e in store.recent_events(20)),
          store.recent_events(1)[0]["message"][:60] if store.recent_events(1) else "")
    bridge.margin_level_override = None

    # --- 9. kill switch stops automation ------------------------------- #
    cfg["risk"]["kill_switch"] = True
    engine._tick()
    check("kill switch stops the engine", not engine.status()["engine"]["running"])
    check("start() refuses while the kill switch is on", not engine.start()["ok"],
          engine.start()["message"])
    cfg["risk"]["kill_switch"] = False

    # --- 9b. the safety block is read, not truth-tested ------------------ #
    # blocked() returns (is_blocked, reason). A 2-tuple is always truthy, so a
    # caller that writes `if bridge.blocked():` refuses every deploy on a
    # perfectly good account. These two checks pin both directions.
    check("start() succeeds when blocked() says (False, '')", engine.start()["ok"],
          "blocked() -> %r" % (bridge.blocked(),))
    bridge._blocked = True
    bridge._block_reason = "Account 900123 on Live-Server is a REAL money account."
    result = engine.start()
    check("start() refuses when blocked() says (True, reason)", not result["ok"],
          result["message"][:60])
    check("the refusal quotes the broker's own reason",
          "REAL money account" in result["message"], result["message"][:60])
    bridge._blocked = False
    bridge._block_reason = ""

    # --- 10. rejected order does not corrupt state --------------------- #
    engine.start()
    bridge.reject_next = True
    before = len(bridge.positions_list)
    engine._tick()
    check("a rejected order leaves no phantom basket",
          len(bridge.positions_list) == before and not engine.status()["basket"]["open"],
          engine.status()["engine"]["last_reason"][:60])

    # --- 11. strategy switching ---------------------------------------- #
    engine._strategy.reset()
    bridge.reject_next = False
    engine._tick()  # opens again
    check("engine reopened after the rejection", len(bridge.positions_list) >= 1)
    result = engine.set_strategy("ai_scalper")
    check("switching strategy is refused while a basket is open", not result["ok"],
          result["message"])
    engine.close_all_now()
    engine._tick()
    result = engine.set_strategy("ai_scalper")
    check("switching strategy works once flat", result["ok"] and engine._strategy.key == "ai_scalper",
          result["message"])
    check("unknown strategy is rejected", not engine.set_strategy("nope")["ok"])
    check("manual flatten was narrated",
          any("by hand" in a["text"] for a in engine.status()["activity"]),
          [a["text"] for a in engine.status()["activity"] if "hand" in a["text"]][:1])

    # --- 11c. the bracket a strategy asks for reaches the broker ---------- #
    # Until this was wired, market_order sent no sl/tp at all: the engine polled
    # every second and closed the basket itself, so a "2.40 stop" was only as tight
    # as the poll interval and stopped existing the moment the app did. Measured on
    # the live demo, a 2.40 stop closed at -3.21 and -4.08 — 34% and 70% past where
    # it was supposed to be, which inverts a 2.40-risk / 3.60-reward bracket.
    #
    # The engine is the only thing that can carry Intent.sl_distance to the bridge,
    # so assert on what the bridge was handed rather than on what the strategy
    # returned — a strategy can state a perfect bracket into a void.
    # The zeroed multipliers keep this test about plumbing. With them set — as
    # they are in shipped config — the broker-side legs are ATR-scaled and move
    # with the fake candles, so asserting 0.80/1.20 would be asserting the fake's
    # volatility. At 0 both legs fall back to the per-0.01-lot cash quote, which
    # is the one distance this test can state exactly and the one the bridge has
    # to carry either way.
    engine._strategy.params.update({"tp_usd": 1.20, "sl_usd": 0.80,
                                    "max_spread_usd": 0.50, "cooldown_s": 0,
                                    "max_positions": 1,
                                    "sl_atr_mult": 0.0, "tp_atr_mult": 0.0,
                                    "broker_net_mult": 0.0})
    engine._strategy.reset()
    bridge.trend = 1
    before = len(bridge.brackets)
    engine._tick()
    check("the scalper's entry reached the broker", len(bridge.brackets) == before + 1,
          "%d fill(s), last intent %s" % (len(bridge.brackets) - before,
                                          engine.status()["engine"]["last_reason"][:50]))
    if len(bridge.brackets) > before:
        sl_d, tp_d = bridge.brackets[-1]
        # gold's contract of 100 makes $x per 0.01 lot exactly x of price
        check("...carrying a broker-side stop 0.80 of price away",
              abs(sl_d - 0.80) < 1e-9, "sl_distance=%.4f" % sl_d)
        check("...and a broker-side target 1.20 away",
              abs(tp_d - 1.20) < 1e-9, "tp_distance=%.4f" % tp_d)
        held = bridge.positions_list[-1]
        entry = held["price_open"]
        check("the position the broker holds has both levels set on it",
              held["sl"] > 0 and held["tp"] > 0 and held["sl"] < entry < held["tp"],
              "entry %.2f sl %.2f tp %.2f" % (entry, held["sl"], held["tp"]))

        # --- 11d. break-even reaches the broker, once ---------------------- #
        # manage() is only worth anything if the new level ends up at the broker,
        # so assert on the position record rather than on the returned StopMove.
        # 0.01 lots at a 1.20 target trigger at 0.72 of profit; 0.85 is past that
        # and short of the target, so the basket is still open to be managed.
        engine._strategy.params.update({"breakeven_at_frac": 0.60,
                                        "breakeven_lock_usd": 0.10})
        ticket, was_tp = held["ticket"], held["tp"]
        bridge.price = entry + 0.85
        engine._tick()
        check("break-even reached the broker as an absolute level",
              bridge.stop_moves == [(ticket, round(entry + 0.10, 2), was_tp)],
              str(bridge.stop_moves))
        check("...and the broker's own record carries it",
              abs(bridge.positions_list[-1]["sl"] - (entry + 0.10)) < 0.005,
              "sl %.2f" % bridge.positions_list[-1]["sl"])
        # StopMove.tp is 0.0 for "leave that side alone", and MT5 reads a zero in
        # an SLTP request as *remove this level* — so this is the check that a
        # break-even move does not quietly strip the target it was protecting.
        check("...without disturbing the target",
              abs(bridge.positions_list[-1]["tp"] - was_tp) < 0.005,
              "tp %.2f, was %.2f" % (bridge.positions_list[-1]["tp"], was_tp))
        check("the move was narrated to the user",
              any("stop now" in a["text"] for a in engine.status()["activity"]),
              [a["text"] for a in engine.status()["activity"]
               if "stop now" in a["text"]][:1])
        engine._tick()
        check("a second tick does not move it again",
              len(bridge.stop_moves) == 1, str(bridge.stop_moves))
    engine.close_all_now()
    engine._tick()

    # --- 11b. an unreadable book is not a flat book ---------------------- #
    # All three of these were hidden by fakes that answered more helpfully than
    # the real bridge does: positions() used to return [] when the terminal could
    # not be read, and close_all() used to report a "closed" count the real one
    # never sends. Automation is off for this section — what is under test is
    # basket tracking and the kill switch, not what a strategy would decide.
    #
    # stop() has to come before the settling tick, not after it. It used to run
    # second, so that tick was still live and free to open a position of its own,
    # and the section's "1 open" assertions were really counting on the scalper
    # declining to trade rather than on automation being off. That held only while
    # the shipping spread cap (0.25) sat below this fake's spread (0.35); raising
    # the cap to 0.30 to let the scalper trade a real broker at all came within
    # 0.05 of turning three unrelated tests red for a reason nothing in them
    # mentions. Ordering it the way the comment already claims makes the section
    # independent of what the cap happens to be.
    engine.stop()
    engine.close_all_now()
    engine._tick()
    bridge.market_order("buy", 0.05)
    engine._tick()  # the engine adopts the open position as its basket
    check("a basket is being tracked before the read is broken",
          engine.status()["basket"]["open"], "%d layers" % engine.status()["basket"]["layers"])
    baskets_before = len(store.recent_baskets(limit=500))
    reads = {"n": 0}

    def blind(magic=None):
        reads["n"] += 1
        raise BridgeReadError("positions_get failed: terminal busy")

    readable = bridge.positions
    bridge.positions = blind
    engine._tick()
    check("a failed position read skips the tick instead of booking a phantom close",
          reads["n"] >= 1 and len(store.recent_baskets(limit=500)) == baskets_before,
          "%d reads, %d closed baskets" % (reads["n"], len(store.recent_baskets(limit=500))))
    flat = engine.close_all_now()
    check("the kill switch refuses to claim 'already flat' when it cannot look",
          not flat["ok"], "ok=%s / %s" % (flat["ok"], flat.get("message")))
    bridge.positions = readable
    check("the position it could not read is still open",
          len(bridge.positions_list) == 1, "%d open" % len(bridge.positions_list))
    flat = engine.close_all_now()
    check("a real flatten reports the count it actually closed",
          flat["ok"] and flat.get("closed") == 1,
          "closed=%s / %s" % (flat.get("closed"), flat.get("message")))
    engine._tick()
    engine.start()

    # --- 12. disconnected broker ---------------------------------------- #
    bridge.connected = False
    engine._last_connect_try = time.time()  # do not attempt a reconnect this tick
    engine._tick()
    snap = engine.status()
    check("disconnected tick publishes connected=false with a notice",
          not snap["connected"] and snap["notice"], snap["notice"][:60])
    check("disconnected snapshot keeps every contract key", need.issubset(snap),
          str(sorted(need - set(snap))))
    check("disconnected snapshot invents no price",
          snap["symbol"]["bid"] == 0 and snap["account"]["equity"] == 0)
    check("history survives the disconnect", snap["stats"]["baskets_closed"] >= 1,
          "%d closed" % snap["stats"]["baskets_closed"])
    check("the disconnected snapshot still reports how the link failed",
          "mt5_stage" in snap and "mt5_error" in snap and "connecting" in snap,
          str(sorted(k for k in ("mt5_stage", "mt5_error", "connecting") if k not in snap)))

    # --- 12b. the kill switch is a setting, not a broker fact ----------- #
    # It used to vanish from the snapshot while disconnected, so the UI showed
    # OFF, and clicking the toggle armed it again — it could never be disarmed.
    cfg["risk"]["kill_switch"] = True
    engine._tick()
    check("an armed kill switch still reads as armed while disconnected",
          engine.status()["risk"]["kill"] is True)
    cfg["risk"]["kill_switch"] = False
    engine._tick()
    check("and reads as disarmed once it is turned off",
          engine.status()["risk"]["kill"] is False)

    # --- 12c. connect / disconnect are asked for, not done inline ------- #
    bridge.connected = True
    engine._tick()
    result = engine.request_disconnect()
    check("request_disconnect() reports success", result["ok"], result["message"])
    check("...and actually drops the link", not bridge.is_connected(),
          "disconnects=%d" % bridge.disconnects)
    check("...and sets offline so the next tick does not undo it", engine.offline)
    before = bridge.disconnects
    engine._tick()
    engine._tick()
    check("two ticks later it is still disconnected — no silent reconnect",
          not bridge.is_connected() and bridge.disconnects == before)

    # the escalating backoff must never collapse to zero: _connect_wait = 0.0
    # made min(300, wait * 1.8) stay at zero, so a missing terminal was retried
    # on every tick, each one a GIL stall
    engine.offline = False
    engine._connect_wait = RECONNECT_EVERY
    fails = {"n": 0}

    def refuse():
        fails["n"] += 1
        return {"ok": False, "message": "MetaTrader 5 is not running."}

    bridge.connected = False
    bridge.connect = refuse
    engine._last_connect_try = 0.0
    engine._tick()
    first_wait = engine._connect_wait
    check("a failed connect grows the backoff", first_wait > RECONNECT_EVERY,
          "%.1fs" % first_wait)
    engine._tick()
    check("...and the next tick respects it instead of retrying", fails["n"] == 1,
          "%d attempts" % fails["n"])
    result = engine.request_connect()
    check("request_connect() returns immediately as pending", result["ok"]
          and result.get("pending") is True, result["message"])
    check("...without zeroing the backoff itself", engine._connect_wait == first_wait,
          "%.1fs" % engine._connect_wait)
    engine._tick()
    check("...but the very next tick does try again", fails["n"] == 2,
          "%d attempts" % fails["n"])
    check("the backoff keeps growing and never reaches zero",
          engine._connect_wait > first_wait, "%.1fs" % engine._connect_wait)
    bridge.connect = FakeBridge.connect.__get__(bridge, FakeBridge)
    engine._connect_wait = RECONNECT_EVERY
    engine._last_connect_try = 0.0
    engine._tick()
    check("a good connect is picked up by the poll thread", bridge.is_connected())

    # --- 13. thread lifecycle ------------------------------------------- #
    bridge.connected = True
    engine.manual = False
    engine.begin()
    time.sleep(0.6)
    check("polling thread runs", engine._thread is not None and engine._thread.is_alive())
    engine.shutdown()
    check("shutdown joins the thread cleanly", not engine._thread.is_alive())

    # --- 14. an exception inside a tick backs off instead of dying ------ #
    boom = {"n": 0}
    original = bridge.account

    def exploding():
        boom["n"] += 1
        raise RuntimeError("simulated broker fault")

    bridge.account = exploding
    engine._stop_evt.clear()
    engine.begin()
    time.sleep(1.2)
    engine.shutdown()
    bridge.account = original
    check("a broker fault is caught, logged and backed off",
          boom["n"] >= 1 and engine._backoff > 0,
          "%d faults, backoff %.0fs" % (boom["n"], engine._backoff))

    cleanup(store, tmp)

    # --- 15. AI SCALPER money maths and stacking ------------------------- #
    # The strategy is a pure function of a Ctx, so these go straight at decide()
    # rather than through the engine — no broker, no clock, no store.
    scalp_checks()

    # --- 15b. the burst rebuild: sizing, the broker net, break-even ------- #
    # Same approach, but these are the mechanics taken from the reference
    # recording rather than the money maths that predate it.
    burst_checks()

    # --- 16. expectancy arithmetic --------------------------------------- #
    # The one thing this app can say about a configuration before it trades.
    expectancy_checks()

    # --- 17. a real account is visible but untradeable -------------------- #
    real_money_checks()

    # --- 18. the bracket migration repairs without overriding ------------- #
    bracket_migration_checks()

    print("-" * 72)
    print("PASS %d   FAIL %d" % (PASS, FAIL))
    return 1 if FAIL else 0


def scalp_ctx(positions=None, profit=None, lots=None, slope=0.5, spread=0.25,
              balance=1400.0, bid=None, ask=None, params=None, symbol=None,
              risk=None, margin=0.0):
    """A Ctx the scalper will act on: 60 bars rising at *slope* per bar.

    A straight ramp puts EMA8 clearly above EMA21 (the lag difference times the
    slope, so 3.25 at slope 0.5) and gives every bar the same true range, so ATR
    is exactly *slope* and the separation floor is predictable. Flip *slope*
    negative for a downtrend.

    *symbol*, *risk* and *margin* exist for the burst sizing: the layer count is
    derived from the broker's own margin figure against ``risk`` — pass
    ``symbol={"margin_per_lot": ...}`` and ``risk={"min_margin_level_pct": ...}``
    to state a broker. Left out, margin cannot be priced at all, which is its own
    case (one rung, never a burst).
    """
    closes = [2400.0 + i * slope for i in range(60)]
    candles = [{"time": i, "open": c, "high": c, "low": c, "close": c,
                "tick_volume": 1, "spread": int(spread * 100), "real_volume": 0}
               for i, c in enumerate(closes)]
    last = closes[-1]
    positions = list(positions or [])
    if lots is None:
        lots = sum(float(p.get("volume") or 0.0) for p in positions)
    if profit is None:
        profit = sum(float(p.get("profit") or 0.0) for p in positions)
    settings = {"ema_fast": 8, "ema_slow": 21, "tp_usd": 1.20, "sl_usd": 0.80,
                "max_spread_usd": 0.35, "cooldown_s": 0, "max_positions": 5,
                "min_spacing_usd": 0.60, "min_sep_atr": 0.30,
                "lot_tiers": [[0, 0.01], [500, 0.05], [5000, 0.20]]}
    settings.update(params or {})
    strategy = AIScalper(settings, logging.getLogger("test.scalper"))
    sym = {"volume_min": 0.01}
    sym.update(symbol or {})
    ctx = Ctx(
        account={"balance": balance, "equity": balance, "currency": "USD",
                 "margin": margin},
        symbol=sym,
        tick={"bid": bid if bid is not None else last - spread / 2.0,
              "ask": ask if ask is not None else last + spread / 2.0,
              "spread_usd": spread},
        positions=positions,
        candles=candles,
        basket={"open": bool(positions), "lots": lots, "profit": profit,
                "layers": len(positions)},
        params=settings,
        risk=dict(risk or {}),
        now=1000.0,
    )
    return strategy, ctx


def pos(volume=0.05, price_open=2400.0, side="buy", profit=0.0, ticket=1):
    return {"ticket": ticket, "type": side, "volume": volume, "price_open": price_open,
            "price_current": price_open, "profit": profit, "swap": 0.0,
            "symbol": "XAUUSDm", "magic": 100100, "comment": "t", "time": 0}


def scalp_checks() -> None:
    # -- the bug that made every trade a guaranteed loss ------------------ #
    # 0.05 lots of gold is 5 ounces, so the spread alone is $1.75 of P/L at a
    # 0.35 spread. Read as a flat amount, an sl_usd of 0.80 is already breached
    # by that opening cost and the position is closed for a loss before price
    # moves at all. Per 0.01 lot the same 0.80 means 0.80 of price, so the stop
    # is $4.00 away and the trade is allowed to happen.
    strategy, ctx = scalp_ctx(positions=[pos(profit=-1.75)], params={"max_positions": 1})
    intent = strategy.decide(ctx)
    check("a 0.05-lot scalp is not stopped out by the spread it opened with",
          intent.action == "hold",
          "%s / %s" % (intent.action, intent.reason))
    check("...even though the loss is bigger than sl_usd read as a flat amount",
          1.75 > float(strategy.p("sl_usd", 0.0)))

    strategy, ctx = scalp_ctx(positions=[pos(profit=-4.10)])
    check("the stop fires at sl_usd scaled by open lots ($4.00 on 0.05)",
          strategy.decide(ctx).action == "close_basket")
    strategy, ctx = scalp_ctx(positions=[pos(profit=-3.90)])
    check("...and not a cent before it", strategy.decide(ctx).action != "close_basket")

    strategy, ctx = scalp_ctx(positions=[pos(profit=6.10)])
    intent = strategy.decide(ctx)
    check("the target fires at tp_usd scaled by open lots ($6.00 on 0.05)",
          intent.action == "close_basket" and "target" in intent.reason, intent.reason)
    strategy, ctx = scalp_ctx(positions=[pos(profit=5.90)])
    check("...and not a cent before it", strategy.decide(ctx).action != "close_basket")

    # the bracket must mean the same thing at every rung of the ladder
    strategy, ctx = scalp_ctx(positions=[pos(volume=0.20, profit=-15.90)], balance=6000.0)
    check("the same stop is $16.00 on 0.20 lots, so 15.90 holds",
          strategy.decide(ctx).action != "close_basket")
    strategy, ctx = scalp_ctx(positions=[pos(volume=0.20, profit=-16.10)], balance=6000.0)
    check("...and 16.10 closes", strategy.decide(ctx).action == "close_basket")

    # -- stacking --------------------------------------------------------- #
    # Every rung is base_lot now, not the lot ladder — see AIScalper._plan for why
    # (growth adds rungs, it does not inflate them). So the burst and the volume
    # ceiling are counted in the same 0.01 unit. The floating loss per rung is
    # kept small deliberately: the basket's own max loss is sl_usd scaled by open
    # lots, which on a single 0.01 rung is $0.80 — a -1.00 rung would be closed by
    # the bracket before any of the stacking logic below was reached.
    far = 2400.0 + 59 * 0.5  # last close; entry is 3.5 below it, well past spacing
    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=far - 3.0, profit=-0.10)])
    intent = strategy.decide(ctx)
    check("a partial burst is topped up while the EMA cross still agrees",
          intent.action == "add_layer" and intent.side == "buy",
          "%s / %s" % (intent.action, intent.reason))
    check("the top-up rung matches the rungs already open, from config not the ladder",
          abs(intent.volume - 0.01) < 1e-9, intent.volume)

    five = [pos(volume=0.01, price_open=far - 3.0 - i, ticket=i, profit=-0.10)
            for i in range(5)]
    strategy, ctx = scalp_ctx(positions=five)
    check("max_positions is a hard ceiling on the stack",
          strategy.decide(ctx).action == "hold")

    # A NETTING account merges same-side orders into ONE position, so the count
    # never reaches the cap however many times this fires. What binds there is the
    # volume ceiling: max_positions rungs of base_lot, 0.05 in total here. Without
    # it the stack would grow without bound — a martingale by accident. Deriving
    # the rung by averaging the position list is what breaks this case: it reads
    # the merged 0.05 as a single 0.05 rung and lifts the ceiling to five times
    # where it belongs.
    strategy, ctx = scalp_ctx(positions=[pos(volume=0.05, price_open=far - 3.0, profit=-0.10)],
                              lots=0.05)
    check("a netting account's single merged position still hits the cap",
          strategy.decide(ctx).action == "hold", str(strategy.decide(ctx).reason))
    strategy, ctx = scalp_ctx(positions=[pos(volume=0.04, price_open=far - 3.0, profit=-0.10)],
                              lots=0.04)
    check("...and one rung short of it still adds",
          strategy.decide(ctx).action == "add_layer")

    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=far - 0.10, profit=-0.10)])
    check("a layer within min_spacing_usd of an existing entry is refused",
          strategy.decide(ctx).action == "hold")

    # price rising, so EMA8 is above EMA21 — a sell basket disagrees with it
    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=far - 3.0,
                                             side="sell", profit=-0.10)])
    check("nothing is stacked into a basket the signal no longer supports",
          strategy.decide(ctx).action == "hold")

    both = [pos(volume=0.01, price_open=far - 3.0, ticket=1),
            pos(volume=0.01, price_open=far - 4.0, side="sell", ticket=2)]
    strategy, ctx = scalp_ctx(positions=both)
    check("a hedged book is left alone rather than added to",
          strategy.decide(ctx).action == "hold")

    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=far - 3.0, profit=-0.10)],
                              params={"max_positions": 1})
    check("max_positions 1 keeps the old one-at-a-time behaviour",
          strategy.decide(ctx).action == "hold")

    # a wide spread stops new layers as well as new baskets
    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=far - 3.0, profit=-0.10)],
                              spread=0.90)
    check("the spread filter applies to layers too",
          strategy.decide(ctx).action == "hold")

    # -- entry gate ------------------------------------------------------- #
    strategy, ctx = scalp_ctx()
    intent = strategy.decide(ctx)
    check("a flat book with a clear cross opens", intent.action == "open" and intent.side == "buy",
          "%s / %s" % (intent.action, intent.reason))
    strategy, ctx = scalp_ctx(slope=-0.5)
    check("a falling market sells", strategy.decide(ctx).side == "sell")
    # slope 0.02: separation is 0.13, under both the 0.25 spread floor and
    # 0.30 x ATR, so the cross is too flat to act on
    strategy, ctx = scalp_ctx(slope=0.02)
    intent = strategy.decide(ctx)
    check("a nearly flat cross is not traded",
          intent.action == "hold" and "apart" in intent.reason, intent.reason)

    # -- the bracket, stated as a distance the broker can hold ------------- #
    # tp_usd/sl_usd are quoted per 0.01 lot precisely so they mean a fixed distance
    # in price; converting back out is undoing that money step. On gold the divisor
    # is 0.01 * 100 = 1.0, so the figures come out looking identical to the money
    # ones — which is exactly the coincidence that would let a missing division sit
    # here unnoticed. The forex check below is the one that can actually fail.
    strategy, ctx = scalp_ctx()
    intent = strategy.decide(ctx)
    check("an entry states its stop as a price distance, not just as money",
          abs(intent.sl_distance - 0.80) < 1e-9 and abs(intent.tp_distance - 1.20) < 1e-9,
          "sl %.4f tp %.4f" % (intent.sl_distance, intent.tp_distance))

    strategy, ctx = scalp_ctx()
    ctx.symbol = dict(ctx.symbol, trade_contract_size=100000.0)
    intent = strategy.decide(ctx)
    check("the same bracket is 0.0008/0.0012 on a 100 000-unit contract",
          abs(intent.sl_distance - 0.0008) < 1e-12
          and abs(intent.tp_distance - 0.0012) < 1e-12,
          "sl %.6f tp %.6f" % (intent.sl_distance, intent.tp_distance))

    # a stacked layer is a position in its own right and needs its own bracket
    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=far - 3.0, profit=-0.10)])
    intent = strategy.decide(ctx)
    check("a stacked layer carries the same bracket as the first entry",
          intent.action == "add_layer" and abs(intent.sl_distance - 0.80) < 1e-9
          and abs(intent.tp_distance - 1.20) < 1e-9,
          "%s sl %.4f tp %.4f" % (intent.action, intent.sl_distance, intent.tp_distance))


def burst_checks() -> None:
    """The rebuild: burst sizing, the broker net's position, and break-even.

    The measured record this replaces: 41 closed baskets on Deriv-Demo, 5 wins,
    36 losses, -$77.66, and ``avg_layers`` of exactly 1.0 across all 41 — the
    stack never fired once. Two causes, one test each below. The stop was
    ``sl_usd`` 0.80 converted to price, 0.21x an M1 ATR(14) of 3.806, so ordinary
    noise closed every trade; and ``min_spacing_usd`` 0.60 vetoed every layer a
    burst would have opened at one price.
    """
    # the shipped bracket, so these tests fail if config and code drift apart
    NET = {"tp_usd": 1.80, "sl_usd": 3.60, "sl_atr_mult": 1.60, "tp_atr_mult": 4.00,
           "broker_net_mult": 1.50, "min_spacing_usd": 0.0}
    # Deriv's own figures: $217.68 of margin per 0.01 lot of gold at $2,714 equity,
    # which is a 5% per-symbol rate rather than the 1:30 the account advertises.
    DERIV = {"margin_per_lot": 21768.0}
    FLOOR = {"min_margin_level_pct": 300.0}

    # -- how many rungs, and who decides ----------------------------------- #
    # The recording's eight-position burst is a property of its broker, not of its
    # strategy. Hard-coding eight would mean a stream of rejections here, so the
    # count comes from the margin the broker charges against the floor the risk
    # manager enforces.
    strategy, ctx = scalp_ctx(params=NET, symbol=DERIV, risk=FLOOR, balance=2714.0)
    intent = strategy.decide(ctx)
    check("the burst is three rungs on the account that was measured",
          intent.action == "open" and abs(intent.volume - 0.03) < 1e-9,
          "%s %.2f / %s" % (intent.action, intent.volume, intent.reason))
    check("...and the reason says so in rungs, not just in lots",
          "3x0.01" in intent.reason, intent.reason)

    strategy, ctx = scalp_ctx(params=NET, symbol={"margin_per_lot": 4000.0},
                              risk=FLOOR, balance=2714.0)
    check("the same code opens a bigger burst on a broker that charges less",
          abs(strategy.decide(ctx).volume - 0.05) < 1e-9, strategy.decide(ctx).volume)

    # margin_per_lot off the symbol is order_calc_margin's answer; Ctx.margin_per_lot
    # is the textbook price*contract/leverage, which under-states Deriv by 1.5x. If
    # the wrong one won, this account would be sized for 8 rungs and capped at 5.
    strategy, ctx = scalp_ctx(params=NET, symbol=DERIV, risk=FLOOR, balance=2714.0)
    ctx.account = dict(ctx.account, leverage=30)
    check("the broker's own margin figure beats the leverage formula",
          abs(strategy.decide(ctx).volume - 0.03) < 1e-9, strategy.decide(ctx).volume)

    # RiskManager refuses to open at min_margin x 1.25 and force-closes at
    # min_margin itself. Sizing against the bare floor builds a basket its own risk
    # manager will not extend and then liquidates — which is defect #17, from the
    # other side.
    strategy, ctx = scalp_ctx(params=dict(NET, entry_margin_band=1.0), symbol=DERIV,
                              risk=FLOOR, balance=2714.0)
    check("sizing uses the band the risk manager enforces, not the bare floor",
          abs(strategy.decide(ctx).volume - 0.04) < 1e-9,
          "%.2f at band 1.0 vs 0.03 at 1.25" % strategy.decide(ctx).volume)

    strategy, ctx = scalp_ctx(params=NET, symbol=DERIV, risk=FLOOR, balance=2714.0,
                              margin=500.0)
    check("margin already committed shrinks the next burst",
          abs(strategy.decide(ctx).volume - 0.01) < 1e-9, strategy.decide(ctx).volume)

    strategy, ctx = scalp_ctx(params=NET, symbol=DERIV,
                              risk=dict(FLOOR, max_basket_layers=2), balance=2714.0)
    check("risk.max_basket_layers caps the burst below what margin allows",
          abs(strategy.decide(ctx).volume - 0.02) < 1e-9, strategy.decide(ctx).volume)

    strategy, ctx = scalp_ctx(params=NET, symbol=DERIV, risk=FLOOR, balance=100.0)
    intent = strategy.decide(ctx)
    check("an account the floor leaves no room in opens nothing at all",
          intent.action == "hold" and "no room" in intent.reason, intent.reason)

    # 0 rungs means "the floor leaves no room"; a broker whose margin cannot be
    # priced at all is a different answer, and guessing a burst there is how you
    # get eight rejections in a row.
    strategy, ctx = scalp_ctx(params=NET, risk=FLOOR, balance=2714.0)
    check("a broker whose margin cannot be priced gets one rung, never a burst",
          abs(strategy.decide(ctx).volume - 0.01) < 1e-9, strategy.decide(ctx).volume)

    strategy, ctx = scalp_ctx(params=NET, symbol=DERIV, balance=2714.0)
    check("with no floor configured the burst is capped by max_positions alone",
          abs(strategy.decide(ctx).volume - 0.05) < 1e-9, strategy.decide(ctx).volume)

    # -- where the broker's stop sits relative to the basket's own ---------- #
    # This is the whole rebuild in one assertion. The basket money bracket is the
    # exit; the broker-side one is a catastrophe net for the case this app is not
    # running to enforce the basket. If the net is the tighter of the two it stops
    # being a net and becomes the strategy — which is exactly what a 0.21x-ATR stop
    # was. sl_usd 3.60 per 0.01 lot is 3.60 of price on gold (contract 100).
    for label, slope, want_sl, want_tp in (
            ("a dead-quiet session floors both legs", 0.10, 5.40, 2.70),
            ("a normal session floors the stop and lets the target run", 1.00, 5.40, 4.00),
            ("a violent session lets ATR set both", 5.00, 8.00, 20.00)):
        strategy, ctx = scalp_ctx(params=NET, symbol=DERIV, risk=FLOOR,
                                  balance=2714.0, slope=slope)
        intent = strategy.decide(ctx)
        check(label,
              abs(intent.sl_distance - want_sl) < 1e-9
              and abs(intent.tp_distance - want_tp) < 1e-9,
              "ATR %.2f -> sl %.2f tp %.2f" % (slope, intent.sl_distance,
                                               intent.tp_distance))
        check("...and the basket still exits first at ATR %.2f" % slope,
              intent.sl_distance > 3.60 and intent.tp_distance > 1.80,
              "net %.2f/%.2f vs basket 3.60/1.80" % (intent.sl_distance,
                                                     intent.tp_distance))

    # -- the chop filter --------------------------------------------------- #
    # A burst commits several rungs at one price, so it is far more exposed to a
    # cross that is really chop than a single position was. These ramps have
    # high==low==close, so every bar's directional movement is the whole of its
    # true range and ADX is exactly 100 — a threshold has to be set above that to
    # provoke the veto, which is why the second case uses a figure no market
    # reaches. What the first case guards is the real risk in adding a gate: that
    # it silently vetoes everything.
    strategy, ctx = scalp_ctx(params=dict(NET, min_adx=15.0), symbol=DERIV,
                              risk=FLOOR, balance=2714.0)
    check("the shipped ADX floor does not block a clean trend",
          strategy.decide(ctx).action == "open", strategy.decide(ctx).reason)
    strategy, ctx = scalp_ctx(params=dict(NET, min_adx=101.0), symbol=DERIV,
                              risk=FLOOR, balance=2714.0)
    intent = strategy.decide(ctx)
    check("a reading below the ADX floor is reported as chop, not traded",
          intent.action == "hold" and "chop" in intent.reason, intent.reason)

    # -- break-even, the other half of the basket bracket ------------------- #
    # The failure this insures against: the group runs to within a fraction of its
    # cash target, reverses, and gives all of it back because the only thing behind
    # it was a net placed 1.6x ATR away at entry.
    BE = dict(NET, breakeven_at_frac=0.60, breakeven_lock_usd=0.10)
    entry = 2400.0

    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=entry, ticket=77)],
                              profit=1.20, params=NET)
    check("bracket management stays off until breakeven_at_frac is set",
          strategy.manage(ctx) == [])

    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=entry, ticket=77)],
                              profit=1.00, params=BE)
    check("below 60% of the 1.80 target nothing is moved",
          strategy.manage(ctx) == [], "1.00 of 1.80")

    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=entry, ticket=77)],
                              profit=1.20, params=BE)
    moves = strategy.manage(ctx)
    check("at 60% of target the rung is locked at break-even plus the buffer",
          len(moves) == 1 and moves[0].ticket == 77
          and abs(moves[0].sl - 2400.10) < 1e-9,
          str([(m.ticket, m.sl) for m in moves]))
    check("...and the target side is left alone rather than removed",
          len(moves) == 1 and moves[0].tp == 0.0,
          "tp=%r" % (moves[0].tp if moves else None))

    # Strategy.manage's docstring says the engine drops a move that does not
    # improve on the current stop; TradingEngine._manage forwards every move with a
    # ticket verbatim, so the ratchet has to live here. It also has to survive
    # being called once a second for the life of the position.
    held = dict(pos(volume=0.01, price_open=entry, ticket=77), sl=2400.10)
    strategy, ctx = scalp_ctx(positions=[held], profit=1.20, params=BE)
    check("a stop already at break-even is not moved again",
          strategy.manage(ctx) == [])
    held = dict(pos(volume=0.01, price_open=entry, ticket=77), sl=2425.00)
    strategy, ctx = scalp_ctx(positions=[held], profit=1.20, params=BE)
    check("a stop further forward than break-even is never pulled back",
          strategy.manage(ctx) == [], "2425.00 would become 2400.10")

    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=2500.0, ticket=77)],
                              profit=1.20, params=BE)
    check("a break-even level on the far side of the market is skipped",
          strategy.manage(ctx) == [], "entry 2500 against a bid of 2429")

    strategy, ctx = scalp_ctx(positions=[pos(volume=0.01, price_open=2460.0,
                                             side="sell", ticket=77)],
                              profit=1.20, params=BE)
    moves = strategy.manage(ctx)
    check("a sell rung locks the same distance the other way",
          len(moves) == 1 and abs(moves[0].sl - 2459.90) < 1e-9,
          str([(m.ticket, m.sl) for m in moves]))

    mixed = [dict(pos(volume=0.01, price_open=entry, ticket=77), sl=2400.10),
             pos(volume=0.01, price_open=entry, ticket=78)]
    strategy, ctx = scalp_ctx(positions=mixed, profit=2.40, params=BE)
    moves = strategy.manage(ctx)
    check("in a burst each rung is judged on its own stop",
          len(moves) == 1 and moves[0].ticket == 78,
          str([(m.ticket, m.sl) for m in moves]))


def expectancy_checks() -> None:
    """The break-even arithmetic, and the sample rule that stops it lying early."""
    from backend.strategies import MIN_SAMPLE, _expectancy

    # -- from settings alone, before a single trade ------------------------ #
    e = _expectancy("ai_scalper", {"tp_usd": 1.20, "sl_usd": 0.80, "max_spread_usd": 0.25})
    check("a 1.5:1 bracket needs 40% of trades to win",
          abs(e["required_win_pct"] - 40.0) < 0.1, str(e.get("required_win_pct")))
    check("the required rate is labelled as coming from the settings",
          e["required_from"] == "settings", str(e.get("required_from")))
    check("a spread cap of 0.25 against a 1.20 target is 21% of the prize",
          abs(e["cost_pct_of_target"] - 20.8) < 0.1, str(e.get("cost_pct_of_target")))

    # -- the exact configuration behind "it is always in loss" ------------- #
    # tp 0.40 with a 0.25 spread cap: 62% of the target goes on the entry, and
    # this must be reported as unpayable without waiting for a single basket
    bad = _expectancy("ai_scalper", {"tp_usd": 0.40, "sl_usd": 0.80, "max_spread_usd": 0.25})
    check("the old bracket is called out as spread-eaten with no history at all",
          bad["verdict"] == "cost-heavy" and bad["baskets_closed"] == 0, str(bad["verdict"]))
    check("that verdict survives a winning-looking sample",
          _expectancy("ai_scalper", {"tp_usd": 0.40, "sl_usd": 0.80, "max_spread_usd": 0.25},
                      {"baskets_closed": 400, "win_pct": 95.0, "avg_win": 0.3,
                       "avg_loss": -0.1})["verdict"] == "cost-heavy")

    # -- measured beats configured ---------------------------------------- #
    # settings say 40%; the trades say the broker delivered +6.15 against -4.12,
    # which is 40.1% - and the measured figure is the one that must be quoted
    e = _expectancy("ai_scalper", {"tp_usd": 1.20, "sl_usd": 0.80, "max_spread_usd": 0.25},
                    {"baskets_closed": 188, "win_pct": 27.7, "net_pl": -240.1,
                     "avg_win": 6.15, "avg_loss": -4.12})
    check("with enough trades the required rate comes from the trades",
          e["required_from"] == "trades" and abs(e["required_win_pct"] - 40.1) < 0.2,
          "%s %s" % (e.get("required_from"), e.get("required_win_pct")))
    check("28% actual against 40% needed is reported as losing money",
          e["verdict"] == "losing", e["note"])

    # -- the sample rule -------------------------------------------------- #
    thin = _expectancy("ai_scalper", {"tp_usd": 1.20, "sl_usd": 0.80, "max_spread_usd": 0.25},
                       {"baskets_closed": MIN_SAMPLE - 1, "win_pct": 0.0,
                        "avg_win": 6.15, "avg_loss": -4.12})
    check("under %d closed baskets it refuses to pass judgement" % MIN_SAMPLE,
          thin["verdict"] == "unproven", "%s: %s" % (thin["verdict"], thin["note"]))
    good = _expectancy("ai_scalper", {"tp_usd": 1.20, "sl_usd": 0.80, "max_spread_usd": 0.25},
                       {"baskets_closed": 200, "win_pct": 55.0,
                        "avg_win": 6.15, "avg_loss": -4.12})
    check("55% against 40% needed reads as on track", good["verdict"] == "working", good["note"])
    edge = _expectancy("ai_scalper", {"tp_usd": 1.20, "sl_usd": 0.80, "max_spread_usd": 0.25},
                       {"baskets_closed": 200, "win_pct": 40.1,
                        "avg_win": 6.15, "avg_loss": -4.12})
    check("landing exactly on the required rate is not called losing",
          edge["verdict"] == "working", "%s: %s" % (edge["verdict"], edge["note"]))

    # -- the grid has no stop, and that is the headline -------------------- #
    grid = _expectancy("recovery_grid", {"basket_tp_pct_equity": 1.5,
                                         "basket_sl_pct_equity": 0.0})
    check("a target with no stop behind it reports no-stop, not a win rate",
          grid["verdict"] == "no-stop" and "required_win_pct" not in grid, str(grid))
    check("a 97% win rate does not hide the missing stop",
          _expectancy("recovery_grid",
                      {"basket_tp_pct_equity": 1.5, "basket_sl_pct_equity": 0.0},
                      {"baskets_closed": 86, "win_pct": 97.7, "net_pl": -412.0,
                       "avg_win": 18.4, "avg_loss": -970.0})["verdict"] == "no-stop")

    # -- nothing to say, said as nothing ---------------------------------- #
    blank = _expectancy("mean_reversion", {})
    check("an unconfigured bracket claims nothing",
          blank["verdict"] == "unproven" and "required_win_pct" not in blank, str(blank))
    check("garbage in the settings does not raise",
          _expectancy("ai_scalper", {"tp_usd": "abc", "sl_usd": None,
                                     "max_spread_usd": []})["verdict"] == "unproven")
    check("a strategy outside the bracket table still returns a verdict",
          _expectancy("does_not_exist", {"tp_usd": 1.0})["verdict"] == "unproven")




# --------------------------------------------------------------------------- #
# A REAL account connects read-only. The UI promises "A real account will still
# connect and be shown - it just will not be traded", and until this section
# existed that sentence was false in both halves: connect() returned early at the
# safety stage so _connected stayed False and the panel read "Not connected",
# and because the connection never happened, the refusal to connect was the only
# thing between a real account and automated trading. Every check below is about
# one of those two halves - the account is visible, and no order can leave.
# --------------------------------------------------------------------------- #

class _RecordingMT5:
    """A stub MetaTrader5 module that screams if an order is ever sent."""

    TRADE_ACTION_DEAL = 1
    TRADE_RETCODE_DONE = 10009
    ORDER_TYPE_BUY = 0
    ORDER_TYPE_SELL = 1
    ORDER_FILLING_IOC = 1

    def __init__(self):
        self.sends = []

    def order_send(self, request):
        self.sends.append(dict(request))
        raise AssertionError("order_send reached on a blocked real account")

    def terminal_info(self):
        return type("T", (), {"_asdict": lambda s: {"connected": True}})()

    def account_info(self):
        return type("A", (), {"_asdict": lambda s: {"login": 555, "trade_mode": 2}})()

    def last_error(self):
        return (0, "")


def _real_bridge(allow, is_real=True):
    """A bridge that believes it is connected to *is_real* with consent *allow*."""
    from backend import mt5_bridge as mb
    bridge = mb.MT5Bridge({"account": {"login": 555, "allow_live_real": allow},
                           "symbol": {"name": "XAUUSD"},
                           "execution": {"max_lot_per_order": 1.0}},
                          logging.getLogger("test.bridge"))
    bridge._connected = True
    bridge._real_account = is_real
    bridge.symbol_name = "XAUUSD"
    return bridge


def real_money_checks() -> None:
    from backend import mt5_bridge as mb

    saved_mt5, saved_avail = mb.mt5, mb.MT5_AVAILABLE
    stub = _RecordingMT5()
    mb.mt5, mb.MT5_AVAILABLE = stub, True
    try:
        # -- the interlock refuses, and refuses before it sends ------------- #
        for allow in (False, None, "false", "true", "yes", 1, "1", [], {}, "TRUE"):
            bridge = _real_bridge(allow)
            guard = bridge._guard()
            # only the literal True is consent. "true" and "yes" are *words*, and
            # a word arriving here means a coercion was skipped somewhere upstream,
            # which is exactly the mistake this layer exists to survive.
            check("a real account with allow_live_real=%r is refused" % (allow,),
                  guard is not None and "blocked by safety policy" == guard.get("comment"),
                  "guard=%r" % (guard,))
            res = bridge.market_order("buy", 0.10)
            check("...and market_order returns that refusal instead of trading",
                  res.get("ok") is False and "allow_live_real" in str(res.get("detail")),
                  "detail=%r" % (res.get("detail"),))
        check("no order request was ever built, let alone sent",
              stub.sends == [], "sends=%r" % (stub.sends,))

        # -- consent is honoured, and read from the live config ------------- #
        bridge = _real_bridge(True)
        check("the literal True is the one value that permits real trading",
              bridge._real_money_block() is None, str(bridge._real_money_block()))
        # revoke it in place, the way POST /api/config does
        bridge.config["account"]["allow_live_real"] = False
        check("revoking consent stops the next order, not the next reconnect",
              bridge._real_money_block() is not None)
        check("...and blocked() agrees at once, so DEPLOY is refused too",
              bridge.blocked()[0] is True and "allow_live_real" in bridge.blocked()[1])

        # -- a demo account is untouched by any of this --------------------- #
        demo = _real_bridge(False, is_real=False)
        check("a demo account is not blocked by the real-money interlock",
              demo._real_money_block() is None and demo.blocked()[0] is False)

        # -- the cached flag still works on its own ------------------------- #
        # _blocked is set once at connect time; _real_money_block() is re-derived
        # per order. Either alone must be enough, so neither can be the single
        # point of failure.
        cached = _real_bridge(True)
        cached._blocked = True
        cached._block_reason = "set at connect time"
        check("the connect-time block alone also stops an order",
              cached._guard() is not None and
              cached._guard().get("detail") == "set at connect time")

        # -- the engine's deploy gate refuses a blocked bridge -------------- #
        cfg, engine, fake, store, tmp = build_engine()
        try:
            fake.blocked = lambda: (True, "real money, no consent")
            out = engine.start()
            check("DEPLOY is refused while the bridge reports blocked",
                  out.get("ok") is False and out.get("running") is False,
                  str(out.get("message")))
            check("...and the reason shown is the bridge's own words",
                  "real money" in str(out.get("message")))
            check("...and automation did not quietly start anyway",
                  engine.running() is False)
        finally:
            cleanup(store, tmp)
    finally:
        mb.mt5, mb.MT5_AVAILABLE = saved_mt5, saved_avail

# --------------------------------------------------------------------------- #
# _migrate_scalper_bracket used to reset all three money keys whenever any one of
# them failed its test, on every single load. A deliberately wide target - tp 5.00
# against a 2.00 spread cap - was therefore pulled back to the 1.20 default and
# stayed there, silently, forever. A migration exists to repair a bracket that
# cannot win; overriding a choice the user made on purpose is a different thing.
# --------------------------------------------------------------------------- #

def bracket_migration_checks() -> None:
    import copy as _copy
    import json as _json
    import tempfile as _tempfile
    from backend.config import DEFAULTS, _migrate_scalper_bracket, load_config

    fresh = DEFAULTS["strategies"]["ai_scalper"]
    log = logging.getLogger("test.migrate")

    def run(tp, sl, cap):
        raw = {"strategies": {"ai_scalper": dict(fresh, tp_usd=tp, sl_usd=sl,
                                                 max_spread_usd=cap)}}
        fired = _migrate_scalper_bracket(raw, log)
        block = raw["strategies"]["ai_scalper"]
        # a migration that is not idempotent rewrites config.json on every start
        again = _migrate_scalper_bracket(_copy.deepcopy(raw), log)
        return fired, block, again

    # -- the shipping bracket is already correct, so nothing happens -------- #
    fired, block, _ = run(fresh["tp_usd"], fresh["sl_usd"], fresh["max_spread_usd"])
    check("the shipping bracket does not trigger the migration", fired is False)

    # -- a wide target is kept; the cap moves instead ----------------------- #
    fired, block, again = run(5.00, 3.00, 2.00)
    check("a deliberate 5.00 target survives the migration",
          block["tp_usd"] == 5.00, "tp=%r" % (block["tp_usd"],))
    check("...and it is the spread cap that was tightened, not the target",
          block["max_spread_usd"] < 2.00 and
          block["max_spread_usd"] <= 5.00 / 3.0,
          "cap=%r" % (block["max_spread_usd"],))
    check("...and the stop the user chose is left alone", block["sl_usd"] == 3.00)
    check("...and running it twice changes nothing more", again is False)

    # -- only the failing key moves ----------------------------------------- #
    fired, block, again = run(5.00, 0.20, 0.25)
    check("a stop inside the spread is widened",
          block["sl_usd"] > block["max_spread_usd"], "sl=%r cap=%r"
          % (block["sl_usd"], block["max_spread_usd"]))
    check("...without touching the target that was already fine",
          block["tp_usd"] == 5.00, "tp=%r" % (block["tp_usd"],))
    check("...or the spread cap that was already fine",
          block["max_spread_usd"] == 0.25)
    check("...and it is idempotent", again is False)

    # -- the bracket this migration actually exists for --------------------- #
    for tp, sl, cap in ((0.40, 0.80, 0.30), (0.40, 0.30, 0.30), (0.40, 0.80, 0.40)):
        fired, block, again = run(tp, sl, cap)
        check("the old flat bracket %.2f/%.2f/%.2f is still fully replaced"
              % (tp, sl, cap),
              fired is True and block["tp_usd"] == fresh["tp_usd"] and
              block["sl_usd"] == fresh["sl_usd"] and
              block["max_spread_usd"] == fresh["max_spread_usd"], str(block["tp_usd"]))
        check("...and the replacement passes its own test", again is False)

    # -- edges -------------------------------------------------------------- #
    fired, block, _ = run(0.40, 0.80, 0.0)
    check("no spread filter means nothing to measure against, so no rewrite",
          fired is False and block["tp_usd"] == 0.40)
    # "exactly at the floor" has to be computed from the shipping cap, not written
    # out as a literal. It was 0.75, which was 0.25 x 3.0 back when the default cap
    # was 0.25 — so raising the cap moved the floor and left the literal sitting
    # below it, and the test failed claiming a viable target had been replaced when
    # what had really changed was the definition of viable.
    floor_tp = fresh["max_spread_usd"] * 3.0
    fired, block, again = run(floor_tp, 0.10, 0.30)
    check("a target exactly at the viability floor is kept, not replaced",
          block["tp_usd"] == floor_tp, "tp=%r (floor %r)" % (block["tp_usd"], floor_tp))
    check("...and its bracket ends up self-consistent",
          block["sl_usd"] > block["max_spread_usd"] and
          block["tp_usd"] >= block["max_spread_usd"] * 3.0 and again is False,
          str(block))

    # -- and the whole thing through the real loader, twice ----------------- #
    tmp = _tempfile.mkdtemp()
    path = os.path.join(tmp, "config.json")
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump({"strategies": {"ai_scalper": dict(fresh, tp_usd=5.0, sl_usd=3.0,
                                                      max_spread_usd=2.0)}}, fh)
    seen = []
    for _ in range(3):
        seen.append(load_config(path)["strategies"]["ai_scalper"]["tp_usd"])
    check("load_config keeps the wide target across repeated starts",
          seen == [5.0, 5.0, 5.0], str(seen))
    with open(path, encoding="utf-8") as fh:
        check("...and what was written back to disk kept it too",
              _json.load(fh)["strategies"]["ai_scalper"]["tp_usd"] == 5.0)

    # a hostile value in a money key must not survive the loader either
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump({"strategies": {"ai_scalper": dict(fresh, tp_usd="x", sl_usd=None)}}, fh)
    block = load_config(path)["strategies"]["ai_scalper"]
    check("garbage in the money keys is coerced to numbers by the loader",
          isinstance(block["tp_usd"], float) and isinstance(block["sl_usd"], float),
          "tp=%r sl=%r" % (block["tp_usd"], block["sl_usd"]))

    # ---------------------------------------------------------------------- #
    # 18b. the revision migration — the one that reaches a real user's file
    #
    # The arithmetic migration above cannot move revision 1's bracket, and that
    # is not an oversight: tp 1.20 / sl 0.80 against an 0.30 cap PASSES the
    # spread test. It was still the bracket that lost 41 baskets, because an
    # 0.80 stop is 0.21x the measured M1 ATR and noise reached it. Nothing in
    # config.json knows what ATR is, so only an explicit revision stamp can
    # move that file forward.
    # ---------------------------------------------------------------------- #
    from backend.config import (SCALPER_BRACKET_REV, _SCALPER_REV1,
                                _migrate_scalper_revision)

    def rev_run(block_in):
        raw = {"strategies": {"ai_scalper": dict(block_in)}}
        fired = _migrate_scalper_revision(raw, log)
        block_out = raw["strategies"]["ai_scalper"]
        again = _migrate_scalper_revision(_copy.deepcopy(raw), log)
        return fired, block_out, again

    # the exact block that was sitting in the user's config.json
    rev1 = {k: v for k, v in fresh.items() if k != "bracket_rev"}
    rev1.update(_SCALPER_REV1)
    fired, block, again = rev_run(rev1)
    check("the shipped revision-1 bracket is recognised and moved on",
          fired is True and block["sl_usd"] == fresh["sl_usd"] and
          block["tp_usd"] == fresh["tp_usd"],
          "tp %.2f sl %.2f" % (block["tp_usd"], block["sl_usd"]))
    check("...including the spacing veto that pinned avg_layers at 1.0",
          block["min_spacing_usd"] == fresh["min_spacing_usd"],
          "min_spacing_usd=%r" % (block["min_spacing_usd"],))
    check("...and the block is stamped so it is never rewritten again",
          block["bracket_rev"] == SCALPER_BRACKET_REV and again is False,
          "rev=%r again=%r" % (block.get("bracket_rev"), again))
    check("...while a key revision 2 does not touch is left alone",
          block["max_spread_usd"] == rev1["max_spread_usd"] and
          block["cooldown_s"] == rev1["cooldown_s"])

    # a value the user changed themselves is NOT last release's default, so it
    # is kept. This is the difference between migrating a file and overwriting
    # somebody's settings.
    hand = dict(rev1, sl_usd=7.5, tp_usd=1.20)
    fired, block, again = rev_run(hand)
    check("a hand-edited stop survives the revision migration",
          block["sl_usd"] == 7.5, "sl=%r" % (block["sl_usd"],))
    check("...while the untouched default beside it still moves",
          block["tp_usd"] == fresh["tp_usd"], "tp=%r" % (block["tp_usd"],))
    check("...and that file is stamped too, so it is asked once and never again",
          block["bracket_rev"] == SCALPER_BRACKET_REV and again is False)

    # a file where every value is deliberate gets the stamp and nothing else
    allmine = dict(rev1, tp_usd=2.5, sl_usd=7.5, min_spacing_usd=1.25)
    fired, block, again = rev_run(allmine)
    check("an all-hand-edited block is stamped without a single value changing",
          fired is True and block["tp_usd"] == 2.5 and block["sl_usd"] == 7.5 and
          block["min_spacing_usd"] == 1.25 and again is False, str(fired))

    # already current, and a rev from the future
    for rev in (SCALPER_BRACKET_REV, SCALPER_BRACKET_REV + 1):
        fired, block, _ = rev_run(dict(rev1, bracket_rev=rev))
        check("revision %d is left completely alone" % rev,
              fired is False and block["sl_usd"] == _SCALPER_REV1["sl_usd"],
              "sl=%r" % (block["sl_usd"],))

    # garbage in the stamp must not skip the migration - an unreadable revision
    # is not a revision, so it reads as "older than 2" and moves forward
    for junk in ("", None, "two", float("nan"), [], {}):
        fired, block, again = rev_run(dict(rev1, bracket_rev=junk))
        check("a stamp of %r is treated as pre-revision, not as current" % (junk,),
              fired is True and block["sl_usd"] == fresh["sl_usd"] and again is False,
              "sl=%r" % (block["sl_usd"],))

    # and the whole thing through the real loader on a revision-1 file
    with open(path, "w", encoding="utf-8") as fh:
        _json.dump({"strategies": {"ai_scalper": dict(rev1)}}, fh)
    seen = []
    for _ in range(3):
        seen.append(load_config(path)["strategies"]["ai_scalper"]["sl_usd"])
    check("load_config moves a revision-1 file forward and then holds it",
          seen == [fresh["sl_usd"]] * 3, str(seen))
    with open(path, encoding="utf-8") as fh:
        on_disk = _json.load(fh)["strategies"]["ai_scalper"]
    check("...and the corrected bracket reached the disk, stamped",
          on_disk["sl_usd"] == fresh["sl_usd"] and
          on_disk["min_spacing_usd"] == fresh["min_spacing_usd"] and
          on_disk["bracket_rev"] == SCALPER_BRACKET_REV, str(on_disk["bracket_rev"]))
    # the migrated result must also satisfy the arithmetic migration, or the two
    # would fight each other on every start
    check("...and the arithmetic migration has nothing left to say about it",
          _migrate_scalper_bracket({"strategies": {"ai_scalper": dict(on_disk)}},
                                   log) is False)


if __name__ == "__main__":
    raise SystemExit(main())
