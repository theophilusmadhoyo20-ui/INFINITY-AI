"""Strategy contract + shared indicator maths.

A strategy is a pure decision function: it looks at a :class:`Ctx` and returns an
:class:`Intent`. It never touches the broker — the engine executes intents.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Optional


# --------------------------------------------------------------------------- #
# data contract
# --------------------------------------------------------------------------- #


@dataclass
class Intent:
    """What the strategy wants done this tick."""

    action: str = "hold"  # 'open' | 'add_layer' | 'close_basket' | 'hold'
    side: str = ""  # 'buy' | 'sell'
    volume: float = 0.0
    reason: str = ""
    #: Bracket to attach at the broker, as a distance in *price* from the entry.
    #: 0.0 means "do not set one", which is what every strategy did implicitly
    #: before these existed — the engine polled and closed on its own, so a stop
    #: was only as tight as the poll interval and vanished if the app stopped.
    #: A strategy that can state its stop in price should say so here and let the
    #: broker hold it.
    sl_distance: float = 0.0
    tp_distance: float = 0.0

    def is_trade(self) -> bool:
        return self.action in ("open", "add_layer")


@dataclass
class StopMove:
    """A change to the broker-held bracket on one already-open position.

    Break-even and trailing are the difference between a bracket that protects a
    winner and one that merely marks where it started. They are expressed as a
    *move* rather than as "trail this position" because the strategy stays a pure
    function: it reads the position's current stop out of the :class:`Ctx` and
    returns where the stop should now be, and the engine sends it.

    ``0.0`` means "leave that side alone" — never "remove it". Removing a stop is
    not something any strategy here has a reason to ask for, and reading 0.0 as
    "clear it" would turn a partially-filled move into an unprotected position.
    """

    ticket: int = 0
    sl: float = 0.0
    tp: float = 0.0
    reason: str = ""


@dataclass
class Ctx:
    """Everything a strategy is allowed to look at."""

    account: dict = field(default_factory=dict)
    symbol: dict = field(default_factory=dict)
    tick: dict = field(default_factory=dict)
    positions: list = field(default_factory=list)
    candles: list = field(default_factory=list)
    #: Raw ticks, oldest first, each ``{time, bid, ask, mid, spread_usd}`` — see
    #: ``MT5Bridge.ticks``. Empty is the normal state: the engine only fetches
    #: these for a strategy that sets ``wants_ticks``, and a broker that serves no
    #: tick history returns nothing. A strategy that reads them must therefore say
    #: what it does when they are missing, out loud, in its own reason string.
    #:
    #: They exist because a candle is a summary written after the fact. Velocity,
    #: displacement over the last two seconds and tick-direction imbalance are not
    #: recoverable from closes at any timeframe, and they are the whole of what a
    #: scalper trades on.
    ticks: list = field(default_factory=list)
    basket: dict = field(default_factory=dict)
    params: dict = field(default_factory=dict)
    #: the risk block from config, so a strategy can size against the same
    #: margin floor the engine will judge it by. Empty means "no floor known",
    #: which every reader treats as permissive rather than as zero.
    risk: dict = field(default_factory=dict)
    now: float = 0.0

    # convenience readers — every one is safe on an empty context
    @property
    def equity(self) -> float:
        return float(self.account.get("equity") or 0.0)

    @property
    def balance(self) -> float:
        return float(self.account.get("balance") or 0.0) or self.equity

    @property
    def ladder_equity(self) -> float:
        """Size input for the lot ladder.

        While a basket is open, floating loss drags equity down — sizing layers
        off it would shrink the ladder exactly when recovery needs it steady, so
        use balance (which only moves on a close) until the basket is flat.
        """
        if self.positions:
            return self.balance or self.equity
        return self.equity or self.balance

    @property
    def bid(self) -> float:
        return float(self.tick.get("bid") or self.symbol.get("bid") or 0.0)

    @property
    def ask(self) -> float:
        return float(self.tick.get("ask") or self.symbol.get("ask") or 0.0)

    @property
    def mid(self) -> float:
        b, a = self.bid, self.ask
        if b and a:
            return (b + a) / 2.0
        return b or a

    @property
    def spread(self) -> float:
        v = self.tick.get("spread_usd", self.symbol.get("spread_usd", 0.0))
        try:
            return float(v or 0.0)
        except (TypeError, ValueError):
            return 0.0

    # -- what the account can actually carry ------------------------------ #
    #
    # Read-only context for a strategy that wants to reason about size. The
    # margin *decision* is not here: RiskManager.lots_allowed already computes
    # the cap against risk.min_margin_level_pct, measures margin-per-lot from
    # live exposure rather than from the textbook formula, and honours
    # volume_max -- and TradingEngine applies it to every entry. A second
    # calculation in the strategies would be a strictly worse copy of it, free
    # to disagree with the one that actually gates the order.

    @property
    def leverage(self) -> float:
        return float(self.account.get("leverage") or 0.0)

    @property
    def contract_size(self) -> float:
        """Units per lot — 100 oz for gold. Never 0, so it is safe to divide by."""
        try:
            size = float(self.symbol.get("trade_contract_size") or 0.0)
        except (TypeError, ValueError):
            size = 0.0
        return size or 100.0

    @property
    def volume_min(self) -> float:
        try:
            return float(self.symbol.get("volume_min") or 0.0) or 0.01
        except (TypeError, ValueError):
            return 0.01

    @property
    def volume_step(self) -> float:
        try:
            return float(self.symbol.get("volume_step") or 0.0) or 0.01
        except (TypeError, ValueError):
            return 0.01

    @property
    def margin_used(self) -> float:
        return float(self.account.get("margin") or 0.0)

    @property
    def margin_per_lot(self) -> float:
        """Margin one lot costs right now, or 0.0 when it cannot be known.

        ``price * contract_size / leverage`` matches MT5 for forex and metals.
        0.0 is the honest answer for a broker that reports no leverage (a few
        report 0 on unlimited-margin accounts), so callers read 0.0 as "unknown"
        rather than as "free".
        """
        price = self.ask or self.bid or self.mid
        lev = self.leverage
        if price <= 0 or lev <= 0:
            return 0.0
        return price * self.contract_size / lev

    def closes(self) -> list:
        return [float(c.get("close", 0.0)) for c in self.candles if c]


class Strategy(ABC):
    """Base class for every strategy module."""

    key: str = "base"
    title: str = "BASE"
    klass: str = "B"  # 'S' | 'A' | 'B'
    stats: dict = {"attack": 0, "defence": 0, "accuracy": 0, "dexterity": 0}
    skills: list = []
    blurb: str = ""
    #: Set True to have the engine fetch tick history into :attr:`Ctx.ticks`.
    #: It costs a second history call to the terminal on every poll, so it is
    #: opt-in rather than free: a strategy that reads candles pays nothing, and
    #: one that does not set this always sees ``ctx.ticks == []``.
    wants_ticks: bool = False

    def __init__(self, params: Optional[dict] = None, logger=None) -> None:
        self.params = dict(params or {})
        self.log = logger
        self._last_close_ts = 0.0
        self._last_layer_price = 0.0
        self._last_side = ""

    # -- lifecycle hooks the engine calls -------------------------------- #

    def manage(self, ctx: Ctx) -> list:
        """Bracket changes for positions that are already open. Default: none.

        Called every tick, before :meth:`decide`, whenever the book is not empty.
        Returning ``[]`` is the old behaviour: the bracket stays exactly where the
        entry put it for the life of the trade, so a position that runs most of the
        way to target and turns around gives all of it back.

        Implementations must be idempotent — this runs once a second — and must
        ratchet: only ever move a stop in the direction that reduces risk. The
        engine drops any move that does not improve on the position's current stop,
        but a strategy that relies on that is one broker quirk away from sending a
        stop backwards, so decide it here.
        """
        return []

    def on_basket_closed(self, now: float, profit: float) -> None:
        self._last_close_ts = float(now)
        self._last_layer_price = 0.0
        self._last_side = ""

    def reset(self) -> None:
        self._last_close_ts = 0.0
        self._last_layer_price = 0.0
        self._last_side = ""

    # -- helpers ---------------------------------------------------------- #

    def p(self, name: str, default=0.0):
        """Read a parameter, falling back to the class default."""
        value = self.params.get(name, default)
        return default if value is None else value

    def cooldown_ok(self, now: float) -> bool:
        wait = float(self.p("cooldown_s", 0) or 0)
        return (float(now) - self._last_close_ts) >= wait

    def spread_ok(self, ctx: Ctx) -> bool:
        cap = float(self.p("max_spread_usd", 0) or 0)
        if cap <= 0:
            return True
        return ctx.spread <= cap

    def lot_for_equity(self, equity: float) -> float:
        """Highest lot tier whose threshold is <= equity.

        Reproduces the reference ladder 0.01 -> 0.02 -> 0.05 -> 0.10 -> 0.50 ->
        2.00 -> 3.00 -> 4.00 as the account grew from $2.44 to $329,438.
        """
        base = float(self.p("base_lot", 0.01) or 0.01)
        tiers = self.p("lot_tiers", None) or []
        clean = []
        for row in tiers:
            try:
                clean.append((float(row[0]), float(row[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if not clean:
            return base
        clean.sort(key=lambda r: r[0])
        lot = clean[0][1]
        try:
            equity = float(equity)
        except (TypeError, ValueError):
            equity = 0.0
        for threshold, tier_lot in clean:
            if equity >= threshold:
                lot = tier_lot
            else:
                break
        return max(lot, 0.0) or base

    def describe(self) -> dict:
        """Roster payload for the UI."""
        stats = dict(self.stats or {})
        total = sum(float(v or 0) for v in stats.values())
        return {
            "key": self.key,
            "title": self.title,
            "klass": self.klass,
            "stats": stats,
            "skills": list(self.skills or []),
            "blurb": self.blurb,
            "level": max(1, int(round(total / 24.0))),
        }

    @abstractmethod
    def decide(self, ctx: Ctx) -> Intent:  # pragma: no cover - abstract
        ...


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #

REGISTRY: dict = {}


def register(cls):
    """Class decorator: add a strategy to :data:`REGISTRY` under ``cls.key``."""
    REGISTRY[cls.key] = cls
    return cls


# --------------------------------------------------------------------------- #
# indicators — pure functions, None when there is not enough data
# --------------------------------------------------------------------------- #


def sma(values: list, period: int):
    if not values or period <= 0 or len(values) < period:
        return None
    window = values[-period:]
    return sum(window) / float(period)


def ema(values: list, period: int):
    """Standard EMA seeded with the first *period* values' SMA."""
    if not values or period <= 0 or len(values) < period:
        return None
    k = 2.0 / (period + 1.0)
    seed = sum(values[:period]) / float(period)
    out = seed
    for v in values[period:]:
        out = (v - out) * k + out
    return out


