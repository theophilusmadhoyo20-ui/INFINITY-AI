"""Server self-test — starts the real HTTP server on a spare port and hits it.

    python -m backend.server_test

Uses the simulated broker from engine_test, so it needs no MetaTrader 5.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend.config import load_config  # noqa: E402
from backend.engine import TradingEngine  # noqa: E402
from backend.engine_test import FakeBridge  # noqa: E402
from backend.persona import AIPersona  # noqa: E402
from backend.risk import RiskManager  # noqa: E402
from backend.server import Api, Server  # noqa: E402
from backend.store import Store  # noqa: E402

PASS = FAIL = 0
BASE = ""


def check(label: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("[ok  ] %s %s" % (label, detail))
    else:
        FAIL += 1
        print("[FAIL] %s %s" % (label, detail))


def get(path: str, raw: bool = False):
    req = urllib.request.Request(BASE + path)
    with urllib.request.urlopen(req, timeout=10) as resp:
        body = resp.read()
        return (resp.status, resp.headers, body if raw else json.loads(body.decode("utf-8")))


def post(path: str, payload=None, ctype: str = "application/json"):
    data = json.dumps(payload if payload is not None else {}).encode("utf-8")
    req = urllib.request.Request(BASE + path, data=data, method="POST")
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw}


def post_raw(path: str, raw: bytes, ctype: str = "application/json"):
    req = urllib.request.Request(BASE + path, data=raw, method="POST")
    req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(body)
        except json.JSONDecodeError:
            return exc.code, {"raw": body}


def raw_request(method: str, path: str, headers: dict, body: bytes = b"") -> tuple:
    """One request over a bare socket, so *every* header is ours to choose.

    urllib insists on writing a correct Host and will not let us omit a
    Content-Type, which are exactly the two things the door checks. Returns
    ``(status, text)``.
    """
    import socket

    host, port = BASE.split("//", 1)[1].split(":")
    lines = ["%s %s HTTP/1.1" % (method, path)]
    for key, value in headers.items():
        lines.append("%s: %s" % (key, value))
    if body:
        lines.append("Content-Length: %d" % len(body))
    lines.append("Connection: close")
    wire = ("\r\n".join(lines) + "\r\n\r\n").encode("utf-8") + body
    sock = socket.create_connection((host, int(port)), timeout=10)
    try:
        sock.sendall(wire)
        chunks = []
        while True:
            chunk = sock.recv(4096)
            if not chunk:
                break
            chunks.append(chunk)
    finally:
        sock.close()
    text = b"".join(chunks).decode("utf-8", "replace")
    first = text.split("\r\n", 1)[0]
    try:
        status = int(first.split(" ")[1])
    except (IndexError, ValueError):
        status = 0
    return status, text


def main() -> int:
    global BASE
    logging.basicConfig(level=logging.CRITICAL)
    print("=" * 72)
    print("INFINITY.100 AI - server self-test (real HTTP, simulated broker)")
    print("=" * 72)

    config = load_config()
    config["account"]["password"] = "not-a-real-password"
    # load_config() reads the real config.json on this machine, so the suite would
    # otherwise inherit whatever the operator last saved. Two checks below assert
    # that real-money trading is still blocked after a rejected write; starting
    # from an ambient True makes them fail for a reason that has nothing to do
    # with the code, and starting from an ambient False makes them pass without
    # proving anything. Pin it.
    config["account"]["allow_live_real"] = False
    # Same reason: the engine-control checks below need a strategy that opens a
    # basket on the fake's synthetic candles, and they switch *to* ai_scalper to
    # prove the switch works — so inheriting the operator's current selection makes
    # them either trade nothing or switch to what is already selected.
    config["engine"]["active_strategy"] = "recovery_grid"
    tmp = os.path.join(tempfile.gettempdir(), "infinity_server_%d.db" % int(time.time()))
    tmp_cfg = os.path.join(tempfile.gettempdir(), "infinity_server_%d.json" % int(time.time()))
    store = Store(tmp)
    bridge = FakeBridge(balance=14000.0)
    engine = TradingEngine(config, bridge, RiskManager(config), store,
                           AIPersona(config, store))
    engine.manual = True
    api = Api(config, bridge, engine, RiskManager(config), store, AIPersona(config, store),
              config_path=tmp_cfg)
    server = Server(api, port=8791)
    BASE = server.start()
    engine._tick()

    try:
        # -- status ---------------------------------------------------- #
        status, headers, body = get("/api/status")
        check("GET /api/status is 200 JSON", status == 200 and body.get("connected") is True,
              headers.get("Content-Type"))
        need = {"ts", "connected", "engine", "account", "symbol", "basket", "risk", "stats",
                "equity_curve", "notice"}
        check("status carries every contract key", need.issubset(body),
              str(sorted(need - set(body))))
        check("status is not cached", headers.get("Cache-Control") == "no-store")
        check("status shows the live simulated equity",
              abs(float(body["account"]["equity"]) - 14000.0) < 0.01,
              str(body["account"]["equity"]))

        # -- strategies ------------------------------------------------ #
        status, _, body = get("/api/strategies")
        check("GET /api/strategies lists 4 cards",
              status == 200 and len(body["strategies"]) == 4,
              ", ".join(s["title"] for s in body["strategies"]))
        card = body["strategies"][0]
        check("each card has stats, skills, level and an equity tag",
              all(k in card for k in ("key", "title", "klass", "stats", "skills", "level",
                                      "equity_tag")),
              "%s L%d %s" % (card["title"], card["level"], card["equity_tag"]))
        check("every card carries an expectancy verdict",
              all(s.get("expectancy", {}).get("verdict") for s in body["strategies"]),
              ", ".join("%s=%s" % (s["key"], s.get("expectancy", {}).get("verdict"))
                        for s in body["strategies"]))
        scalp = [s for s in body["strategies"] if s["key"] == "ai_scalper"][0]
        # the shipped bracket needed 69% of trades to win; the arithmetic that
        # would have said so has to be in the payload, not just in a test
        check("the scalper reports the win rate its bracket needs",
              30.0 < float(scalp["expectancy"]["required_win_pct"]) < 50.0,
              str(scalp["expectancy"].get("required_win_pct")))
        check("a strategy with no stop loss says so instead of a win rate",
              [s for s in body["strategies"]
               if s["key"] == "recovery_grid"][0]["expectancy"]["verdict"] == "no-stop",
              str([s for s in body["strategies"]
                   if s["key"] == "recovery_grid"][0]["expectancy"]))

        # -- config redaction ------------------------------------------ #
        status, _, body = get("/api/config")
        check("GET /api/config redacts the password",
              status == 200 and body["config"]["account"]["password"] == "***",
              repr(body["config"]["account"]["password"]))
        check("the real password never appears in the response",
              "not-a-real-password" not in json.dumps(body))

        # -- linking a broker ------------------------------------------- #
        # This is the endpoint that takes a secret, so most of these checks are
        # about what must NOT happen: no echo, no guess, no silent coercion.
        status, _, body = get("/api/broker")
        check("GET /api/broker answers with the panel's data",
              status == 200 and body["ok"] is True
              and isinstance(body.get("terminals"), list),
              "%d terminal(s), %d server name(s)"
              % (len(body.get("terminals") or []), len(body.get("servers") or [])))
        check("...and reports a password is set without saying what it is",
              body["account"]["password_set"] is True
              and "not-a-real-password" not in json.dumps(body),
              "password_set=%s from_env=%s" % (body["account"]["password_set"],
                                               body["account"]["password_from_env"]))
        check("...and never puts a 'password' key in the account view",
              "password" not in body["account"], str(sorted(body["account"])))
        for terminal in (body.get("terminals") or []):
            check("discovered terminal %r has a path and a server list"
                  % terminal.get("name", "?"),
                  isinstance(terminal.get("path"), str) and terminal["path"]
                  and isinstance(terminal.get("servers"), list),
                  "%s (%s)" % (terminal.get("path", ""),
                               "found" if terminal.get("exists") else "missing"))

        status, body = post("/api/broker/link", {"login": 90210, "server": "Demo-One",
                                                 "password": "hunter2", "connect": False})
        check("POST /api/broker/link saves the details",
              status == 200 and body["ok"] and body["saved"], body["message"])
        check("...and does not echo the password back",
              "hunter2" not in json.dumps(body))
        check("...and the config now holds them",
              config["account"]["login"] == 90210
              and config["account"]["server"] == "Demo-One"
              and config["account"]["password"] == "hunter2",
              "%s@%s" % (config["account"]["login"], config["account"]["server"]))
        check("...and it landed on disk, not just in memory",
              json.loads(open(tmp_cfg, encoding="utf-8").read())["account"]["login"] == 90210)
        saved_pw = json.loads(open(tmp_cfg, encoding="utf-8").read())["account"]["password"]
        check("...with the password written for next launch, not blanked",
              saved_pw == "hunter2", repr("*" * len(saved_pw)))

        status, body = post("/api/broker/link", {"server": "Demo-Two", "connect": False})
        check("a field left out keeps the value it had",
              status == 200 and config["account"]["login"] == 90210
              and config["account"]["password"] == "hunter2",
              "changed: %s" % body.get("changed"))
        check("...and only the field sent is reported as changed",
              body.get("changed") == ["server"], str(body.get("changed")))

        status, body = post("/api/broker/link", {"password": "***", "connect": False})
        check("sending back the '***' mask leaves the password alone",
              status == 200 and config["account"]["password"] == "hunter2",
              "changed: %s" % body.get("changed"))

        status, body = post("/api/broker/link", {"login": "not-a-number", "connect": False})
        check("a non-numeric login is a 400 with a sentence, not a stack trace",
              status == 400 and body["ok"] is False and "digits" in body["error"],
              body["error"])
        check("...and nothing was applied", config["account"]["login"] == 90210)

        status, body = post("/api/broker/link", {"allow_live_real": "false",
                                                 "connect": False})
        check("allow_live_real must be a real boolean, not the string 'false'",
              status == 400 and "true or false" in body["error"], body["error"])
        check("...and real-money trading stayed blocked",
              config["account"]["allow_live_real"] is False)

        status, body = post("/api/broker/link", {"logn": 1, "connect": False})
        check("a misspelt field is refused instead of silently ignored",
              status == 400 and "unexpected field" in body["error"], body["error"])

        status, body = post("/api/broker/link", {"login": 555, "password": "",
                                                 "connect": False})
        check("a login with no password is refused before the broker sees it",
              status == 400 and "needs a password" in body["error"], body["error"])
        check("...and the working login was not overwritten",
              config["account"]["login"] == 90210, str(config["account"]["login"]))

        status, body = post("/api/broker/link", {"login": 0, "password": "",
                                                 "connect": False})
        check("clearing both means 'use whoever is logged in', and is allowed",
              status == 200 and body["ok"] and config["account"]["login"] == 0,
              "; ".join(body.get("warnings") or []))

        status, body = post("/api/broker/link", {"login": 90210, "password": "hunter2",
                                                 "allow_live_real": True,
                                                 "connect": False})
        check("turning on real-money trading is allowed and said out loud",
              status == 200 and config["account"]["allow_live_real"] is True
              and "REAL" in body["message"], body["message"])
        config["account"]["allow_live_real"] = False

        status, body = post("/api/broker/link", {"terminal_path": "D:/nowhere/terminal64.exe",
                                                 "connect": False})
        check("a terminal path that does not exist warns but still saves",
              status == 200 and body["ok"]
              and any("no file at" in w for w in body.get("warnings") or []),
              "; ".join(body.get("warnings") or []))

        # changing the account while connected must not leave automation armed
        # on positions that belong to a different login
        bridge.connected = True
        engine._running = True
        status, body = post("/api/broker/link", {"login": 777001, "password": "other",
                                                 "connect": False})
        check("re-linking a different account stops automation first",
              status == 200 and engine.running() is False
              and "Automation was stopped" in body["message"], body["message"])
        check("...and drops the old connection", bridge.is_connected() is False,
              "%d disconnect(s)" % bridge.disconnects)

        began = time.time()
        status, body = post("/api/broker/link", {"login": 777002, "password": "other",
                                                 "connect": True})
        elapsed = time.time() - began
        check("linking with connect:true returns at once, it does not block",
              status == 200 and elapsed < 1.0, "%.2fs" % elapsed)
        check("...and reports the connection as pending",
              (body.get("connect") or {}).get("pending") is True,
              (body.get("connect") or {}).get("message", ""))

        # put the harness back the way the later tests expect it
        config["account"].update({"login": 12345, "server": "Fake-Demo",
                                  "password": "not-a-real-password",
                                  "allow_live_real": False})
        engine.offline = False
        bridge.connected = True
        engine._tick()

        # -- briefing --------------------------------------------------- #
        status, _, body = get("/api/briefing")
        check("GET /api/briefing greets the user by name",
              status == 200 and "Theophilus" in body["greeting"], body["greeting"])
        check("briefing returns 4 suggestions", len(body["suggestions"]) == 4)

        # -- history ---------------------------------------------------- #
        status, _, body = get("/api/history?limit=5")
        check("GET /api/history returns baskets, curve, events and stats",
              status == 200 and all(k in body for k in ("baskets", "equity_curve", "events",
                                                        "stats")),
              "%d baskets" % len(body["baskets"]))
        status, _, body = get("/api/history?limit=abc")
        check("a junk limit falls back instead of erroring", status == 200)

        # -- engine control -------------------------------------------- #
        status, body = post("/api/engine/start")
        check("POST /api/engine/start deploys", status == 200 and body["ok"], body["message"])
        check("status reflects running automation", get("/api/status")[2]["engine"]["running"])
        engine._tick()
        check("the engine traded through the API", len(bridge.orders) >= 1, str(bridge.orders))

        status, body = post("/api/engine/strategy", {"key": "ai_scalper"})
        check("switching strategy is refused while the basket is open",
              status == 200 and not body["ok"], body["message"])

        status, body = post("/api/engine/close_all")
        check("POST /api/engine/close_all flattens", status == 200 and body["ok"],
              body["message"])
        engine._tick()
        status, body = post("/api/engine/strategy", {"key": "ai_scalper"})
        check("switching strategy works once flat", status == 200 and body["ok"], body["message"])
        # The switch has to reach disk, not just memory. It used to live only in the
        # in-process config, so every restart reverted to whatever config.json held —
        # on the live account that meant silently falling back to recovery_grid, the
        # martingale configured with no basket stop loss, after the user had chosen
        # something else. Read the file back rather than trusting the response.
        with open(api.config_path, encoding="utf-8") as handle:
            on_disk = json.load(handle)
        check("...and the choice is written to config.json, not just to memory",
              on_disk.get("engine", {}).get("active_strategy") == "ai_scalper",
              "on disk: %r" % on_disk.get("engine", {}).get("active_strategy"))

        status, body = post("/api/engine/strategy", {"key": "does_not_exist"})
        check("an unknown strategy key is rejected", not body["ok"], body["message"][:60])
        with open(api.config_path, encoding="utf-8") as handle:
            after_bad = json.load(handle)
        check("a rejected switch does not overwrite the saved strategy",
              after_bad.get("engine", {}).get("active_strategy") == "ai_scalper",
              "on disk: %r" % after_bad.get("engine", {}).get("active_strategy"))
        status, body = post("/api/engine/strategy", {})
        check("a missing key is a 400", status == 400, body.get("error", "")[:50])
        post("/api/engine/strategy", {"key": "recovery_grid"})

        status, body = post("/api/engine/stop")
        check("POST /api/engine/stop halts automation", status == 200 and body["ok"],
              body["message"])

        # -- kill switch ------------------------------------------------ #
        status, body = post("/api/risk/kill", {"on": True})
        check("POST /api/risk/kill turns the kill switch on",
              status == 200 and body["kill_switch"] is True, body["message"])
        check("kill switch blocks deploying", not post("/api/engine/start")[1]["ok"])
        post("/api/risk/kill", {"on": False})
        check("kill switch can be turned back off",
              not (config.get("risk") or {}).get("kill_switch"))
        status, body = post("/api/risk/kill", {"on": "yes"})
        check("a non-boolean kill flag is a 400", status == 400, body.get("error", "")[:40])

        # -- config write ----------------------------------------------- #
        status, body = post("/api/config", {"engine": {"poll_ms": 1500}})
        check("POST /api/config merges and saves",
              status == 200 and body["config"]["engine"]["poll_ms"] == 1500, body["message"])
        check("the merge did not wipe neighbouring keys",
              body["config"]["engine"]["magic"] == 100100)
        check("a redacted password patch does not overwrite the real one",
              post("/api/config", {"account": {"password": "***"}})[1]["ok"]
              and config["account"]["password"] == "not-a-real-password")
        post("/api/config", {"engine": {"poll_ms": 1000}})

        # -- a config patch cannot buy consent to trade real money ------- #
        # This endpoint used to deep_merge the request body into the live config
        # and adopt it untouched, so a patch could store the *string* "false" in
        # account.allow_live_real. A non-empty string is truthy, and the safety
        # interlock read `not bool(...)`, so the word "false" granted permission
        # and a real-money account became tradeable for the rest of the session.
        # Every value below must leave the flag a real bool, and only a
        # deliberate yes may turn it on.
        for raw, want in [("false", False), ("no", False), ("0", False), (" ", False),
                          (1, False), (["x"], False), ({"a": 1}, False),
                          (None, False), (True, True), ("true", True), (False, False)]:
            post("/api/config", {"account": {"allow_live_real": False}})
            status, body = post("/api/config", {"account": {"allow_live_real": raw}})
            live = config["account"]["allow_live_real"]
            # `is` and not `==`: 1 == True in Python, which is the whole point
            check("allow_live_real=%r is stored as %r" % (raw, want),
                  status == 200 and live is want,
                  "http %s, stored %r (%s)" % (status, live, type(live).__name__))
            check("...and the API answer agrees with what was stored",
                  body.get("config", {}).get("account", {}).get("allow_live_real") is live)
        post("/api/config", {"account": {"allow_live_real": False}})
        check("the flag is left off after this section",
              config["account"]["allow_live_real"] is False)
        # the same gate must survive a value that is hostile in a different way:
        # a patch is not allowed to make a numeric setting unusable either
        status, body = post("/api/config", {"engine": {"poll_ms": "not a number"}})
        check("a non-numeric poll_ms is clamped rather than crashing the handler",
              status == 200 and isinstance(config["engine"]["poll_ms"], int),
              "http %s, poll_ms=%r" % (status, config["engine"]["poll_ms"]))
        post("/api/config", {"engine": {"poll_ms": 1000}})

        # -- assistant -------------------------------------------------- #
        status, body = post("/api/assistant", {"message": "how is my risk"})
        check("POST /api/assistant answers about risk",
              status == 200 and "SAFE" in body["text"].upper(), body["text"][:60])
        check("assistant replies carry suggestions", len(body["suggestions"]) == 4)
        status, body = post("/api/assistant", {"message": 12})
        check("a non-string message is a 400", status == 400, body.get("error", "")[:40])
        status, body = post("/api/assistant", {"message": "x" * 5000})
        check("an oversized message is a 400", status == 400, body.get("error", "")[:40])

        # -- validation and errors -------------------------------------- #
        status, body = post_raw("/api/assistant", b"{not json")
        check("malformed JSON is a 400, not a stack trace", status == 400,
              body.get("error", "")[:50])
        status, body = post_raw("/api/assistant", b'["a","list"]')
        check("a JSON array body is a 400", status == 400, body.get("error", "")[:40])
        status, body = post("/api/assistant", {"message": "hi"}, ctype="text/plain")
        check("a non-JSON content type is a 415", status == 415, body.get("error", "")[:40])
        try:
            get("/api/nope")
            check("unknown API route is a 404", False)
        except urllib.error.HTTPError as exc:
            check("unknown API route is a 404", exc.code == 404)

        # -- the door: CSRF and DNS rebinding ---------------------------- #
        # Binding to 127.0.0.1 stops the network, not the browser. Any page the
        # user visits can aim a request here, so these two headers are the only
        # thing standing between a random web page and this account.
        origin = BASE  # http://127.0.0.1:<port> — this app's own origin
        host_hdr = BASE.split("//", 1)[1]

        status, text = raw_request("POST", "/api/risk/kill", {"Host": host_hdr},
                                   b'{"on": true}')
        check("a POST with no Content-Type at all is a 415, not accepted",
              status == 415, "HTTP %d" % status)
        check("...and the kill switch was not flipped by it",
              not (config.get("risk") or {}).get("kill_switch"))

        status, text = raw_request("POST", "/api/risk/kill",
                                   {"Host": host_hdr,
                                    "Content-Type": "application/x-www-form-urlencoded"},
                                   b"on=true")
        check("a form-encoded POST is a 415 (the shape a plain HTML form sends)",
              status == 415, "HTTP %d" % status)

        status, text = raw_request("POST", "/api/engine/start",
                                   {"Host": host_hdr, "Content-Type": "application/json",
                                    "Origin": "https://not-this-app.example"}, b"{}")
        check("a foreign Origin is refused with 403", status == 403, "HTTP %d" % status)
        check("the refusal names the origin it rejected", "not-this-app.example" in text,
              text.split("\r\n\r\n")[-1][:80])

        status, text = raw_request("POST", "/api/engine/start",
                                   {"Host": host_hdr, "Content-Type": "application/json",
                                    "Origin": "null"}, b"{}")
        check("Origin: null (a file:// page or sandboxed frame) is refused",
              status == 403, "HTTP %d" % status)

        status, text = raw_request("POST", "/api/engine/start",
                                   {"Host": host_hdr, "Content-Type": "application/json",
                                    "Sec-Fetch-Site": "cross-site"}, b"{}")
        check("Sec-Fetch-Site: cross-site is refused even with no Origin",
              status == 403, "HTTP %d" % status)

        status, text = raw_request("POST", "/api/engine/stop",
                                   {"Host": host_hdr, "Content-Type": "application/json",
                                    "Origin": origin, "Sec-Fetch-Site": "same-origin"}, b"{}")
        check("the app's own origin is still let through", status == 200,
              "HTTP %d" % status)

        rebind = "attacker.example:" + host_hdr.split(":")[1]
        status, text = raw_request("POST", "/api/engine/start",
                                   {"Host": rebind, "Content-Type": "application/json"}, b"{}")
        check("a rebound Host on POST is refused (DNS rebinding)", status == 403,
              "HTTP %d" % status)
        status, text = raw_request("GET", "/api/status", {"Host": rebind})
        check("a rebound Host on GET is refused too — status leaks the account",
              status == 403, "HTTP %d" % status)
        check("the refusal does not leak the account into the body",
              "not-a-real-password" not in text and '"equity"' not in text)
        status, text = raw_request("GET", "/api/status", {"Host": "localhost:" +
                                                          host_hdr.split(":")[1]})
        check("Host: localhost is fine", status == 200, "HTTP %d" % status)

        # -- connect / disconnect must not block the handler -------------- #
        # bridge.connect() holds the bridge lock for its whole timeout, and
        # mt5.initialize() holds the GIL with it. Run that on an HTTP thread and
        # the interface, the SSE stream and every other request freeze together.
        slow = {"calls": 0}
        real_connect = bridge.connect

        def slow_connect():
            slow["calls"] += 1
            time.sleep(3.0)
            return real_connect()

        bridge.connected = False
        bridge.connect = slow_connect
        began = time.time()
        status, body = post("/api/connect")
        elapsed = time.time() - began
        check("POST /api/connect answers at once instead of blocking",
              status == 200 and elapsed < 1.0, "%.2fs" % elapsed)
        check("...and says pending rather than claiming to be connected",
              body.get("pending") is True and body.get("connected") is not True,
              body.get("message", ""))
        check("...and ran no connect on the HTTP thread", slow["calls"] == 0,
              "%d calls" % slow["calls"])
        bridge.connect = real_connect
        bridge.connected = True

        status, body = post("/api/disconnect")
        check("POST /api/disconnect drops the link", status == 200 and body["ok"]
              and not bridge.is_connected(), body["message"])
        check("...and leaves the engine offline so it will not quietly reconnect",
              engine.offline is True)
        engine.offline = False
        bridge.connected = True
        engine._tick()

        # -- static + traversal ----------------------------------------- #
        status, headers, raw = get("/", raw=True)
        check("GET / serves HTML", status == 200 and b"<" in raw[:400],
              headers.get("Content-Type"))
        check("HTML is served no-store so edits show up on reload",
              headers.get("Cache-Control") == "no-store")
        for attack in ("/../config.json", "/..%2f..%2fconfig.json", "/css/../../config.json",
                       "/%2e%2e/%2e%2e/run.py"):
            code = 0
            try:
                code = get(attack, raw=True)[0]
                blocked = False
            except urllib.error.HTTPError as exc:
                code = exc.code
                blocked = exc.code in (403, 404)
            check("traversal %s is blocked" % attack, blocked, "HTTP %s" % code)

        # -- SSE --------------------------------------------------------- #
        frames = []

        def read_stream():
            try:
                with urllib.request.urlopen(BASE + "/api/stream", timeout=8) as resp:
                    if "text/event-stream" not in (resp.headers.get("Content-Type") or ""):
                        return
                    buf = b""
                    deadline = time.time() + 3.5
                    while time.time() < deadline and len(frames) < 3:
                        chunk = resp.read(1)
                        if not chunk:
                            break
                        buf += chunk
                        while b"\n\n" in buf:
                            frame, buf = buf.split(b"\n\n", 1)
                            frames.append(frame.decode("utf-8", "replace"))
            except Exception:  # noqa: BLE001
                pass

        reader = threading.Thread(target=read_stream, daemon=True)
        reader.start()
        reader.join(timeout=9)
        check("SSE delivers at least 2 status frames", len(frames) >= 2,
              "%d frames" % len(frames))
        if frames:
            first = frames[0]
            check("frames are labelled 'event: status'", first.startswith("event: status"),
                  first.split("\n")[0])
            payload = json.loads(first.split("data: ", 1)[1])
            check("each frame is a full status snapshot", need.issubset(payload),
                  str(sorted(need - set(payload))))
            check("the stream never leaks the password", "not-a-real-password" not in first)

        # -- concurrency -------------------------------------------------- #
        errors = []

        def hammer():
            try:
                for _ in range(15):
                    get("/api/status")
                    get("/api/strategies")
            except Exception as exc:  # noqa: BLE001
                errors.append(repr(exc))

        threads = [threading.Thread(target=hammer) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        check("6 clients x 30 requests with no errors", not errors, str(errors[:2]))

        # -- shutdown drains in-flight requests ------------------------- #
        # run.py closes the database on the line after server.stop(), and handler
        # threads are daemons that socketserver never joins, so stop() has to know
        # when the last request has finished. Nothing in the stdlib reports that;
        # these checks pin the accounting that does. Kept last: it leaves
        # api.stopping set, which is what ends the stream.
        #
        # The SSE section above walks away from its stream, and that handler stays
        # legitimately in flight until its next write hits the closed socket. Wait
        # for the slate to clear rather than measuring against a moving baseline —
        # and note that the wait is itself worth asserting, because it is what
        # keeps the shutdown pause in stop() short in real use.
        check("an abandoned stream does not pin the count open", api.wait_idle(8.0),
              "%d in flight" % api._inflight)
        get("/api/status")
        settled = False
        for _ in range(100):  # up to 2s: the finally in do_GET runs after the
            if api.wait_idle(0.0):  # client already has the whole response
                settled = True
                break
            time.sleep(0.02)
        check("a completed request is taken back out of the count", settled,
              "%d in flight" % api._inflight)

        live = {"bytes": 0}

        def hold_stream():
            try:
                response = urllib.request.urlopen(BASE + "/api/stream", timeout=20)
                while response.read(1):
                    live["bytes"] += 1
            except Exception:  # noqa: BLE001
                pass

        streamer = threading.Thread(target=hold_stream, daemon=True)
        streamer.start()
        for _ in range(40):  # up to 4s for the first status frame to arrive
            if live["bytes"]:
                break
            time.sleep(0.1)
        check("an open SSE stream is counted as one request in flight",
              live["bytes"] > 0 and api._inflight == 1,
              "%d bytes, %d in flight" % (live["bytes"], api._inflight))
        api.stopping.set()
        drained = api.wait_idle(5.0)
        check("setting stopping drains every stream within the timeout", drained,
              "%d still in flight" % api._inflight)
        # the reader learns the stream ended only when the server closes the socket
        # under it, which is a moment after the handler itself returns
        streamer.join(timeout=5)
        check("...and it is the streams that ended, not the count that was reset",
              not streamer.is_alive() and api._inflight == 0,
              "reader alive=%s, %d in flight" % (streamer.is_alive(), api._inflight))

    finally:
        server.stop()
        engine.shutdown()
        store.close()
        for suffix in ("", "-wal", "-shm"):
            try:
                os.remove(tmp + suffix)
            except OSError:
                pass

    print("-" * 72)
    print("PASS %d   FAIL %d" % (PASS, FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
