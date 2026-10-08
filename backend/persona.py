"""AIPersona — the voice of INFINITY.100 AI.

Deterministic templates over real state. No external model, no network: the
"intelligence" is that it actually knows the account, the session, yesterday's
result and what the market just did, and says so in plain English.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

# session windows in UTC hours, [open, close)
SESSIONS = {
    "Sydney": (21, 6),
    "Tokyo": (0, 9),
    "London": (7, 16),
    "New York": (12, 21),
}


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _money(value: float, currency: str = "USD") -> str:
    symbol = {"USD": "$", "EUR": "€", "GBP": "£"}.get(str(currency).upper(), "")
    value = _f(value)
    text = "{:,.2f}".format(abs(value))
    sign = "-" if value < 0 else ""
    return "%s%s%s" % (sign, symbol, text) if symbol else "%s%s %s" % (sign, text, currency)


def open_sessions(now_utc: Optional[time.struct_time] = None) -> list:
    now_utc = now_utc or time.gmtime()
    hour = now_utc.tm_hour
    live = []
    for name, (start, end) in SESSIONS.items():
        if start <= end:
            if start <= hour < end:
                live.append(name)
        else:  # wraps midnight
            if hour >= start or hour < end:
                live.append(name)
    return live


def market_phrase(candles: list, tick: dict = None) -> str:
    """One clause describing what price is doing, from candles alone."""
    closes = [_f(c.get("close")) for c in (candles or []) if c]
    highs = [_f(c.get("high")) for c in (candles or []) if c]
    lows = [_f(c.get("low")) for c in (candles or []) if c]
    if len(closes) < 12:
        return "is quiet — not enough history loaded yet to read it"

    window = closes[-40:] if len(closes) >= 40 else closes
    hi = max(highs[-len(window):]) if highs else max(window)
    lo = min(lows[-len(window):]) if lows else min(window)
    last = closes[-1]
    span = hi - lo
    if span <= 0:
        return "is flat"

    position = (last - lo) / span  # 0 at the low, 1 at the high
    drift = last - window[0]
    recent = closes[-10:]
    recent_span = max(recent) - min(recent)
    tight = recent_span < span * 0.25

    if position >= 0.9:
        return "is pressing into resistance at %.2f" % hi
    if position <= 0.1:
        return "is sitting on support at %.2f" % lo
    if tight and position >= 0.6:
        return "is consolidating below resistance at %.2f" % hi
    if tight and position <= 0.4:
        return "is coiling above support at %.2f" % lo
    if tight:
        return "is ranging between %.2f and %.2f" % (lo, hi)
    if drift > span * 0.3:
        return "is trending higher, %.2f above where this window opened" % abs(drift)
    if drift < -span * 0.3:
        return "is trending lower, %.2f below where this window opened" % abs(drift)
    return "is two-way between %.2f and %.2f" % (lo, hi)


class AIPersona:
    """Builds briefings, narration and replies from the live status snapshot."""

    def __init__(self, config: dict, store=None, logger: Optional[logging.Logger] = None) -> None:
        self.config = config or {}
        self.store = store
        self.log = logger or logging.getLogger("infinity.persona")

    # ------------------------------------------------------------------ #

    def _user(self) -> dict:
        return dict(self.config.get("user") or {})

    def name(self) -> str:
        return str(self._user().get("name") or "trader").strip() or "trader"

    def _instrument(self, ctx: dict) -> str:
        fav = str(self._user().get("favourite_symbol") or "").strip()
        live = str((ctx.get("symbol") or {}).get("name") or "").strip()
        if fav:
            return fav
        if live:
            return live
        return "Gold"

    def suggestions(self, ctx: dict = None) -> list:
        ctx = ctx or {}
        running = bool((ctx.get("engine") or {}).get("running"))
        return [
            {"label": "Scan for new opportunities", "message": "Scan for new opportunities"},
            {"label": "Pause automation" if running else "Continue automation",
             "message": "Pause automation" if running else "Continue automation"},
            {"label": "Explain today's market", "message": "Explain today's market"},
            {"label": "Review yesterday's trades", "message": "Review yesterday's trades"},
        ]

    # ------------------------------------------------------------------ #

    def briefing(self, ctx: dict = None) -> dict:
        """The personalised welcome. {greeting, lines[], suggestions[]}"""
        ctx = ctx or {}
        user = self._user()
        account = dict(ctx.get("account") or {})
        symbol = dict(ctx.get("symbol") or {})
        risk = dict(ctx.get("risk") or {})
        basket = dict(ctx.get("basket") or {})
        engine = dict(ctx.get("engine") or {})
        connected = bool(ctx.get("connected"))
        instrument = self._instrument(ctx)
        currency = str(account.get("currency") or "USD")

        greeting = "Hi %s. Welcome back to INFINITY.100 AI." % self.name()
        lines: list = []

        # 1. what it knows about how you trade
        sessions = [str(s) for s in (user.get("sessions") or []) if str(s).strip()]
        if sessions:
            live_now = open_sessions()
            joined = " and ".join(sessions) if len(sessions) <= 2 else \
                ", ".join(sessions[:-1]) + " and " + sessions[-1]
            sentence = "I noticed you usually trade %s during the %s sessions." % (instrument, joined)
            overlap = [s for s in sessions if s in live_now]
            if len(overlap) > 1:
                sentence += " Both are open right now."
            elif overlap:
                sentence += " %s is open right now." % overlap[0]
            elif live_now:
                sentence += " Neither is open yet — %s has the book." % " and ".join(live_now)
            else:
                sentence += " The market is between sessions at the moment."
            lines.append(sentence)

        profile = str(user.get("risk_profile") or "").strip()
        if profile:
            lines.append("Your preferred risk profile is %s." % profile)

        # 2. what happened yesterday
        lines.append(self._yesterday_line(currency))

        # 3. what the market is doing
        if connected and symbol.get("name"):
            phrase = market_phrase(ctx.get("candles") or [], ctx.get("tick") or {})
            bid = _f(symbol.get("bid"))
            if bid:
                lines.append("%s %s, trading at %.2f." % (instrument, phrase, bid))
            else:
                lines.append("%s %s." % (instrument, phrase))
        else:
            lines.append(
                "I am not connected to MetaTrader 5 yet, so I cannot read the %s book. %s"
                % (instrument, self._connect_hint(ctx))
            )

        # 4. what is live right now
        if basket.get("open"):
            lines.append(
                "You currently have %d %s %s in one basket, %s floating."
                % (int(_f(basket.get("layers"))),
                   str(basket.get("side") or "").upper(),
                   "layer" if int(_f(basket.get("layers"))) == 1 else "layers",
                   _money(basket.get("profit"), currency))
            )
        if engine.get("running"):
            lines.append("Automation is live on %s." % self._strategy_title(engine.get("strategy")))
        elif connected:
            lines.append("Automation is idle — nothing will be traded until you deploy.")

        if risk.get("level") in ("danger", "critical") and risk.get("reasons"):
            lines.append("Heads up: %s" % str(risk["reasons"][0]))

        lines.append("Would you like me to:")
        return {
            "greeting": greeting,
            "lines": lines,
            "suggestions": self.suggestions(ctx),
        }

    def _connect_hint(self, ctx: dict) -> str:
        notice = str(ctx.get("notice") or "").strip()
        if notice:
            return notice
        return ("Open MetaTrader 5, log in to your account, then press the connection "
                "indicator at the top right.")

    def _strategy_title(self, key: str) -> str:
        try:
            from backend.strategies import REGISTRY

            cls = REGISTRY.get(str(key or ""))
            if cls is not None:
                return cls.title
        except Exception:
            pass
        return str(key or "no strategy").replace("_", " ").upper()

    def _strategy_keys(self) -> list:
        try:
            from backend.strategies import keys

            return list(keys())
        except Exception:
            return list((self.config.get("strategies") or {}).keys())

    def _yesterday_line(self, currency: str = "USD") -> str:
        summary = {}
        if self.store is not None:
            try:
                summary = self.store.yesterday_summary() or {}
            except Exception as exc:  # noqa: BLE001
                self.log.debug("yesterday_summary failed: %s", exc)
                summary = {}
        count = int(_f(summary.get("count")))
        if not count:
            total = {}
            if self.store is not None:
                try:
                    total = self.store.stats() or {}
                except Exception:
                    total = {}
            closed = int(_f(total.get("baskets_closed")))
            if closed:
                return ("Nothing closed yesterday. Across the whole record the AI has closed "
                        "%d %s for %s, a %.0f%% win rate."
                        % (closed, "basket" if closed == 1 else "baskets",
                           _money(total.get("net_pl"), currency), _f(total.get("win_rate"))))
            return "This is a fresh install, so there is no trading history to review yet."

        words = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six",
                 7: "seven", 8: "eight", 9: "nine", 10: "ten"}
        spelled = words.get(count, str(count))
        profit = _f(summary.get("profit"))
        pct = _f(summary.get("pct"))
        direction = "gain" if profit >= 0 else "loss"
        line = "Yesterday the AI closed %s %s for a total %s of %s" % (
            spelled, "position" if count == 1 else "positions", direction,
            _money(abs(profit), currency))
        if pct:
            line += " (%.1f%%)" % abs(pct)
        line += "."
        wins = int(_f(summary.get("wins")))
        if count > 1:
            line += " %d of %d closed green." % (wins, count)
        return line

    # ------------------------------------------------------------------ #

    def narrate(self, event: dict) -> str:
        """One sentence for the activity feed."""
        event = event or {}
        kind = str(event.get("kind") or "").lower()
        currency = str(event.get("currency") or "USD")
        side = str(event.get("side") or "").upper()
        volume = _f(event.get("volume"))
        price = _f(event.get("price"))
        profit = _f(event.get("profit"))
        layers = int(_f(event.get("layers")))
        reason = str(event.get("reason") or "").strip()

        if kind == "open":
            text = "Opened %s %.2f lots at %.2f." % (side, volume, price)
            return text + (" %s" % reason if reason else "")
        if kind == "layer":
            return ("Added layer %d — %s %.2f lots at %.2f. %s"
                    % (layers, side, volume, price, reason)).strip()
        if kind == "close":
            verdict = "Banked" if profit >= 0 else "Cut"
            text = "%s the basket for %s across %d %s." % (
                verdict, _money(profit, currency), layers,
                "layer" if layers == 1 else "layers")
            return text + (" %s" % reason if reason else "")
        if kind == "risk":
            return "Risk control stepped in: %s" % (reason or "limit reached.")
        if kind == "engine":
            return reason or "Engine state changed."
        if kind == "connect":
            return reason or "Broker connection changed."
        return reason or "Nothing to report."

    # ------------------------------------------------------------------ #

    def reply(self, message: str, ctx: dict = None) -> dict:
        """Keyword intent matching over the live snapshot."""
        ctx = ctx or {}
        text = str(message or "").strip()
        low = text.lower()
        account = dict(ctx.get("account") or {})
        symbol = dict(ctx.get("symbol") or {})
        risk = dict(ctx.get("risk") or {})
        basket = dict(ctx.get("basket") or {})
        engine = dict(ctx.get("engine") or {})
        stats = dict(ctx.get("stats") or {})
        currency = str(account.get("currency") or "USD")
        connected = bool(ctx.get("connected"))
        instrument = self._instrument(ctx)
        action = ""

        def has(*words) -> bool:
            return any(w in low for w in words)

        if not text:
            out = "I did not catch that. Ask me about the market, your risk, or the engine."

        # --- scan ------------------------------------------------------ #
        elif has("scan", "opportunit", "setup", "signal"):
            if not connected:
                out = ("I cannot scan without a broker feed. %s" % self._connect_hint(ctx))
            else:
                phrase = market_phrase(ctx.get("candles") or [])
                strategy = self._strategy_title(engine.get("strategy"))
                spread = _f(symbol.get("spread_usd"))
                out = ("Scanning %s: it %s. Spread is %.2f. %s is armed and would %s."
                       % (instrument, phrase, spread, strategy,
                          "add to the open basket" if basket.get("open") else
                          "open on the next qualifying tick"))
                if not risk.get("allow_open", True):
                    out += " Risk control is holding new entries back, though."
                elif not engine.get("running"):
                    out += " Deploy when you want me to act on it."
                    action = "engine.start"

        # --- flatten (checked before pause: "close everything" has no stop word) --- #
        elif has("flatten", "close all", "close everything", "close the basket",
                 "close my position", "get me out", "exit everything"):
            if basket.get("open"):
                out = ("That closes all %d open %s immediately at market, locking in %s. "
                       "Confirm and I will flatten the basket."
                       % (int(_f(basket.get("layers"))),
                          "position" if int(_f(basket.get("layers"))) == 1 else "positions",
                          _money(basket.get("profit"), currency)))
                action = "engine.close_all"
            else:
                out = "There is nothing open to close — you are already flat."

        # --- automation ------------------------------------------------ #
        elif has("continue automation", "start", "deploy", "resume", "go live"):
            if engine.get("running"):
                out = "Automation is already live on %s." % self._strategy_title(engine.get("strategy"))
            elif not connected:
                out = "I need a broker connection before I can deploy. %s" % self._connect_hint(ctx)
            else:
                out = ("Ready to deploy %s on %s. Say the word and I will start the engine."
                       % (self._strategy_title(engine.get("strategy")), instrument))
                action = "engine.start"
        elif has("pause", "stop", "halt", "shut"):
            if has("everything", "flatten", "close all", "close everything"):
                out = ("That closes every open position immediately. Confirm and I will flatten "
                       "the basket.")
                action = "engine.close_all"
            elif engine.get("running"):
                out = "I can stop the engine now. Open positions stay open unless you flatten too."
                action = "engine.stop"
            else:
                out = "Automation is already idle."

        # --- market ---------------------------------------------------- #
        elif has("explain", "market", "today", "what's happening", "whats happening"):
            if not connected:
                out = ("No live prices yet, so anything I said about the market would be "
                       "invented. %s" % self._connect_hint(ctx))
            else:
                phrase = market_phrase(ctx.get("candles") or [])
                live = open_sessions()
                out = ("%s %s. Bid %.2f / ask %.2f, spread %.2f. %s"
                       % (instrument, phrase, _f(symbol.get("bid")), _f(symbol.get("ask")),
                          _f(symbol.get("spread_usd")),
                          ("%s %s open." % (" and ".join(live), "is" if len(live) == 1 else "are"))
                          if live else "No major session is open right now."))

        # --- review ---------------------------------------------------- #
        elif has("review", "yesterday", "history", "past trade", "last trade"):
            out = self._yesterday_line(currency)
            closed = int(_f(stats.get("baskets_closed")))
            if closed:
                out += (" All time: %d closed, %d green, %d red, net %s."
                        % (closed, int(_f(stats.get("wins"))), int(_f(stats.get("losses"))),
                           _money(stats.get("net_pl"), currency)))
            if self.store is not None:
                try:
                    rows = self.store.recent_baskets(3)
                except Exception:
                    rows = []
                for r in rows:
                    out += ("\n· %s %d layers, %.2f lots -> %s (%s)"
                            % (str(r.get("side") or "").upper(), int(_f(r.get("layers"))),
                               _f(r.get("lots")), _money(r.get("profit"), currency),
                               str(r.get("reason") or "closed")))

        # --- risk ------------------------------------------------------ #
        elif has("risk", "margin", "safe", "stop out", "stop-out", "drawdown", "exposure"):
            level = str(risk.get("level") or "safe")
            if basket.get("open"):
                out = ("Risk is %s. Margin level %.2f%%, %.0f%% clear of the stop-out. "
                       "Drawdown %.2f%% from peak, %.2f%% on the day."
                       % (level.upper(), _f(risk.get("margin_level")),
                          _f(risk.get("stop_out_distance_pct")), _f(risk.get("drawdown_pct")),
                          _f(risk.get("daily_pl_pct"))))
            else:
                out = ("Risk is %s — you are flat, so there is no margin exposure. "
                       "Drawdown %.2f%% from peak."
                       % (level.upper(), _f(risk.get("drawdown_pct"))))
            for reason in (risk.get("reasons") or [])[:2]:
                out += "\n· %s" % reason

        # --- account --------------------------------------------------- #
        elif has("balance", "equity", "account", "how much", "profit", "p/l", "pnl"):
            if not connected:
                out = "No account is connected yet, so I have no balance to show you."
            else:
                out = ("Balance %s, equity %s, free margin %s. %s"
                       % (_money(account.get("balance"), currency),
                          _money(account.get("equity"), currency),
                          _money(account.get("margin_free"), currency),
                          ("Basket floating %s across %d layers."
                           % (_money(basket.get("profit"), currency),
                              int(_f(basket.get("layers")))))
                          if basket.get("open") else "Flat."))
                if account.get("is_real"):
                    out += " This is a REAL money account."

        # --- strategy -------------------------------------------------- #
        elif has("strategy", "strategies", "which bot", "module"):
            try:
                from backend.strategies import REGISTRY, keys

                names = ", ".join("%s (class %s)" % (REGISTRY[k].title, REGISTRY[k].klass)
                                  for k in keys())
            except Exception:
                names = "the installed modules"
            out = ("%s is selected. Installed: %s. Pick any card on the left to switch — "
                   "the engine reloads it without a restart."
                   % (self._strategy_title(engine.get("strategy")), names))

        # --- identity / help ------------------------------------------- #
        elif has("who are you", "what are you", "help", "what can you"):
            out = ("I am INFINITY.100 AI — the trading operating system running on your machine. "
                   "I hold %d strategy modules, watch your margin every second, and only ever "
                   "trade what you deploy. Nothing leaves this computer."
                   % len(self._strategy_keys()))
        else:
            out = ""

        if not out:
            out = ("I can scan %s for setups, explain what the market is doing, review your "
                   "closed baskets, report risk and margin, read your balance, switch strategy, "
                   "or start and stop automation. Ask me for any of those."
                   % instrument)

        return {"text": out, "suggestions": self.suggestions(ctx), "action": action}


# --------------------------------------------------------------------------- #
# self-test
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import os
    import sys
    import tempfile

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logging.basicConfig(level=logging.WARNING)

    from backend.config import load_config
    from backend.store import Store

    passed = failed = 0

    def check(label, cond, detail=""):
        global passed, failed
        if cond:
            passed += 1
            print("[ok  ] %s %s" % (label, detail))
        else:
            failed += 1
            print("[FAIL] %s %s" % (label, detail))

    print("=" * 72)
    print("INFINITY.100 AI - persona.py self-test")
    print("=" * 72)

    cfg = load_config()
    tmp = os.path.join(tempfile.gettempdir(), "infinity_persona_%d.db" % int(time.time()))
    store = Store(tmp)

    # --- sessions ------------------------------------------------------- #
    class T:
        def __init__(self, h):
            self.tm_hour = h

    check("09:00 UTC -> London open", "London" in open_sessions(T(9)), str(open_sessions(T(9))))
    check("14:00 UTC -> London and New York overlap",
          set(open_sessions(T(14))) >= {"London", "New York"}, str(open_sessions(T(14))))
    check("03:00 UTC -> Sydney + Tokyo, no London",
          "London" not in open_sessions(T(3)) and "Tokyo" in open_sessions(T(3)),
          str(open_sessions(T(3))))
    check("open_sessions with real clock never raises", isinstance(open_sessions(), list),
          str(open_sessions()))

    # --- market phrasing ------------------------------------------------ #
    def bars(prices):
        return [{"time": i, "open": p, "high": p + 0.5, "low": p - 0.5, "close": p}
                for i, p in enumerate(prices)]

    rising = bars([2400 + i * 1.2 for i in range(60)])
    tight_high = bars([2400 + i * 1.5 for i in range(40)] + [2459.5 + (0.3 if i % 2 else -0.3)
                                                             for i in range(20)])
    check("no candles -> honest 'not enough history'",
          "not enough history" in market_phrase([]), market_phrase([]))
    check("straight up -> resistance or trending",
          any(w in market_phrase(rising) for w in ("resistance", "trending higher")),
          market_phrase(rising))
    check("run then chop below the high -> consolidating/ranging",
          any(w in market_phrase(tight_high) for w in ("consolidat", "ranging", "resistance")),
          market_phrase(tight_high))

    # --- briefing, disconnected, no history ----------------------------- #
    persona = AIPersona(cfg, store)
    b = persona.briefing({"connected": False, "engine": {"running": False,
                                                         "strategy": "recovery_grid"}})
    check("greeting names the user",
          b["greeting"] == "Hi Theophilus. Welcome back to INFINITY.100 AI.", b["greeting"])
    check("briefing offers exactly 4 suggestions", len(b["suggestions"]) == 4,
          " | ".join(s["label"] for s in b["suggestions"]))
    check("disconnected briefing says so, invents no price",
          any("not connected to MetaTrader 5" in ln for ln in b["lines"]),
          [ln for ln in b["lines"] if "MetaTrader" in ln][:1])
    check("fresh install admits it has no history",
          any("no trading history" in ln for ln in b["lines"]))
    check("briefing ends with the offer", b["lines"][-1] == "Would you like me to:")

    # --- briefing, connected, with yesterday's result ------------------- #
    now = time.time()
    midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
    y = midnight - 43200.0  # midday yesterday
    store.record_equity(1000.0, 1000.0, 900.0, ts=y - 60)
    for i, profit in enumerate((8.40, 7.10, 5.50)):
        bid_ = store.open_basket("XAUUSD", "buy", "recovery_grid", 2400.0, ts=y + i)
        store.close_basket(bid_, profit=profit, exit_price=2402.0, reason="target",
                           layers=3, lots=0.3, ts=y + 100 + i)

    ctx = {
        "connected": True,
        "account": {"balance": 1488.97, "equity": 1155.07, "margin_free": 300.0,
                    "currency": "USD", "is_real": False},
        "symbol": {"name": "XAUUSDm", "bid": 4394.55, "ask": 4394.90, "spread_usd": 0.35},
        "candles": tight_high,
        "risk": {"level": "safe", "allow_open": True, "margin_level": 900.0,
                 "stop_out_distance_pct": 94.0, "drawdown_pct": 0.0, "daily_pl_pct": 1.2,
                 "reasons": ["Flat — no exposure."]},
        "basket": {"open": False, "side": "", "layers": 0, "lots": 0.0, "profit": 0.0},
        "engine": {"running": False, "strategy": "recovery_grid"},
        "stats": store.stats(),
    }
    b = persona.briefing(ctx)
    text = " ".join(b["lines"])
    check("mentions Gold and the London/New York sessions",
          "Gold" in text and "London" in text and "New York" in text,
          b["lines"][0])
    check("states the Moderate risk profile", "risk profile is Moderate" in text)
    check("reports yesterday: three positions, total gain, percentage",
          "three positions" in text and "total gain" in text and "%" in text,
          [ln for ln in b["lines"] if "Yesterday" in ln][:1])
    check("reads the live market with a real price",
          "4394.55" in text, [ln for ln in b["lines"] if "4394" in ln][:1])
    check("says automation is idle", "Automation is idle" in text)

    ctx_live = dict(ctx)
    ctx_live["engine"] = {"running": True, "strategy": "recovery_grid"}
    ctx_live["basket"] = {"open": True, "side": "sell", "layers": 8, "lots": 16.0,
                          "profit": 1876.80}
    b2 = persona.briefing(ctx_live)
    t2 = " ".join(b2["lines"])
    check("live basket is reported with layers and float",
          "8 SELL layers" in t2 and "1,876.80" in t2,
          [ln for ln in b2["lines"] if "layers" in ln][:1])
    check("suggestion flips to Pause automation",
          any(s["label"] == "Pause automation" for s in b2["suggestions"]))

    # --- narration ------------------------------------------------------ #
    opened = persona.narrate({"kind": "open", "side": "sell", "volume": 2.0, "price": 4394.56})
    check("narrate open", opened == "Opened SELL 2.00 lots at 4394.56.", opened)
    check("narrate layer mentions the layer number",
          "layer 3" in persona.narrate({"kind": "layer", "side": "buy", "volume": 0.5,
                                        "price": 2399.6, "layers": 3, "reason": "step reached"}),
          persona.narrate({"kind": "layer", "side": "buy", "volume": 0.5, "price": 2399.6,
                           "layers": 3, "reason": "step reached"}))
    check("narrate winning close says Banked",
          persona.narrate({"kind": "close", "profit": 1876.8, "layers": 8}).startswith("Banked"),
          persona.narrate({"kind": "close", "profit": 1876.8, "layers": 8}))
    check("narrate losing close says Cut",
          persona.narrate({"kind": "close", "profit": -42.15, "layers": 1}).startswith("Cut"),
          persona.narrate({"kind": "close", "profit": -42.15, "layers": 1}))
    check("narrate of junk never raises", isinstance(persona.narrate({}), str),
          persona.narrate({}))
    check("narrate(None) never raises", isinstance(persona.narrate(None), str))

    # --- replies -------------------------------------------------------- #
    r = persona.reply("Scan for new opportunities", ctx)
    check("scan reply reads the market and offers to deploy",
          "Scanning" in r["text"] and r["action"] == "engine.start", r["text"][:80])
    r = persona.reply("Scan for new opportunities", {"connected": False})
    check("scan with no broker refuses instead of inventing",
          "cannot scan" in r["text"] and not r["action"], r["text"][:60])
    r = persona.reply("Continue automation", ctx)
    check("continue automation -> engine.start action", r["action"] == "engine.start",
          r["text"][:70])
    r = persona.reply("Pause automation", ctx_live)
    check("pause -> engine.stop action", r["action"] == "engine.stop", r["text"][:70])
    r = persona.reply("close everything now", ctx_live)
    check("close everything -> engine.close_all and asks to confirm",
          r["action"] == "engine.close_all" and "Confirm" in r["text"], r["text"][:70])
    r = persona.reply("Explain today's market", ctx)
    check("explain market quotes bid and ask", "4394.55" in r["text"] and "4394.90" in r["text"],
          r["text"][:90])
    r = persona.reply("Review yesterday's trades", ctx)
    check("review lists closed baskets", "three positions" in r["text"] and "All time" in r["text"],
          r["text"].split("\n")[0][:80])
    r = persona.reply("how is my risk", ctx)
    check("risk reply is honest about being flat",
          "flat" in r["text"].lower() and "SAFE" in r["text"], r["text"][:70])
    r = persona.reply("what's my balance", ctx)
    check("balance reply uses real account numbers",
          "1,488.97" in r["text"] and "1,155.07" in r["text"], r["text"][:80])
    r = persona.reply("which strategies do you have", ctx)
    check("strategy reply lists the installed modules",
          "RECOVERY GRID" in r["text"] and "AI SCALPER" in r["text"], r["text"][:90])
    r = persona.reply("who are you", ctx)
    check("identity reply mentions staying local", "Nothing leaves this computer" in r["text"],
          r["text"][:60])
    r = persona.reply("banana helicopter", ctx)
    check("unknown input falls back to a capability list, never an error",
          "I can scan" in r["text"], r["text"][:60])
    r = persona.reply("", ctx)
    check("empty input handled", "did not catch that" in r["text"], r["text"][:50])
    check("every reply carries 4 suggestions",
          all(len(persona.reply(m, ctx)["suggestions"]) == 4
              for m in ("scan", "risk", "", "who are you")))

    # --- degraded: no store, no config ---------------------------------- #
    bare = AIPersona({}, None)
    b3 = bare.briefing({})
    check("persona with empty config and no store still briefs",
          b3["greeting"].startswith("Hi ") and len(b3["lines"]) >= 2, b3["greeting"])
    check("bare persona replies without raising",
          isinstance(bare.reply("scan", {})["text"], str))

    store.close()
    try:
        os.remove(tmp)
        for suffix in ("-wal", "-shm"):
            if os.path.exists(tmp + suffix):
                os.remove(tmp + suffix)
    except OSError:
        pass

    print("-" * 72)
    print("PASS %d   FAIL %d" % (passed, failed))
    raise SystemExit(1 if failed else 0)