def ema_series(values: list, period: int) -> list:
    if not values or period <= 0 or len(values) < period:
        return []
    k = 2.0 / (period + 1.0)
    out = [sum(values[:period]) / float(period)]
    for v in values[period:]:
        out.append((v - out[-1]) * k + out[-1])
    return out


def stdev(values: list, period: int):
    if not values or period <= 1 or len(values) < period:
        return None
    window = values[-period:]
    mean = sum(window) / float(period)
    var = sum((v - mean) ** 2 for v in window) / float(period)
    return math.sqrt(var)


def bollinger(values: list, period: int = 20, dev: float = 2.0):
    """(lower, mid, upper) or None."""
    mid = sma(values, period)
    sd = stdev(values, period)
    if mid is None or sd is None:
        return None
    return (mid - dev * sd, mid, mid + dev * sd)


def true_ranges(candles: list) -> list:
    out = []
    for i in range(1, len(candles)):
        c, prev = candles[i], candles[i - 1]
        try:
            high, low = float(c["high"]), float(c["low"])
            close_prev = float(prev["close"])
        except (KeyError, TypeError, ValueError):
            continue
        out.append(max(high - low, abs(high - close_prev), abs(low - close_prev)))
    return out


def atr(candles: list, period: int = 14):
    trs = true_ranges(candles)
    if len(trs) < period or period <= 0:
        return None
    return sum(trs[-period:]) / float(period)


