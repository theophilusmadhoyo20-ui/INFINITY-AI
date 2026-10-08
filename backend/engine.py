"""TradingEngine — one thread, one decision loop, risk first.

Order of operations every tick, and it is not negotiable:

    read broker  ->  RiskManager.evaluate  ->  strategy.decide  ->  execute
                     (risk can veto or override the strategy entirely)

The thread runs whenever the app is up so the UI always has live numbers;
``start()`` / ``stop()`` only toggle whether decisions are allowed to trade.
"""

from __future__ import annotations

import copy
import logging
import threading
import time
from typing import Optional

from backend.mt5_bridge import BridgeReadError
from backend.strategies import REGISTRY, Ctx, build, keys as strategy_keys

MAX_BACKOFF = 30.0
RECONNECT_EVERY = 20.0
EQUITY_EVERY = 10.0
CANDLE_EVERY = 2.0
ACTIVITY_MAX = 60


def _close_msg(result: dict) -> str:
    """Plain-English reason out of an execution result.

    The bridge contract is ``{ok, retcode, comment, tickets, detail}``. Callers
    that reached for ``message`` got None and logged "Close failed: None", so both
    documented keys are tried before falling back.
    """
    result = result or {}
    for key in ("comment", "detail", "message", "error"):
        value = str(result.get(key) or "").strip()
        if value:
            return value
    return "Close failed."


def _f(value, default: float = 0.0) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return default
    return default if out != out else out


