"""SQLite persistence: baskets, fills, the equity curve and an event log.

One connection guarded by a lock — the engine thread and every SSE thread share
this object, so thread safety is not optional here.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
from typing import Optional

SCHEMA = (
    """
    CREATE TABLE IF NOT EXISTS baskets (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        opened_ts  REAL    NOT NULL,
        closed_ts  REAL,
        symbol     TEXT    NOT NULL DEFAULT '',
        side       TEXT    NOT NULL DEFAULT '',
        layers     INTEGER NOT NULL DEFAULT 0,
        lots       REAL    NOT NULL DEFAULT 0,
        entry_avg  REAL    NOT NULL DEFAULT 0,
        exit_price REAL    NOT NULL DEFAULT 0,
        profit     REAL    NOT NULL DEFAULT 0,
        strategy   TEXT    NOT NULL DEFAULT '',
        reason     TEXT    NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS fills (
        id        INTEGER PRIMARY KEY AUTOINCREMENT,
        basket_id INTEGER,
        ticket    INTEGER NOT NULL DEFAULT 0,
        ts        REAL    NOT NULL,
        side      TEXT    NOT NULL DEFAULT '',
        volume    REAL    NOT NULL DEFAULT 0,
        price     REAL    NOT NULL DEFAULT 0,
        comment   TEXT    NOT NULL DEFAULT ''
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS equity (
        ts           REAL PRIMARY KEY,
        equity       REAL NOT NULL DEFAULT 0,
        balance      REAL NOT NULL DEFAULT 0,
        margin_level REAL NOT NULL DEFAULT 0
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS events (
        id           INTEGER PRIMARY KEY AUTOINCREMENT,
        ts           REAL NOT NULL,
        kind         TEXT NOT NULL DEFAULT '',
        message      TEXT NOT NULL DEFAULT '',
        payload_json TEXT NOT NULL DEFAULT '{}'
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_baskets_closed ON baskets (closed_ts)",
    "CREATE INDEX IF NOT EXISTS ix_fills_basket ON fills (basket_id)",
    "CREATE INDEX IF NOT EXISTS ix_events_ts ON events (ts)",
)


class Store:
    """Thread-safe SQLite store. Every method is safe to call from any thread."""

    def __init__(self, path: str, logger: Optional[logging.Logger] = None) -> None:
        self.path = str(path)
        self.log = logger or logging.getLogger("infinity.store")
        self._lock = threading.RLock()
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False, timeout=15.0)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            try:
                self.conn.execute("PRAGMA journal_mode=WAL")
                self.conn.execute("PRAGMA synchronous=NORMAL")
                self.conn.execute("PRAGMA busy_timeout=15000")
            except sqlite3.Error as exc:
                self.log.warning("pragma setup failed: %s", exc)
            for statement in SCHEMA:
                self.conn.execute(statement)
            self.conn.commit()

    # -- plumbing ----------------------------------------------------------- #

    def _write(self, sql: str, args: tuple = ()) -> int:
        with self._lock:
            try:
                cur = self.conn.execute(sql, args)
                self.conn.commit()
                return int(cur.lastrowid or 0)
            except sqlite3.Error as exc:
                self.log.error("store write failed: %s | %s", exc, sql.split("(")[0].strip())
                return 0

    def _rows(self, sql: str, args: tuple = ()) -> list:
        with self._lock:
            try:
                return [dict(r) for r in self.conn.execute(sql, args).fetchall()]
            except sqlite3.Error as exc:
                self.log.error("store read failed: %s", exc)
                return []

    def close(self) -> None:
        with self._lock:
            try:
                self.conn.commit()
                self.conn.close()
            except sqlite3.Error:
                pass

    # -- baskets ------------------------------------------------------------ #

    def open_basket(self, symbol: str, side: str, strategy: str, entry_avg: float = 0.0,
                    ts: Optional[float] = None) -> int:
        return self._write(
            "INSERT INTO baskets (opened_ts, symbol, side, strategy, entry_avg) VALUES (?,?,?,?,?)",
            (float(ts if ts is not None else time.time()), str(symbol), str(side),
             str(strategy), float(entry_avg or 0.0)),
        )

    def update_basket(self, basket_id: int, layers: int = None, lots: float = None,
                      entry_avg: float = None) -> None:
        if not basket_id:
            return
        sets, args = [], []
        if layers is not None:
            sets.append("layers=?")
            args.append(int(layers))
        if lots is not None:
            sets.append("lots=?")
            args.append(float(lots))
        if entry_avg is not None:
            sets.append("entry_avg=?")
            args.append(float(entry_avg))
        if not sets:
            return
        args.append(int(basket_id))
        self._write("UPDATE baskets SET %s WHERE id=?" % ", ".join(sets), tuple(args))

    def close_basket(self, basket_id: int, profit: float, exit_price: float = 0.0,
                     reason: str = "", layers: int = None, lots: float = None,
                     ts: Optional[float] = None) -> None:
        if not basket_id:
            return
        sets = ["closed_ts=?", "profit=?", "exit_price=?", "reason=?"]
        args = [float(ts if ts is not None else time.time()), float(profit or 0.0),
                float(exit_price or 0.0), str(reason)[:240]]
        if layers is not None:
            sets.append("layers=?")
            args.append(int(layers))
        if lots is not None:
            sets.append("lots=?")
            args.append(float(lots))
        args.append(int(basket_id))
        self._write("UPDATE baskets SET %s WHERE id=?" % ", ".join(sets), tuple(args))

    def open_basket_id(self) -> int:
        rows = self._rows(
            "SELECT id FROM baskets WHERE closed_ts IS NULL ORDER BY id DESC LIMIT 1"
        )
        return int(rows[0]["id"]) if rows else 0

    def recent_baskets(self, limit: int = 25) -> list:
        return self._rows(
            "SELECT * FROM baskets WHERE closed_ts IS NOT NULL "
            "ORDER BY closed_ts DESC LIMIT ?",
            (max(1, int(limit)),),
        )

    def baskets_since(self, ts: float) -> list:
        return self._rows(
            "SELECT * FROM baskets WHERE closed_ts IS NOT NULL AND closed_ts >= ? "
            "ORDER BY closed_ts ASC",
            (float(ts),),
        )

    def baskets_between(self, start_ts: float, end_ts: float) -> list:
        return self._rows(
            "SELECT * FROM baskets WHERE closed_ts IS NOT NULL "
            "AND closed_ts >= ? AND closed_ts < ? ORDER BY closed_ts ASC",
            (float(start_ts), float(end_ts)),
        )

    # -- fills -------------------------------------------------------------- #

    def add_fill(self, basket_id: int, ticket: int, side: str, volume: float,
                 price: float, comment: str = "", ts: Optional[float] = None) -> int:
        return self._write(
            "INSERT INTO fills (basket_id, ticket, ts, side, volume, price, comment) "
            "VALUES (?,?,?,?,?,?,?)",
            (int(basket_id or 0), int(ticket or 0),
             float(ts if ts is not None else time.time()), str(side),
             float(volume or 0.0), float(price or 0.0), str(comment)[:120]),
        )

    def fills_for(self, basket_id: int) -> list:
        return self._rows("SELECT * FROM fills WHERE basket_id=? ORDER BY ts ASC",
                          (int(basket_id),))

    # -- equity ------------------------------------------------------------- #

    def record_equity(self, equity: float, balance: float = 0.0, margin_level: float = 0.0,
                      ts: Optional[float] = None) -> None:
        self._write(
            "INSERT OR REPLACE INTO equity (ts, equity, balance, margin_level) VALUES (?,?,?,?)",
            (round(float(ts if ts is not None else time.time()), 2), float(equity or 0.0),
             float(balance or 0.0), float(margin_level or 0.0)),
        )

    def equity_curve(self, limit: int = 240) -> list:
        rows = self._rows(
            "SELECT ts, equity FROM equity ORDER BY ts DESC LIMIT ?", (max(2, int(limit)),)
        )
        return [[round(r["ts"], 2), round(r["equity"], 2)] for r in reversed(rows)]

    def day_start_equity(self) -> float:
        """Equity at the first sample since local midnight, else the earliest sample."""
        midnight = time.mktime(time.localtime()[:3] + (0, 0, 0, 0, 0, -1))
        rows = self._rows(
            "SELECT equity FROM equity WHERE ts >= ? ORDER BY ts ASC LIMIT 1", (midnight,)
        )
        if rows:
            return float(rows[0]["equity"])
        rows = self._rows("SELECT equity FROM equity ORDER BY ts ASC LIMIT 1")
        return float(rows[0]["equity"]) if rows else 0.0

    def peak_equity(self) -> float:
        rows = self._rows("SELECT MAX(equity) AS peak FROM equity")
        return float(rows[0]["peak"] or 0.0) if rows else 0.0

    # -- events ------------------------------------------------------------- #

    def log_event(self, kind: str, message: str, payload: dict = None,
                  ts: Optional[float] = None) -> int:
        try:
            blob = json.dumps(payload or {}, default=str)[:4000]
        except (TypeError, ValueError):
            blob = "{}"
        return self._write(
            "INSERT INTO events (ts, kind, message, payload_json) VALUES (?,?,?,?)",
            (float(ts if ts is not None else time.time()), str(kind)[:40],
             str(message)[:600], blob),
        )

    def recent_events(self, limit: int = 40, kind: str = "") -> list:
        if kind:
            rows = self._rows(
                "SELECT * FROM events WHERE kind=? ORDER BY id DESC LIMIT ?",
                (str(kind), max(1, int(limit))),
            )
        else:
            rows = self._rows("SELECT * FROM events ORDER BY id DESC LIMIT ?",
                              (max(1, int(limit)),))
        for r in rows:
            try:
                r["payload"] = json.loads(r.pop("payload_json", "{}") or "{}")
            except (TypeError, ValueError):
                r["payload"] = {}
        return rows

    # -- aggregates --------------------------------------------------------- #

    def stats(self) -> dict:
        rows = self._rows(
            "SELECT COUNT(*) AS n, "
            "SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) AS wins, "
            "SUM(CASE WHEN profit <= 0 THEN 1 ELSE 0 END) AS losses, "
            "SUM(profit) AS net FROM baskets WHERE closed_ts IS NOT NULL"
        )
        row = rows[0] if rows else {}
        closed = int(row.get("n") or 0)
        wins = int(row.get("wins") or 0)
        losses = int(row.get("losses") or 0)
        net = float(row.get("net") or 0.0)
        start_rows = self._rows("SELECT equity FROM equity ORDER BY ts ASC LIMIT 1")
        return {
            "baskets_closed": closed,
            "wins": wins,
            "losses": losses,
            "net_pl": round(net, 2),
            "win_rate": round(wins / closed * 100.0, 1) if closed else 0.0,
            "peak_equity": round(self.peak_equity(), 2),
            "start_equity": round(float(start_rows[0]["equity"]), 2) if start_rows else 0.0,
        }

    def strategy_stats(self) -> dict:
        """Closed-basket record per strategy, keyed by strategy name.

        One grouped query rather than one per strategy, because this is read on
        every roster poll. ``avg_win``/``avg_loss`` are what make it useful: they
        give the reward/risk the broker actually delivered — spread, swap and
        partial fills included — which is the only honest way to say what win rate
        a strategy needs. SQLite's AVG skips NULLs, so the CASE expressions average
        only the rows on their own side of zero.

        Baskets written before the ``strategy`` column was populated group under
        the empty string and match no roster entry, which is the intent: they
        cannot be attributed, so they are not attributed.
        """
        rows = self._rows(
            "SELECT strategy, COUNT(*) AS n, "
            "SUM(CASE WHEN profit > 0 THEN 1 ELSE 0 END) AS wins, "
            "SUM(profit) AS net, "
            "AVG(CASE WHEN profit > 0 THEN profit END) AS avg_win, "
            "AVG(CASE WHEN profit <= 0 THEN profit END) AS avg_loss, "
            "AVG(layers) AS avg_layers "
            "FROM baskets WHERE closed_ts IS NOT NULL GROUP BY strategy"
        )
        out = {}
        for row in rows:
            closed = int(row.get("n") or 0)
            if not closed:
                continue
            wins = int(row.get("wins") or 0)
            out[str(row.get("strategy") or "")] = {
                "baskets_closed": closed,
                "wins": wins,
                "losses": closed - wins,
                "net_pl": round(float(row.get("net") or 0.0), 2),
                "win_pct": round(wins / closed * 100.0, 1),
                "avg_win": round(float(row.get("avg_win") or 0.0), 2),
                "avg_loss": round(float(row.get("avg_loss") or 0.0), 2),
                "avg_layers": round(float(row.get("avg_layers") or 0.0), 2),
            }
        return out

    def yesterday_summary(self) -> dict:
        """Closed baskets and percentage result for the previous local day."""
        now = time.localtime()
        midnight = time.mktime(now[:3] + (0, 0, 0, 0, 0, -1))
        start = midnight - 86400.0
        rows = self.baskets_between(start, midnight)
        profit = sum(float(r.get("profit") or 0.0) for r in rows)
        eq = self._rows(
            "SELECT equity FROM equity WHERE ts >= ? AND ts < ? ORDER BY ts ASC LIMIT 1",
            (start, midnight),
        )
        base = float(eq[0]["equity"]) if eq else 0.0
        return {
            "count": len(rows),
            "wins": sum(1 for r in rows if float(r.get("profit") or 0.0) > 0),
            "profit": round(profit, 2),
            "pct": round(profit / base * 100.0, 2) if base > 0 else 0.0,
            "base_equity": round(base, 2),
        }


# --------------------------------------------------------------------------- #
# self-test — temp database, no broker
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import tempfile

    logging.basicConfig(level=logging.WARNING)
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
    print("INFINITY.100 AI - store.py self-test")
    print("=" * 72)

    tmp = os.path.join(tempfile.gettempdir(), "infinity_selftest_%d.db" % int(time.time()))
    store = Store(tmp)
    now = time.time()

    # a full winning basket lifecycle, mirroring the reference recording's shape
    bid = store.open_basket("XAUUSD", "sell", "recovery_grid", 4394.558, ts=now - 300)
    check("open_basket returns an id", bid > 0, "id=%d" % bid)
    for i in range(8):
        store.add_fill(bid, 5000 + i, "sell", 2.00, 4394.558, "INFINITY grid", ts=now - 300 + i)
    store.update_basket(bid, layers=8, lots=16.0, entry_avg=4394.558)
    check("8 fills recorded at one entry price", len(store.fills_for(bid)) == 8)
    check("open_basket_id finds the live basket", store.open_basket_id() == bid)
    store.close_basket(bid, profit=1876.80, exit_price=4382.10, reason="target", ts=now - 60)

    # a losing basket
    bid2 = store.open_basket("XAUUSD", "buy", "recovery_grid", 2400.0, ts=now - 50)
    store.add_fill(bid2, 6000, "buy", 0.50, 2400.0, "INFINITY grid", ts=now - 50)
    store.close_basket(bid2, profit=-42.15, exit_price=2391.5, reason="risk", ts=now - 10)

    st = store.stats()
    check("stats counts 2 closed, 1 win, 1 loss",
          st["baskets_closed"] == 2 and st["wins"] == 1 and st["losses"] == 1, str(st["win_rate"]))
    check("net P/L is 1834.65", abs(st["net_pl"] - 1834.65) < 0.01, str(st["net_pl"]))

    for i in range(30):
        store.record_equity(1000.0 + i * 12.5, 1000.0, 800.0, ts=now - (30 - i) * 60)
    curve = store.equity_curve(20)
    check("equity_curve returns [[ts, equity]] ascending",
          len(curve) == 20 and curve[0][0] < curve[-1][0] and isinstance(curve[0], list),
          "%d points, last=%.2f" % (len(curve), curve[-1][1]))
    check("peak_equity tracks the maximum", abs(store.peak_equity() - 1362.5) < 0.01,
          str(store.peak_equity()))
    check("day_start_equity is a real number", store.day_start_equity() > 0,
          "%.2f" % store.day_start_equity())

    store.log_event("engine", "engine started", {"strategy": "recovery_grid"})
    store.log_event("trade", "opened sell 2.00", {"tickets": [5000]})
    events = store.recent_events(10)
    check("events round-trip with a parsed payload",
          len(events) == 2 and events[0]["payload"].get("tickets") == [5000], events[0]["message"])

    check("recent_baskets returns closed baskets newest first",
          [b["id"] for b in store.recent_baskets(5)] == [bid2, bid])

    ys = store.yesterday_summary()
    check("yesterday_summary never raises", isinstance(ys, dict) and "pct" in ys, str(ys))

    # thread safety: hammer it from several threads at once
    errors: list = []

    def worker(n):
        try:
            for i in range(60):
                store.record_equity(2000.0 + n * 100 + i, 2000.0, 500.0, ts=now + n * 1000 + i)
                store.log_event("stress", "thread %d iter %d" % (n, i))
                store.stats()
                store.equity_curve(10)
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    check("6 threads x 60 mixed read/write with no errors", not errors, str(errors[:2]))

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