def adx(candles: list, period: int = 14):
    """Wilder's ADX. None when there is not enough history."""
    if not candles or period <= 0 or len(candles) < period * 2 + 1:
        return None
    plus_dm, minus_dm, trs = [], [], []
    for i in range(1, len(candles)):
        try:
            high, low = float(candles[i]["high"]), float(candles[i]["low"])
            phigh, plow = float(candles[i - 1]["high"]), float(candles[i - 1]["low"])
            pclose = float(candles[i - 1]["close"])
        except (KeyError, TypeError, ValueError):
            return None
        up, down = high - phigh, plow - low
        plus_dm.append(up if (up > down and up > 0) else 0.0)
        minus_dm.append(down if (down > up and down > 0) else 0.0)
        trs.append(max(high - low, abs(high - pclose), abs(low - pclose)))

    def smooth(seq: list) -> list:
        acc = sum(seq[:period])
        out = [acc]
        for v in seq[period:]:
            acc = acc - acc / float(period) + v
            out.append(acc)
        return out

    if len(trs) < period:
        return None
    str_, splus, sminus = smooth(trs), smooth(plus_dm), smooth(minus_dm)
    dx = []
    for tr, dp, dm in zip(str_, splus, sminus):
        if tr <= 0:
            continue
        di_plus = 100.0 * dp / tr
        di_minus = 100.0 * dm / tr
        denom = di_plus + di_minus
        if denom <= 0:
            continue
        dx.append(100.0 * abs(di_plus - di_minus) / denom)
    if len(dx) < period:
        return None
    return sum(dx[-period:]) / float(period)