def _i(value, default: int = 0) -> int:
    """Int, via float, so a ticket that arrives as "12345.0" still parses."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def as_block(value) -> tuple:
    """Normalise ``bridge.blocked()`` to ``(is_blocked, reason)``.

    MT5Bridge.blocked() returns the 2-tuple ``(self._blocked, self._block_reason)``,
    and a 2-tuple is *always* truthy — ``if bridge.blocked():`` therefore reads as
    "blocked" even when the first element is False. That refuses every deploy the
    moment a broker is genuinely connected, which no test caught because the
    simulated bridge used to return a bare bool. Unpack, never truth-test.

    A bare bool is still tolerated so an alternative bridge cannot crash start().
    """
    if isinstance(value, (tuple, list)):
        return (bool(value[0]) if value else False,
                str(value[1]) if len(value) > 1 and value[1] else "")
    return bool(value), ""


def _empty_snapshot() -> dict:
    """The exact contract shape, every key present, all zeros."""
    return {
        "ts": 0.0,
        "connected": False,
        "engine": {"running": False, "strategy": "", "strategy_title": "", "poll_ms": 1000,
                   "magic": 0, "last_intent": "", "last_reason": "", "iterations": 0},
        "account": {"login": 0, "server": "", "currency": "USD", "balance": 0.0, "equity": 0.0,
                    "margin": 0.0, "margin_free": 0.0, "margin_level": 0.0, "leverage": 0,
                    "is_real": False, "trade_mode": "DEMO"},
        "symbol": {"name": "", "bid": 0.0, "ask": 0.0, "spread_usd": 0.0},
        "basket": {"open": False, "side": "", "layers": 0, "lots": 0.0, "profit": 0.0,
                   "avg_price": 0.0, "tp_target": 0.0, "positions": []},
        "risk": {"level": "safe", "margin_level": 0.0, "stop_out_distance_pct": 0.0,
                 "drawdown_pct": 0.0, "daily_pl_pct": 0.0, "reasons": [],
                 "allow_open": True, "force_close": False, "kill": False,
                 "peak_equity": 0.0, "so_call": 0.0, "so_so": 0.0},
        "stats": {"baskets_closed": 0, "wins": 0, "losses": 0, "net_pl": 0.0, "win_rate": 0.0,
                  "peak_equity": 0.0, "start_equity": 0.0},
        "equity_curve": [],
        "activity": [],
        "notice": "",
        # how the last connection attempt went — present in every snapshot, so
        # the UI can bind to them without checking whether we are connected
        "mt5_stage": "",
        "mt5_error": "",
        "connecting": False,
    }


class TradingEngine:
    """Polls the broker, applies risk, runs one strategy, persists everything."""

    def __init__(self, config: dict, bridge, risk, store, persona,
                 logger: Optional[logging.Logger] = None) -> None:
        self.config = config or {}
        self.bridge = bridge
        self.risk = risk
        self.store = store
        self.persona = persona
        self.log = logger or logging.getLogger("infinity.engine")

        self._lock = threading.RLock()
        self._stop_evt = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._running = False           # trading enabled?
        self._snapshot = _empty_snapshot()
        self._activity: list = []
        self._notice = ""
        self._iterations = 0
        self._backoff = 0.0
        self._last_connect_try = 0.0
        #: seconds to wait before the next reconnect attempt. It grows because
        #: mt5.initialize() holds the GIL while it runs — retrying hard would
        #: freeze the UI in a loop when the terminal is simply not there.
        self._connect_wait = RECONNECT_EVERY
        #: set while an attempt is actually in flight on the poll thread, so a
        #: second click cannot queue another one behind the bridge lock
        self._connecting = threading.Event()
        #: when True the engine never calls bridge.connect() (run.py --no-connect,
        #: or the user pressing disconnect)
        self.offline = False
        self._last_equity_write = 0.0
        self._last_candle_fetch = 0.0
        self._candles: list = []
        #: Raw tick history for a strategy that sets ``wants_ticks``. Unlike the
        #: candles above this is never throttled and never held over from an
        #: empty read: the newest tick is the entire point of having it, and a
        #: buffer left over from two seconds ago would let a strategy compute a
        #: confident velocity out of ticks that had stopped arriving.
        self._ticks: list = []
        self._basket_id = 0
        self._basket_open_balance = 0.0
        self._basket_seen = False
        self._last_basket: dict = {}
        self._last_intent = ""
        self._last_reason = ""
        #: when True, begin() does not spawn the poll thread — the caller ticks manually
        self.manual = False

        engine_cfg = dict(self.config.get("engine") or {})
        self.magic = int(_f(engine_cfg.get("magic"), 100100))
        self.poll_s = max(0.2, _f(engine_cfg.get("poll_ms"), 1000.0) / 1000.0)
        self._strategy = build(str(engine_cfg.get("active_strategy") or "recovery_grid"),
                               self._params(engine_cfg.get("active_strategy")), self.log)
        self.log.info("engine ready: strategy=%s magic=%s poll=%.1fs",
                      self._strategy.key, self.magic, self.poll_s)

    # ------------------------------------------------------------------ #
    # helpers
    # ------------------------------------------------------------------ #

    def _params(self, key) -> dict:
        return dict((self.config.get("strategies") or {}).get(str(key or "")) or {})

    def _note(self, kind: str, message: str, payload: dict = None, level: int = logging.INFO) -> None:
        """Log it, store it, and put it on the UI activity feed."""
        text = str(message or "").strip()
        if not text:
            return
        self.log.log(level, text)
        try:
            self.store.log_event(kind, text, payload or {})
        except Exception as exc:  # noqa: BLE001
            self.log.debug("event persist failed: %s", exc)
        with self._lock:
            self._activity.insert(0, {"ts": time.time(), "kind": kind, "text": text})
            del self._activity[ACTIVITY_MAX:]

    def _narrate(self, event: dict, kind: str = "trade", level: int = logging.INFO) -> None:
        try:
            text = self.persona.narrate(event)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("narrate failed: %s", exc)
            text = str(event.get("reason") or "")
        self._note(kind, text, event, level)

    def _timeframe(self) -> str:
        p = self._strategy.params
        return str(p.get("timeframe") or p.get("momentum_tf") or "M5")

    def _bars_needed(self) -> int:
        p = self._strategy.params
        wanted = max(
            int(_f(p.get("ema_slow"), 0)), int(_f(p.get("bb_period"), 0)),
            int(_f(p.get("momentum_period"), 0)), int(_f(p.get("adx_period"), 0)),
            int(_f(p.get("ema_fast"), 0)),
        )
        return max(120, min(400, wanted + 80))

    def _ticks_needed(self) -> int:
        """How many ticks to ask for, from the strategy's own params.

        Same idea as :meth:`_bars_needed`: the engine does not know what a
        strategy's buffer is for, so it asks the strategy. Clamped because the
        parameter arrives from the config API without type coercion — a caller
        who posts ``"600"`` or ``-1`` gets a sane window rather than an
        exception on the poll thread.
        """
        p = self._strategy.params
        want = int(_f(p.get("tick_buffer"), 0)) or 300
        return max(20, min(2000, want))

    def _tick_window_s(self) -> float:
        """How far back the tick request reaches, in seconds. 0 = bridge default."""
        p = self._strategy.params
        return max(0.0, min(900.0, _f(p.get("tick_window_s"), 0.0)))

    def _basket_view(self, positions: list, equity: float) -> dict:
        lots = sum(_f(p.get("volume")) for p in positions)
        profit = sum(_f(p.get("profit")) + _f(p.get("swap")) for p in positions)
        avg = (sum(_f(p.get("price_open")) * _f(p.get("volume")) for p in positions) / lots
               if lots > 0 else 0.0)
        side = str(positions[0].get("type") or "") if positions else ""
        target = 0.0
        try:
            if hasattr(self._strategy, "tp_target"):
                target = _f(self._strategy.tp_target(equity))
        except Exception:  # noqa: BLE001
            target = 0.0
        return {
            "open": bool(positions),
            "side": side,
            "layers": len(positions),
            "lots": round(lots, 2),
            "profit": round(profit, 2),
            "avg_price": round(avg, 5),
            "tp_target": round(target, 2),
            "positions": positions,
        }

    # ------------------------------------------------------------------ #
    # public control
    # ------------------------------------------------------------------ #

    def begin(self) -> None:
        """Start the polling thread. Trading stays off until start()."""
        if self.manual:  # tests and backtests drive _tick() themselves
            return
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            self._stop_evt.clear()
            self._thread = threading.Thread(target=self._loop, name="infinity-engine",
                                            daemon=True)
            self._thread.start()
        self.log.info("engine thread started")

    def shutdown(self) -> None:
        self._stop_evt.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=max(3.0, self.poll_s * 3))
        with self._lock:
            self._running = False
        self.log.info("engine thread stopped")

    def start(self) -> dict:
        if not self.bridge.is_connected():
            return {"ok": False, "running": False,
                    "message": "Cannot deploy: MetaTrader 5 is not connected."}
        is_blocked, block_reason = as_block(self.bridge.blocked())
        if is_blocked:
            return {"ok": False, "running": False,
                    "message": block_reason or "Trading is blocked on this account."}
        if bool((self.config.get("risk") or {}).get("kill_switch")):
            return {"ok": False, "running": False,
                    "message": "Kill switch is on. Turn it off before deploying."}
        with self._lock:
            already = self._running
            self._running = True
            self._backoff = 0.0
        self.begin()
        if not already:
            self._note("engine", "Deployed %s on %s." % (
                self._strategy.title, self.bridge.symbol_name or "the symbol"))
        return {"ok": True, "running": True,
                "message": "Automation live on %s." % self._strategy.title}

    def stop(self) -> dict:
        with self._lock:
            was = self._running
            self._running = False
        if was:
            self._note("engine", "Automation stopped. Open positions were left untouched.")
        return {"ok": True, "running": False, "message": "Automation stopped."}

    def running(self) -> bool:
        """True while automation is allowed to open and manage positions."""
        with self._lock:
            return self._running

    # -- the broker link ------------------------------------------------- #
    #
    # Neither of these calls the bridge on the caller's thread. bridge.connect()
    # holds the bridge lock for its whole timeout — up to 20 seconds measured,
    # because mt5.initialize() is a native call that keeps the GIL — so running
    # it from an HTTP handler freezes the interface, the SSE stream and every
    # other request along with it. The click only *asks*; the poll thread does
    # the work on its next tick and the UI watches mt5_stage / notice for the
    # answer.

    def request_connect(self) -> dict:
        """Ask the poll thread to connect. Returns at once, never blocks."""
        if self.bridge.is_connected():
            return {"ok": True, "connected": True, "pending": False,
                    "message": "Already connected to MetaTrader 5."}
        self.offline = False
        # clear the escalating backoff for this one attempt — an explicit click
        # means now. _connect_wait itself is left alone: zeroing it made
        # min(300, wait * 1.8) collapse to zero forever, so a missing terminal
        # was then retried on every single tick.
        self._last_connect_try = 0.0
        with self._lock:
            self._notice = "Connecting to MetaTrader 5…"
        if self._connecting.is_set():
            return {"ok": True, "connected": False, "pending": True,
                    "message": "Already connecting — give it a moment."}
        self.begin()
        if self.manual:
            return {"ok": True, "connected": False, "pending": True,
                    "message": "Connect requested (manual mode: tick to run it)."}
        return {"ok": True, "connected": False, "pending": True,
                "message": "Connecting to MetaTrader 5…"}

    def connect_now(self) -> dict:
        """Connect on *this* thread and report what happened. Startup only.

        run.py calls this before the poll thread exists, so the banner can say
        whether the broker actually answered. It also stamps the attempt, so a
        failure at startup is not retried one second later — that would be a
        second GIL stall on top of the first. Everything after startup goes
        through request_connect().
        """
        self.offline = False
        self._last_connect_try = time.time()
        self._connecting.set()
        try:
            result = self.bridge.connect()
        finally:
            self._connecting.clear()
        if result.get("ok"):
            self._connect_wait = RECONNECT_EVERY
            self._last_equity_write = 0.0
        else:
            self._connect_wait = min(300.0, max(RECONNECT_EVERY, self._connect_wait) * 1.8)
            with self._lock:
                self._notice = str(result.get("message") or "Not connected.")
        return result

    def request_disconnect(self) -> dict:
        """Stop automation and drop the broker link — and stay dropped."""
        self.stop()
        self.offline = True          # without this the next tick reconnects
        self._connect_wait = RECONNECT_EVERY
        self._last_connect_try = 0.0
        if self._connecting.is_set():
            # an attempt holds the bridge lock; the next tick tears the link
            # down instead of blocking this request behind it
            with self._lock:
                self._notice = "Cancelling the connection attempt…"
            return {"ok": True, "connected": False, "pending": True,
                    "message": "Cancelling the connection attempt…"}
        try:
            self.bridge.disconnect()
        except Exception as exc:  # noqa: BLE001
            self.log.debug("disconnect: %s", exc)
        self._note("connect", "Disconnected from MetaTrader 5 by hand.")
        with self._lock:
            self._notice = "Not connected. Press LINK BROKER when you are ready."
        return {"ok": True, "connected": False, "message": "Disconnected."}

    def set_strategy(self, key: str) -> dict:
        key = str(key or "").strip()
        if key not in REGISTRY:
            return {"ok": False, "message": "Unknown strategy '%s'. Installed: %s"
                    % (key, ", ".join(strategy_keys()))}
        with self._lock:
            if self._basket_seen:
                return {"ok": False, "message":
                        "A basket is still open. Close it before switching strategy."}
            self._strategy = build(key, self._params(key), self.log)
            self.config.setdefault("engine", {})["active_strategy"] = key
            self._candles = []
            self._ticks = []
            self._last_candle_fetch = 0.0
        self._note("engine", "Strategy switched to %s (class %s)."
                   % (self._strategy.title, self._strategy.klass))
        return {"ok": True, "strategy": key, "title": self._strategy.title,
                "message": "%s selected." % self._strategy.title}

    def close_all_now(self) -> dict:
        if not self.bridge.is_connected():
            return {"ok": False, "message": "Not connected to MetaTrader 5."}
        with self._lock:
            basket = dict(self._last_basket)
        result = self.bridge.close_all(self.magic)
        # Per CONTRACT.md the bridge answers with `tickets` and `comment`; reading
        # `closed`/`message` instead meant a successful flatten counted 0 positions,
        # skipped the narration, and told the user "Nothing was open."
        closed = len(result.get("tickets") or [])
        if result.get("ok") and closed:
            self._narrate({"kind": "close", "profit": _f(basket.get("profit")),
                           "layers": int(_f(basket.get("layers"))) or closed,
                           "currency": self._snapshot["account"].get("currency", "USD"),
                           "reason": "Closed by hand."})
            return {"ok": True, "closed": closed,
                    "message": "Flattened %d position%s." % (closed, "" if closed == 1 else "s")}
        if result.get("ok"):
            return {"ok": True, "closed": 0, "message": "Nothing was open."}
        self._note("risk", "Flatten failed: %s" % _close_msg(result), result, logging.ERROR)
        return {"ok": False, "closed": closed, "message": _close_msg(result)}

    def status(self) -> dict:
        with self._lock:
            snap = copy.deepcopy(self._snapshot)
            snap["engine"]["running"] = self._running
            snap["activity"] = list(self._activity[:20])
            if self._notice:
                snap["notice"] = self._notice
        return snap

    # ------------------------------------------------------------------ #
    # the loop
    # ------------------------------------------------------------------ #

    def _loop(self) -> None:
        try:
            self.risk.seed_peak(self.store.peak_equity())
        except Exception as exc:  # noqa: BLE001
            self.log.debug("peak seed failed: %s", exc)
        while not self._stop_evt.is_set():
            started = time.time()
            try:
                self._tick()
                self._backoff = 0.0
            except Exception as exc:  # noqa: BLE001
                self._backoff = min(MAX_BACKOFF, max(self.poll_s, self._backoff * 2 or 1.0))
                self.log.exception("engine tick failed, backing off %.0fs: %s", self._backoff, exc)
                with self._lock:
                    self._notice = ("The engine hit an error and is retrying in %.0fs: %s"
                                    % (self._backoff, exc))
            wait = self._backoff or max(0.05, self.poll_s - (time.time() - started))
            self._stop_evt.wait(wait)

    def _tick(self) -> None:
        self._iterations += 1
        now = time.time()

        # --- connection ------------------------------------------------ #
        if self.offline and self.bridge.is_connected():
            # request_disconnect() asked for this but would not block on the
            # bridge lock to get it, so the teardown lands here instead
            try:
                self.bridge.disconnect()
            except Exception as exc:  # noqa: BLE001
                self.log.debug("disconnect: %s", exc)
            self._note("connect", "Link to MetaTrader 5 closed.")

        if not self.bridge.is_connected():
            if self.offline:
                with self._lock:
                    self._notice = ("Not connected to MetaTrader 5. Press LINK BROKER "
                                    "to enter your account details and connect.")
            elif now - self._last_connect_try >= self._connect_wait:
                self._last_connect_try = now
                self._connecting.set()
                try:
                    result = self.bridge.connect()
                finally:
                    self._connecting.clear()
                if result.get("ok"):
                    self._connect_wait = RECONNECT_EVERY
                    self._note("connect", "Connected to %s account %s on %s." % (
                        (result.get("account") or {}).get("trade_mode", "?"),
                        (result.get("account") or {}).get("login", "?"),
                        (result.get("account") or {}).get("server", "?")))
                    self._last_equity_write = 0.0
                else:
                    # back off to at most 5 minutes so a missing terminal costs
                    # one short stall now and then instead of a constant one
                    self._connect_wait = min(300.0, max(RECONNECT_EVERY,
                                                        self._connect_wait) * 1.8)
                    with self._lock:
                        self._notice = str(result.get("message") or "Not connected.")
            self._publish_disconnected()
            return

        account = self.bridge.account()
        symbol = self.bridge.symbol_info()
        tick = self.bridge.tick()
        # If the book cannot be read, do nothing this cycle. Treating the failure
        # as a flat account would make _track_basket narrate a close that never
        # happened, write a phantom loss to the store, and re-open a second basket
        # row for the same live positions on the next tick.
        try:
            positions = self.bridge.positions(self.magic)
        except BridgeReadError as exc:
            self.log.warning("skipping tick: %s", exc)
            return
        equity = _f(account.get("equity"))
        currency = str(account.get("currency") or "USD")

        if now - self._last_candle_fetch >= CANDLE_EVERY or not self._candles:
            fetched = self.bridge.candles(self._timeframe(), self._bars_needed())
            if fetched:
                self._candles = fetched
            self._last_candle_fetch = now

        # Tick history, only for a strategy that says it reads it. Three guards,
        # each for a real case: ``wants_ticks`` so a candle strategy never pays
        # for a second history call per poll; ``callable`` so a bridge without
        # the method (any older fake, or a future replacement) degrades to an
        # empty buffer instead of an AttributeError on the poll thread; and the
        # ``except`` because this is the one read whose failure must not skip the
        # tick — positions still need managing when tick history is unavailable.
        # Reassigned unconditionally, so a strategy switch or a broker that stops
        # serving history is seen immediately rather than on the next success.
        self._ticks = []
        if getattr(self._strategy, "wants_ticks", False):
            fetch = getattr(self.bridge, "ticks", None)
            if callable(fetch):
                try:
                    self._ticks = fetch(self._ticks_needed(), self._tick_window_s()) or []
                except Exception as exc:  # noqa: BLE001
                    self.log.debug("tick history unavailable: %s", exc)
                    self._ticks = []

        basket = self._basket_view(positions, equity)

        # --- persistence ----------------------------------------------- #
        if equity and now - self._last_equity_write >= EQUITY_EVERY:
            self.store.record_equity(equity, _f(account.get("balance")),
                                     _f(account.get("margin_level")), ts=now)
            self._last_equity_write = now

        self._track_basket(basket, account, now)

        # --- risk BEFORE the strategy ---------------------------------- #
        try:
            day_start = self.store.day_start_equity() or _f(account.get("balance"))
        except Exception:  # noqa: BLE001
            day_start = _f(account.get("balance"))
        verdict = self.risk.evaluate(account, positions, day_start)

        try:
            stats = self.store.stats()
            curve = self.store.equity_curve(240)
        except Exception:  # noqa: BLE001
            stats, curve = {}, []

        notice = ""
        if verdict.get("force_close") and basket["open"]:
            reason = (verdict.get("reasons") or ["Risk limit reached."])[0]
            self._note("risk", "Risk override — flattening. %s" % reason, verdict, logging.WARNING)
            result = self.bridge.close_all(self.magic)
            if result.get("ok"):
                self._narrate({"kind": "close", "profit": basket["profit"],
                               "layers": basket["layers"], "currency": currency,
                               "reason": "Closed by risk control."})
                try:
                    positions = self.bridge.positions(self.magic)
                except BridgeReadError as exc:
                    # the close succeeded; only the confirming read failed, so leave
                    # the basket row to the next tick rather than guessing at it
                    self.log.warning("post-close read failed: %s", exc)
                else:
                    basket = self._basket_view(positions, equity)
                    self._track_basket(basket, self.bridge.account(), time.time())
            else:
                self._note("risk", "Risk close failed: %s" % _close_msg(result),
                           result, logging.ERROR)
            notice = reason
        elif verdict.get("reasons") and verdict.get("level") in ("danger", "critical"):
            notice = verdict["reasons"][0]

        if verdict.get("kill") and self._running:
            with self._lock:
                self._running = False
            self._note("engine", "Kill switch engaged — automation stopped.", level=logging.WARNING)

        # --- strategy --------------------------------------------------- #
        intent_action, intent_reason = "idle", "Automation is off."
        if self._running:
            ctx = Ctx(account=account, symbol=symbol, tick=tick, positions=positions,
                      candles=self._candles, ticks=self._ticks, basket=basket,
                      params=self._strategy.params,
                      risk=dict(self.config.get("risk") or {}), now=now)
            # Manage what is already open before deciding anything new. A stop that
            # should have moved to break-even matters more than another entry, and
            # doing it first means a tick that ends in an error still got the
            # protection in.
            if positions:
                self._manage(ctx)
            try:
                intent = self._strategy.decide(ctx)
            except Exception as exc:  # noqa: BLE001
                self.log.exception("strategy %s raised: %s", self._strategy.key, exc)
                self._note("engine", "%s errored and was skipped this tick: %s"
                           % (self._strategy.title, exc), level=logging.ERROR)
                intent = None
            if intent is not None:
                intent_action, intent_reason = intent.action, intent.reason
                executed = self._execute(intent, verdict, account, symbol, basket, now)
                if executed:
                    positions = self.bridge.positions(self.magic)
                    basket = self._basket_view(positions, _f(account.get("equity")))
                    self._track_basket(basket, self.bridge.account(), time.time())

        self._publish(account, symbol, tick, basket, verdict, stats, curve, notice,
                      intent_action, intent_reason)

    # ------------------------------------------------------------------ #
    # basket bookkeeping
    # ------------------------------------------------------------------ #

    def _track_basket(self, basket: dict, account: dict, now: float) -> None:
        """Open a store row when a basket appears, close it when it disappears."""
        balance = _f(account.get("balance"))
        if basket["open"] and not self._basket_seen:
            self._basket_seen = True
            self._basket_open_balance = balance
            try:
                self._basket_id = self.store.open_basket(
                    self.bridge.symbol_name or "", basket["side"], self._strategy.key,
                    basket["avg_price"], ts=now)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("open_basket failed: %s", exc)
                self._basket_id = 0
        elif basket["open"] and self._basket_id:
            # Volume can change without the layer count changing — on a netting
            # account it always does, since every add merges into the one position.
            # Keying the write on layers alone left `lots` frozen at the first
            # fill, which then understates every closed-basket record the roster's
            # expectancy figures are computed from.
            if (int(_f(self._last_basket.get("layers"))) != basket["layers"]
                    or abs(_f(self._last_basket.get("lots")) - basket["lots"]) > 1e-9):
                self.store.update_basket(self._basket_id, layers=basket["layers"],
                                         lots=basket["lots"], entry_avg=basket["avg_price"])
        elif not basket["open"] and self._basket_seen:
            previous = dict(self._last_basket)
            realized = balance - self._basket_open_balance
            if abs(realized) < 1e-9:
                realized = _f(previous.get("profit"))
            if self._basket_id:
                self.store.close_basket(
                    self._basket_id, profit=realized,
                    exit_price=_f(self._snapshot.get("symbol", {}).get("bid")),
                    reason=self._last_reason or "closed",
                    layers=int(_f(previous.get("layers"))), lots=_f(previous.get("lots")), ts=now)
            self._narrate({"kind": "close", "profit": realized,
                           "layers": int(_f(previous.get("layers"))) or 1,
                           "currency": str(account.get("currency") or "USD"),
                           "reason": ""})
            try:
                self._strategy.on_basket_closed(now, realized)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("on_basket_closed failed: %s", exc)
            self._basket_seen = False
            self._basket_id = 0
            self._basket_open_balance = 0.0
        self._last_basket = {k: basket[k] for k in
                             ("open", "side", "layers", "lots", "profit", "avg_price")}

    # ------------------------------------------------------------------ #
    # execution
    # ------------------------------------------------------------------ #

    def _manage(self, ctx) -> None:
        """Apply the strategy's bracket moves to the open positions.

        Runs every tick, so it is deliberately quiet: only a move the broker
        accepted is narrated, and a rejection is a warning rather than an error
        because the position still has whatever stop it had before. The one case
        worth shouting about — an order that ended up with no broker-side stop at
        all — is reported by the bridge at the point it happens.

        A strategy raising in here must not cost the tick its entry decision, so
        the whole hook is wrapped: management is an improvement on the trade, not a
        precondition for it.
        """
        try:
            moves = self._strategy.manage(ctx) or []
        except Exception as exc:  # noqa: BLE001
            self.log.exception("%s.manage raised: %s", self._strategy.key, exc)
            return
        if not moves:
            return
        modify = getattr(self.bridge, "modify_stops", None)
        if not callable(modify):
            return
        for move in moves:
            ticket = _i(getattr(move, "ticket", 0))
            if ticket <= 0:
                continue
            try:
                result = modify(ticket, _f(getattr(move, "sl", 0.0)),
                                _f(getattr(move, "tp", 0.0)))
            except Exception as exc:  # noqa: BLE001
                self.log.warning("modify_stops(%s) raised: %s", ticket, exc)
                continue
            if result.get("ok"):
                self._note("trade", "%s — stop now %.2f on #%d."
                           % (getattr(move, "reason", None) or "Bracket moved",
                              _f(result.get("sl")), ticket),
                           {"ticket": ticket, "sl": result.get("sl"),
                            "tp": result.get("tp")})
            elif _i(result.get("retcode"), 0) != 0:
                # retcode 0 is the bridge's own "already at that level", which is
                # the normal answer on most ticks and not worth a line.
                self.log.warning("bracket move on %d refused: %s", ticket,
                                 result.get("comment"))

    def _execute(self, intent, verdict: dict, account: dict, symbol: dict,
                 basket: dict, now: float) -> bool:
        action = intent.action
        currency = str(account.get("currency") or "USD")

        if action == "close_basket":
            if not basket["open"]:
                return False
            result = self.bridge.close_all(self.magic)
            if result.get("ok"):
                self._last_reason = intent.reason or "target"
                return True
            self._note("engine", "Close failed: %s" % _close_msg(result),
                       result, logging.ERROR)
            return False

        if action not in ("open", "add_layer"):
            return False

        if not verdict.get("allow_open", True):
            self._last_reason = (verdict.get("reasons") or ["Risk control is holding entries."])[0]
            return False

        volume = _f(intent.volume)
        cap = -1.0            # -1 means unmeasured; 0.0 means measured at no room
        try:
            probe = dict(symbol)
            probe["_open_lots"] = basket["lots"]
            cap = self.risk.lots_allowed(account, probe, self.config)
        except Exception as exc:  # noqa: BLE001
            self.log.debug("lots_allowed failed: %s", exc)
            cap = -1.0
        if cap == 0.0:
            # the add with no margin behind it: previously this read the same as
            # "no cap information" and went through at full size, and the stack it
            # created was force-closed by the margin floor on a later tick
            self._last_reason = "No margin headroom left for another entry."
            self._note("risk", self._last_reason, {"wanted": volume}, logging.WARNING)
            return False
        if cap > 0 and volume > cap:
            self._note("risk", "Trimmed %.2f lots to %.2f — that is all the margin allows."
                       % (volume, cap), {"wanted": volume, "allowed": cap}, logging.WARNING)
            volume = cap
        vmin = _f(symbol.get("volume_min"), 0.01) or 0.01
        if volume < vmin:
            self._last_reason = ("Margin only allows %.2f lots, below the %.2f minimum."
                                 % (volume, vmin))
            return False

        result = self.bridge.market_order(intent.side, volume,
                                          "INFINITY %s" % self._strategy.key,
                                          sl_distance=_f(getattr(intent, "sl_distance", 0.0)),
                                          tp_distance=_f(getattr(intent, "tp_distance", 0.0)))
        if not result.get("ok"):
            self._note("engine", "Order rejected: %s" % _close_msg(result),
                       result, logging.ERROR)
            self._last_reason = _close_msg(result)
            return False

        price = _f(result.get("price"))
        filled = _f(result.get("volume"), volume)
        tickets = result.get("tickets") or []
        self._last_reason = intent.reason or action
        if not self._basket_seen:
            self._basket_seen = True
            self._basket_open_balance = _f(account.get("balance"))
            try:
                self._basket_id = self.store.open_basket(
                    self.bridge.symbol_name or "", intent.side, self._strategy.key, price, ts=now)
            except Exception as exc:  # noqa: BLE001
                self.log.debug("open_basket failed: %s", exc)
        for ticket in tickets or [0]:
            self.store.add_fill(self._basket_id, int(_f(ticket)), intent.side,
                                filled / max(1, len(tickets or [0])), price,
                                "INFINITY %s" % self._strategy.key, ts=now)
        self._narrate({
            "kind": "layer" if action == "add_layer" else "open",
            "side": intent.side, "volume": filled, "price": price,
            "layers": basket["layers"] + 1, "currency": currency,
            "reason": intent.reason,
        })
        return True

    # ------------------------------------------------------------------ #
    # publishing
    # ------------------------------------------------------------------ #

    def _publish_disconnected(self) -> None:
        snap = _empty_snapshot()
        try:
            stats = self.store.stats()
            curve = self.store.equity_curve(240)
        except Exception:  # noqa: BLE001
            stats, curve = {}, []
        snap["ts"] = time.time()
        snap["connected"] = False
        snap["engine"].update({
            "running": self._running, "strategy": self._strategy.key,
            "strategy_title": self._strategy.title, "poll_ms": int(self.poll_s * 1000),
            "magic": self.magic, "last_intent": "offline",
            "last_reason": "Waiting for MetaTrader 5.", "iterations": self._iterations,
        })
        snap["stats"].update({k: stats.get(k, snap["stats"][k]) for k in snap["stats"]})
        snap["equity_curve"] = curve
        snap["risk"]["reasons"] = ["No broker connection — nothing is at risk."]
        # the kill switch is a config setting, not a broker fact: dropping it here
        # left the UI showing OFF while it was on, so it could be armed and never
        # disarmed once the connection went away
        snap["risk"]["kill"] = bool((self.config.get("risk") or {}).get("kill_switch"))
        snap["risk"]["allow_open"] = False
        snap["notice"] = self._notice or "MetaTrader 5 is not connected."
        snap["mt5_stage"] = getattr(self.bridge, "last_stage", "")
        snap["mt5_error"] = getattr(self.bridge, "last_error", "")
        snap["connecting"] = self._connecting.is_set()
        with self._lock:
            snap["activity"] = list(self._activity[:20])
            self._snapshot = snap

    def _publish(self, account: dict, symbol: dict, tick: dict, basket: dict, verdict: dict,
                 stats: dict, curve: list, notice: str, intent_action: str,
                 intent_reason: str) -> None:
        snap = _empty_snapshot()
        snap["ts"] = time.time()
        snap["connected"] = True
        snap["engine"].update({
            "running": self._running, "strategy": self._strategy.key,
            "strategy_title": self._strategy.title, "poll_ms": int(self.poll_s * 1000),
            "magic": self.magic, "last_intent": intent_action, "last_reason": intent_reason,
            "iterations": self._iterations,
        })
        for key in snap["account"]:
            if key in account:
                snap["account"][key] = account[key]
        snap["account"]["trade_mode"] = str(account.get("trade_mode") or "DEMO")
        snap["symbol"].update({
            "name": str(symbol.get("name") or self.bridge.symbol_name or ""),
            "bid": _f(tick.get("bid")) or _f(symbol.get("bid")),
            "ask": _f(tick.get("ask")) or _f(symbol.get("ask")),
            "spread_usd": _f(tick.get("spread_usd")) or _f(symbol.get("spread_usd")),
            "digits": symbol.get("digits", 2),
            "volume_min": symbol.get("volume_min", 0.01),
            "volume_max": symbol.get("volume_max", 100.0),
        })
        snap["basket"].update(basket)
        for key in snap["risk"]:
            if key in verdict:
                snap["risk"][key] = verdict[key]
        for key in snap["stats"]:
            if key in stats:
                snap["stats"][key] = stats[key]
        snap["equity_curve"] = curve
        snap["notice"] = notice
        snap["mt5_stage"] = getattr(self.bridge, "last_stage", "")
        snap["mt5_error"] = getattr(self.bridge, "last_error", "")
        with self._lock:
            snap["activity"] = list(self._activity[:20])
            self._notice = notice
            self._snapshot = snap