def trend_direction(candles: list, period: int = 20) -> str:
    """'buy' when the last close is above its EMA, 'sell' below, '' if unknown."""
    closes = [float(c.get("close", 0.0)) for c in candles if c]
    base = ema(closes, period)
    if base is None or not closes:
        return ""
    if closes[-1] > base:
        return "buy"
    if closes[-1] < base:
        return "sell"
    return ""


# --------------------------------------------------------------------------- #
# tick indicators
# --------------------------------------------------------------------------- #
#
# Same contract as the candle indicators above: None whenever the data cannot
# support an answer, never a plausible zero.
#
# They exist because nothing above can answer the only question a scalper asks.
# An M1 close is a summary published sixty seconds late; whether price has moved
# forty cents in the last two seconds, and whether the ticks that moved it were
# mostly up-ticks, is not recoverable from closes at any timeframe. Candle
# indicators describe where price has been. These describe what it is doing.


class TickView:
    """One parse of a tick buffer, exposing the measures a scalper reads.

    Built once per decision and then queried, rather than having each measure
    walk the raw list again: a two-minute window of gold is several hundred
    dicts (6 ticks a second, measured), and there are eight measures.

    Construction never raises and never rejects a whole buffer for one bad row.
    A tick stream is broker data — it arrives with duplicate millisecond stamps,
    with one side of the book missing on an illiquid feed, and occasionally out
    of order — so unusable rows are dropped individually and :attr:`count` tells
    the caller how much survived.
    """

    __slots__ = ("times", "mids", "spreads")

    def __init__(self, ticks: list) -> None:
        rows = []
        for t in ticks or []:
            try:
                ts = float(t.get("time") or 0.0)
                mid = float(t.get("mid") or 0.0)
            except (AttributeError, TypeError, ValueError):
                continue
            if ts <= 0 or mid <= 0:
                continue
            try:
                spread = float(t.get("spread_usd") or 0.0)
            except (TypeError, ValueError):
                spread = 0.0
            rows.append((ts, mid, max(0.0, spread)))
        # The bridge sorts, but this class is also handed hand-built lists in
        # tests and could one day be handed another feed's. Sorting a sorted list
        # costs nothing and removes the question.
        rows.sort(key=lambda r: r[0])
        self.times = [r[0] for r in rows]
        self.mids = [r[1] for r in rows]
        self.spreads = [r[2] for r in rows]

    # -- shape of the buffer --------------------------------------------- #

    @property
    def count(self) -> int:
        return len(self.mids)

    @property
    def span(self) -> float:
        """Seconds from the oldest usable tick to the newest. 0.0 if under two."""
        if len(self.times) < 2:
            return 0.0
        return max(0.0, self.times[-1] - self.times[0])

    @property
    def newest_time(self) -> float:
        return self.times[-1] if self.times else 0.0

    @property
    def rate(self):
        """Ticks per second across the buffer, or None if it cannot be measured.

        A collapse in tick rate is itself a market state — the difference between
        a quiet session and a feed that has stopped — and it is invisible in
        price.
        """
        span = self.span
        if span <= 0 or len(self.mids) < 2:
            return None
        return (len(self.mids) - 1) / span

    def staleness(self, quote_time: float):
        """Seconds the newest buffered tick lags *quote_time*, or None.

        *quote_time* is ``tick()["time"]`` — the live quote's own timestamp, in
        the same broker server clock as the buffer, which is what makes the
        subtraction meaningful. Measured on Deriv-Demo this came back at 19,730
        seconds against a buffer that otherwise looked perfect, so this is the
        one check between trading now and trading this morning.
        """
        try:
            quote_time = float(quote_time or 0.0)
        except (TypeError, ValueError):
            return None
        if quote_time <= 0 or not self.times:
            return None
        return quote_time - self.times[-1]

    # -- where the window starts ----------------------------------------- #

    def _index_at(self, seconds: float) -> int:
        """Index of the oldest tick within *seconds* of the newest one.

        Walks back from the end, because that end is the one being asked about
        and a scalper's window is a small tail of a long buffer.
        """
        if not self.times:
            return 0
        cutoff = self.times[-1] - max(0.0, float(seconds))
        i = len(self.times) - 1
        while i > 0 and self.times[i - 1] >= cutoff:
            i -= 1
        return i

    # -- the measures ----------------------------------------------------- #

    def displacement(self, seconds: float = 0.0):
        """Signed change in mid over the trailing *seconds*, or the whole buffer.

        Signed, because direction is the point. ``None`` when there are not two
        distinct ticks to subtract.
        """
        if len(self.mids) < 2:
            return None
        i = self._index_at(seconds) if seconds and seconds > 0 else 0
        if i >= len(self.mids) - 1:
            return None
        return self.mids[-1] - self.mids[i]

    def velocity(self, seconds: float = 0.0):
        """Displacement per second over the trailing *seconds*, or None.

        Divides by the *elapsed time actually spanned*, not by the requested
        window, so a two-second request served by a buffer holding 0.4s of ticks
        reports the velocity of those 0.4s instead of quartering it. MT5 stamps
        many ticks in the same millisecond, so a zero span is ordinary and
        returns None rather than dividing.
        """
        if len(self.mids) < 2:
            return None
        i = self._index_at(seconds) if seconds and seconds > 0 else 0
        if i >= len(self.mids) - 1:
            return None
        elapsed = self.times[-1] - self.times[i]
        if elapsed <= 0:
            return None
        return (self.mids[-1] - self.mids[i]) / elapsed

    def momentum(self, ticks_back: int):
        """Signed change in mid over the last *ticks_back* ticks, or None.

        Counted in ticks rather than seconds on purpose: this is the measure the
        fast/medium/long alignment test compares against itself, and comparing
        three windows only means something if they are all measured the same
        way. Tick count also self-scales with activity — thirty ticks is a longer
        stretch of clock time on a quiet market, which is when a scalper should
        be looking further back anyway.
        """
        try:
            n = int(ticks_back)
        except (TypeError, ValueError):
            return None
        if n < 1 or len(self.mids) < n + 1:
            return None
        return self.mids[-1] - self.mids[-1 - n]

    def imbalance(self, ticks_back: int = 0):
        """Up-ticks versus down-ticks over the last *ticks_back*, in -1.0..+1.0.

        ``(up - down) / (up + down)``: +1.0 is every move up, -1.0 every move
        down, 0.0 a balanced fight. Unchanged ticks are excluded from both sides
        rather than counted as neutral, because a quote that repeats is the feed
        saying nothing, and letting it drag the ratio toward zero would read a
        thin one-way market as a balanced one.

        ``None`` when no tick in the window changed price at all.
        """
        if len(self.mids) < 2:
            return None
        try:
            n = int(ticks_back or 0)
        except (TypeError, ValueError):
            n = 0
        start = max(1, len(self.mids) - n) if n > 0 else 1
        up = down = 0
        for i in range(start, len(self.mids)):
            delta = self.mids[i] - self.mids[i - 1]
            if delta > 0:
                up += 1
            elif delta < 0:
                down += 1
        total = up + down
        if total <= 0:
            return None
        return (up - down) / float(total)

    def excursion(self, seconds: float = 0.0):
        """High-minus-low of mid over the trailing window, or None.

        The unsigned companion to :meth:`displacement`, and the pair is what
        separates a move from a thrash: 40 cents of displacement inside 45 cents
        of range is a one-way push, the same 40 inside 300 is noise that happened
        to end up somewhere.
        """
        if len(self.mids) < 2:
            return None
        i = self._index_at(seconds) if seconds and seconds > 0 else 0
        window = self.mids[i:]
        if len(window) < 2:
            return None
        return max(window) - min(window)

    def efficiency(self, seconds: float = 0.0):
        """``|displacement| / excursion`` over the window, in 0.0..1.0, or None.

        1.0 is a straight line, 0.0 is a round trip back to where it started.
        This is the single number that says whether a burst is worth joining, and
        it is scale-free — no ATR needed, nothing to tune per symbol.
        """
        moved = self.displacement(seconds)
        spanned = self.excursion(seconds)
        if moved is None or spanned is None or spanned <= 0:
            return None
        return min(1.0, abs(moved) / spanned)

    def median_spread(self):
        """Median spread across the buffer, or None.

        Median rather than mean: one 3.00 print during a news spike would drag a
        mean far enough to veto an otherwise ordinary session, and the question
        being asked is what the spread has typically been.
        """
        vals = sorted(s for s in self.spreads if s > 0)
        if not vals:
            return None
        mid = len(vals) // 2
        if len(vals) % 2:
            return vals[mid]
        return (vals[mid - 1] + vals[mid]) / 2.0

