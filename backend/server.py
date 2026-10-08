"""HTTP + SSE server. Python standard library only, bound to localhost.

Serves the frontend from ``frontend/`` and the JSON API from ``/api/*``.
Nothing here talks to MetaTrader 5 directly — it all goes through the engine
and the bridge, so a broker fault can never take the UI down with it.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import mimetypes
import os
import posixpath
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Optional

from backend.config import FRONTEND_DIR, PASSWORD_ENV, REDACTED, redact, save_config
from backend.config import deep_merge, sanitize_config
from backend.engine import as_block
from backend.mt5_bridge import MT5_AVAILABLE, MT5_IMPORT_ERROR
from backend.mt5_bridge import find_terminals, known_servers
from backend.strategies import roster

HOST = "127.0.0.1"
PORT = 8100
SSE_INTERVAL = 1.0
SSE_KEEPALIVE = 15.0
MAX_BODY = 256 * 1024

#: MT5 carries the login in a signed 64-bit field
MAX_LOGIN = 9223372036854775807

NO_CACHE = (".html", ".css", ".js", ".json", ".svg", ".txt")

mimetypes.add_type("application/javascript", ".js")
mimetypes.add_type("image/svg+xml", ".svg")
mimetypes.add_type("font/woff2", ".woff2")


# --------------------------------------------------------------------------- #
# Who is allowed to talk to us
#
# Binding to 127.0.0.1 keeps the network out, but it does not keep *web pages*
# out: any site the user visits can aim a request at http://localhost:8100.
# Two headers are what actually decide whether a request came from this app.
# --------------------------------------------------------------------------- #

LOOPBACK_NAMES = ("localhost", "127.0.0.1", "::1")


def host_only(header: str) -> str:
    """Strip the port off a Host header, leaving ``[::1]`` brackets intact."""
    raw = (header or "").strip()
    if raw.startswith("["):
        end = raw.find("]")
        return raw[:end + 1] if end > 0 else raw
    return raw.split(":")[0]


def is_loopback(hostname: str) -> bool:
    """True only when ``hostname`` cannot mean any machine but this one.

    This is the DNS-rebinding check. A page on evil.com whose DNS answer is
    127.0.0.1 reaches this server *as evil.com*, so the browser treats the
    reply as same-origin and hands it over — the bind address never helped.
    The Host header is the one place the lie is still visible, so anything
    that is not literally loopback is refused.
    """
    name = (hostname or "").strip().lower()
    if not name:
        return False
    if name.startswith("[") and name.endswith("]"):
        name = name[1:-1]
    if name in LOOPBACK_NAMES or name.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False  # a name we would have to resolve to trust — so, no


# --------------------------------------------------------------------------- #
# Linking a broker
#
# This is the one endpoint that accepts a secret, so the rules are strict and
# they are all here in one readable place:
#
#   * nothing is ever guessed. A field left out keeps the value it already has;
#     a field we cannot make sense of is refused with a sentence saying why.
#     A wrong login guessed on the user's behalf costs them a failed attempt
#     against a real broker account, and brokers lock accounts after a few.
#   * the password goes one way only. It is accepted, held in memory, written
#     to config.json by save_config (which blanks it when the environment is
#     the source), and never returned — the response says *whether* one is set,
#     never what it is.
#   * a REAL account still cannot trade unless allow_live_real is explicitly
#     true, and turning that on is reported back in plain words.
# --------------------------------------------------------------------------- #

#: fields a link request may carry, so a typo is caught instead of ignored
LINK_FIELDS = frozenset({"login", "server", "password", "terminal_path",
                         "allow_live_real", "connect"})


def parse_link(body: dict, current: dict) -> tuple:
    """Read a ``/api/broker/link`` body. Returns ``(fields, error, warnings)``.

    *fields* is the subset of ``account`` settings to apply — only what the
    caller actually sent, so an untouched box never clears a saved value.
    *error* is a sentence to show the user and means nothing was applied.
    *warnings* are things worth saying out loud that are not refusals.

    *current* is the account section as it stands, needed to tell "left the
    password box alone" from "asked for no password at all".
    """
    if not isinstance(body, dict):
        return {}, "expected a JSON object", []
    unknown = sorted(set(body) - LINK_FIELDS)
    if unknown:
        return {}, "unexpected field %s — allowed: %s" % (
            ", ".join(repr(u)[:30] for u in unknown[:4]),
            ", ".join(sorted(LINK_FIELDS))), []

    fields: dict = {}
    warnings: list = []

    if "login" in body:
        raw = body.get("login")
        if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
            return {}, "login must be the account number MetaTrader 5 shows you", []
        text = str(raw).strip().replace(" ", "").replace(",", "")
        if text.endswith(".0"):           # a JSON number arrives as 123.0
            text = text[:-2]
        if text in ("", "0"):
            # deliberate: means "use whatever account the terminal is already
            # logged in to", which the bridge supports by skipping mt5.login
            fields["login"] = 0
        elif not text.isdigit():
            return {}, ("login must be digits only — %r is not an account number"
                        % str(raw)[:40]), []
        else:
            login = int(text)
            if login > MAX_LOGIN:
                return {}, "login %s is too long to be an account number" % text[:24], []
            fields["login"] = login

    if "server" in body:
        raw = body.get("server")
        if not isinstance(raw, str):
            return {}, "server must be text, exactly as your broker spells it", []
        server = raw.strip()
        if len(server) > 128:
            return {}, "server name is too long (128 characters maximum)", []
        if any(ch < " " or ch == "\x7f" for ch in server):
            return {}, "server name contains characters MetaTrader 5 cannot use", []
        fields["server"] = server

    if "password" in body:
        raw = body.get("password")
        if not isinstance(raw, str):
            return {}, "password must be text", []
        if raw == REDACTED:
            # the UI shows "***" for a password that is already set; sending it
            # back means "unchanged", never "set my password to three stars"
            pass
        elif len(raw) > 256:
            return {}, "password is too long (256 characters maximum)", []
        else:
            fields["password"] = raw

    if "terminal_path" in body:
        raw = body.get("terminal_path")
        if not isinstance(raw, str):
            return {}, "terminal_path must be the full path to terminal64.exe", []
        path = raw.strip().strip('"')
        if len(path) > 512:
            return {}, "terminal_path is too long (512 characters maximum)", []
        fields["terminal_path"] = path
        if path and not os.path.isfile(path):
            warnings.append(
                "There is no file at %s. MetaTrader 5 will still be found if it "
                "is already running; if not, fix the path." % path)
        elif path and not os.path.basename(path).lower().endswith(".exe"):
            warnings.append(
                "%s does not look like a program — MetaTrader 5 wants the .exe "
                "itself, not a shortcut or the folder it sits in."
                % os.path.basename(path))
        elif path and "terminal" not in os.path.basename(path).lower():
            # Deliberately not a complaint about the *name*. Brokers rebrand the
            # terminal — MetaTrader5-Deriv-Terminal64.exe is a real one — and the
            # old wording ("not the usual terminal64.exe") told such users their
            # correct path looked wrong. Only say something when the name gives no
            # sign of being a terminal at all, and then say it will still work.
            warnings.append(
                "%s is an unusual name for the MetaTrader 5 program. If that is "
                "what your broker installed, it will work." % os.path.basename(path))

    if "allow_live_real" in body:
        raw = body.get("allow_live_real")
        if not isinstance(raw, bool):
            # never coerce: the string "false" is truthy, and getting this one
            # wrong is the difference between a demo and someone's money
            return {}, 'allow_live_real must be true or false, not %r' % str(raw)[:30], []
        fields["allow_live_real"] = raw

    # -- cross-field checks, on the merged picture rather than one box ------ #
    merged = dict(current or {})
    merged.update(fields)
    login = merged.get("login") or 0
    server = str(merged.get("server") or "").strip()
    env_pw = os.environ.get(PASSWORD_ENV, "") or ""
    password = env_pw or str(merged.get("password") or "")

    if login and not password:
        return {}, ("Account %s needs a password. Type it in, or clear the "
                    "account number to use whichever account is already logged "
                    "in inside MetaTrader 5." % login), []
    if login and not server:
        warnings.append(
            "No trade server given, so MetaTrader 5 will try the one it used "
            "last. If the login is refused, add the server name your broker "
            "gave you.")
    if server and not login:
        warnings.append(
            "A server without an account number is only a note — INFINITY.100 "
            "will use whichever account is already logged in to the terminal.")
    if not login and not password:
        warnings.append(
            "No account details given. INFINITY.100 will trade whichever "
            "account is already logged in inside MetaTrader 5.")
    if env_pw and "password" in fields and fields["password"] != env_pw:
        warnings.append(
            "%s is set in the environment, and that password wins. The one you "
            "typed was saved but will not be used until you clear the variable."
            % PASSWORD_ENV)
    return fields, "", warnings


def account_view(account: dict) -> dict:
    """The account section as it is safe to send back — no password, ever."""
    account = account or {}
    env_pw = os.environ.get(PASSWORD_ENV, "") or ""
    stored = str(account.get("password") or "")
    return {
        "login": int(account.get("login") or 0),
        "server": str(account.get("server") or ""),
        "terminal_path": str(account.get("terminal_path") or ""),
        "connect_timeout_ms": int(account.get("connect_timeout_ms") or 0),
        "allow_live_real": bool(account.get("allow_live_real")),
        # "is a password set" and "where does it come from" are all the UI needs
        "password_set": bool(env_pw or stored),
        "password_from_env": bool(env_pw),
        "password_env_name": PASSWORD_ENV,
    }


class Api:
    """Everything the handler is allowed to touch."""

    def __init__(self, config: dict, bridge, engine, risk, store, persona,
                 logger: Optional[logging.Logger] = None, config_path: str = None) -> None:
        self.config = config
        self.bridge = bridge
        self.engine = engine
        self.risk = risk
        self.store = store
        self.persona = persona
        self.config_path = config_path  # None = the app's own config.json
        self.log = logger or logging.getLogger("infinity.server")
        self.stopping = threading.Event()
        self.started_at = time.time()
        # requests currently being served, so shutdown can wait for them
        self._inflight = 0
        self._inflight_lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    # -- in-flight accounting -------------------------------------------- #
    #
    # ThreadingHTTPServer runs each request on a *daemon* thread, and
    # socketserver's _Threads.append() drops daemon threads on the floor, so
    # server_close() joins nothing however block_on_close is set. Nothing in the
    # stdlib therefore tells us when the last handler has finished — and run.py
    # closes the database on the very next line. A handler still inside a write
    # at that moment loses it silently (Store swallows sqlite3.Error), which on
    # the way out of /api/close_all means a basket that closed for real is never
    # recorded. Counting the requests ourselves is the only honest answer.
    #
    # The count brackets the dispatch, not the socket read, so a browser holding
    # an idle keep-alive connection is not mistaken for work in progress.

    def request_started(self) -> None:
        with self._inflight_lock:
            self._inflight += 1
            self._idle.clear()

    def request_finished(self) -> None:
        with self._inflight_lock:
            self._inflight = max(0, self._inflight - 1)
            if not self._inflight:
                self._idle.set()

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Block until no request is mid-flight. False if *timeout* ran out.

        Bounded on purpose: a handler wedged on a socket write must delay the
        shutdown, not prevent it.
        """
        return self._idle.wait(max(0.0, float(timeout)))

    # -- assembled views ------------------------------------------------- #

    def status(self) -> dict:
        snap = self.engine.status()
        snap["uptime_s"] = round(time.time() - self.started_at, 1)
        # ask the bridge, do not read its private flag: an alternative bridge
        # need only implement blocked(), and the tuple has to be unpacked
        getter = getattr(self.bridge, "blocked", None)
        is_blocked, reason = as_block(getter() if callable(getter) else False)
        snap["blocked"] = is_blocked
        if is_blocked and not snap.get("notice"):
            snap["notice"] = reason
        return snap

    def briefing(self) -> dict:
        ctx = self.status()
        try:
            ctx["candles"] = self.bridge.candles(
                str((self.engine._strategy.params.get("timeframe")
                     or self.engine._strategy.params.get("momentum_tf") or "M5")), 120)
        except Exception:  # noqa: BLE001
            ctx["candles"] = []
        out = self.persona.briefing(ctx)
        out["user"] = dict(self.config.get("user") or {})
        return out

    def assistant(self, message: str) -> dict:
        ctx = self.status()
        try:
            ctx["candles"] = self.bridge.candles("M5", 120)
        except Exception:  # noqa: BLE001
            ctx["candles"] = []
        reply = self.persona.reply(message, ctx)
        reply["ts"] = time.time()
        return reply

    def history(self, limit: int = 50) -> dict:
        return {
            "baskets": self.store.recent_baskets(limit),
            "equity_curve": self.store.equity_curve(max(60, limit * 4)),
            "events": self.store.recent_events(min(60, max(10, limit))),
            "stats": self.store.stats(),
        }

    # -- linking a broker ------------------------------------------------- #

    def broker(self) -> dict:
        """Everything the Link Broker panel needs to fill itself in.

        Read-only and safe while disconnected — that is the whole point, since
        it is what someone opens *because* nothing is connected.
        """
        account = dict(self.config.get("account") or {})
        getter = getattr(self.bridge, "blocked", None)
        is_blocked, block_reason = as_block(getter() if callable(getter) else False)
        try:
            terminals = find_terminals()
        except Exception as exc:  # noqa: BLE001 - discovery is a convenience
            self.log.debug("terminal discovery failed: %s", exc)
            terminals = []

        live = None
        if self.bridge.is_connected():
            try:
                live = self.bridge.account()
            except Exception as exc:  # noqa: BLE001
                self.log.debug("account() failed: %s", exc)

        return {
            "ok": True,
            "account": account_view(account),
            "connected": bool(self.bridge.is_connected()),
            "connecting": bool(getattr(self.engine, "_connecting", None)
                               and self.engine._connecting.is_set()),
            "offline": bool(getattr(self.engine, "offline", False)),
            "blocked": is_blocked,
            "block_reason": block_reason,
            "stage": str(getattr(self.bridge, "last_stage", "") or ""),
            "error": str(getattr(self.bridge, "last_error", "") or ""),
            "symbol": str(getattr(self.bridge, "symbol_name", "") or ""),
            "mt5_available": MT5_AVAILABLE,
            "mt5_import_error": MT5_IMPORT_ERROR,
            "terminals": terminals,
            "servers": known_servers(terminals),
            "live_account": live,
            "running": bool(self.engine.running()) if callable(
                getattr(self.engine, "running", None)) else None,
        }

    def link_broker(self, body: dict) -> dict:
        """Save MT5 account details and (by default) connect with them.

        Returns the same shape as :meth:`broker` plus what happened, so one
        round trip both applies the change and refreshes the panel.
        """
        account = self.config.setdefault("account", {})
        if not isinstance(account, dict):  # a hand-edited config.json can do this
            account = {}
            self.config["account"] = account
        fields, error, warnings = parse_link(body, account)
        if error:
            out = self.broker()
            out.update({"ok": False, "saved": False, "message": error,
                        "error": error, "warnings": warnings})
            return out

        wants_connect = bool(body.get("connect", True))
        identity = ("login", "server", "password", "terminal_path")
        changed = sorted(k for k in fields if account.get(k) != fields[k])
        moved = [k for k in changed if k in identity]

        # A different account must not inherit the live one's automation. The
        # strategy is tracking baskets by ticket on the account it opened them
        # on; pointing it at another one mid-flight would have it manage
        # positions that are not there and ignore ones that are.
        was_running = bool(self.engine.running()) if callable(
            getattr(self.engine, "running", None)) else False
        relinking = bool(moved) and self.bridge.is_connected()
        if relinking:
            self.engine.request_disconnect()

        account.update(fields)
        saved = bool(save_config(self.config, self.config_path, logger=self.log))

        told = []
        if moved:
            told.append("Saved %s." % ", ".join(m.replace("_", " ") for m in moved))
        elif changed:
            told.append("Saved %s." % ", ".join(c.replace("_", " ") for c in changed))
        else:
            told.append("Nothing changed.")
        if not saved:
            told.append("Could not write config.json, so this will not survive a "
                        "restart — check the folder is writable.")
        if relinking and was_running:
            told.append("Automation was stopped because the account changed; "
                        "press DEPLOY when you are happy with the new one.")
        elif relinking:
            told.append("The old connection was closed first.")
        if fields.get("allow_live_real") is True:
            told.append("REAL-money trading is now allowed for this account. "
                        "Real losses are real.")
        elif fields.get("allow_live_real") is False and "allow_live_real" in changed:
            told.append("Real-money trading is blocked again — a real account "
                        "will connect but will not be traded.")

        connect_result = None
        if wants_connect:
            connect_result = self.engine.request_connect()
            told.append(str(connect_result.get("message") or ""))

        out = self.broker()
        out.update({
            "ok": True,
            "saved": saved,
            "changed": changed,
            "warnings": warnings,
            "connect_requested": wants_connect,
            "connect": connect_result,
            "message": " ".join(t for t in told if t),
        })
        return out


# --------------------------------------------------------------------------- #


def make_handler(api: Api):
    class Handler(BaseHTTPRequestHandler):
        server_version = "INFINITY100/1.0"
        protocol_version = "HTTP/1.1"

        # -- plumbing ---------------------------------------------------- #

        def log_message(self, fmt, *args):  # noqa: A003
            api.log.debug("%s %s", self.address_string(), fmt % args)

        def _headers(self, status: int, ctype: str, length: int = None,
                     extra: dict = None) -> None:
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for key, value in (extra or {}).items():
                self.send_header(key, value)
            self.end_headers()

        def _json(self, payload, status: int = 200) -> None:
            try:
                body = json.dumps(payload, default=str).encode("utf-8")
            except (TypeError, ValueError) as exc:
                api.log.error("payload not serialisable: %s", exc)
                body = b'{"ok":false,"error":"internal serialisation error"}'
                status = 500
            self._headers(status, "application/json; charset=utf-8", len(body),
                          {"Cache-Control": "no-store"})
            self._write(body)

        def _error(self, status: int, message: str) -> None:
            self._json({"ok": False, "error": message}, status)

        def _write(self, body: bytes) -> None:
            if self.command == "HEAD":
                return
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError, OSError):
                self.close_connection = True

        def _body(self) -> dict:
            length = 0
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                length = 0
            if length <= 0:
                return {}
            if length > MAX_BODY:
                raise ValueError("request body too large")
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("body is not valid JSON: %s" % exc) from exc
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        # -- who is calling ----------------------------------------------- #

        def _bad_host(self) -> Optional[str]:
            """Reason to refuse on the Host header, or None. Applies to every method.

            Cheap, and the only defence against DNS rebinding — see is_loopback().
            A missing Host is allowed: HTTP/1.1 browsers always send one, so its
            absence means a local script, which already has whatever access the
            attack would be trying to steal.
            """
            host = self.headers.get("Host")
            if host is None or is_loopback(host_only(host)):
                return None
            return ("unexpected Host %r — this server only answers as localhost"
                    % host[:80])

        def _bad_origin(self) -> Optional[str]:
            """Reason to refuse a state-changing request, or None.

            Only POST goes through this. A cross-site GET is already harmless:
            we send no CORS headers, so a foreign page can fire the request but
            never read the reply. A POST does not need to be read to do damage,
            which is what CSRF is, so it is checked at the door.

            Requiring application/json (see do_POST) does most of the work: it
            makes the request non-simple, so the browser must preflight, and we
            answer no preflight. These two headers close the rest.
            """
            site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
            if site and site not in ("same-origin", "none"):
                return "cross-site request refused (Sec-Fetch-Site: %s)" % site[:40]
            origin = (self.headers.get("Origin") or "").strip()
            if origin:
                parsed = urllib.parse.urlsplit(origin)
                if (parsed.scheme not in ("http", "https")
                        or not is_loopback(parsed.hostname or "")):
                    # also catches Origin: null — a file:// page or sandboxed frame
                    return "cross-origin request refused (Origin: %s)" % origin[:80]
            return None

        # -- routing ----------------------------------------------------- #

        def do_GET(self):  # noqa: N802
            api.request_started()
            try:
                return self._serve_get()
            finally:
                api.request_finished()

        do_HEAD = do_GET

        def do_POST(self):  # noqa: N802
            api.request_started()
            try:
                return self._serve_post()
            finally:
                api.request_finished()

        # The routing itself lives below, wrapped by the two counters above so
        # Server.stop() can tell a finished request from one still running. Every
        # exit path must go through the finally, and both of these have many
        # returns, hence the split rather than an inline try.

        def _serve_get(self):
            path, query = self._split()
            refuse = self._bad_host()
            if refuse:
                return self._error(403, refuse)
            try:
                if path == "/api/status":
                    return self._json(api.status())
                if path == "/api/stream":
                    return self._stream()
                if path == "/api/strategies":
                    # the roster is a pure function of config; the closed-basket
                    # record is passed in so it can say what each strategy needs
                    # to win versus what it has actually managed
                    return self._json({"ok": True,
                                       "strategies": roster(api.config,
                                                            api.store.strategy_stats()),
                                       "active": (api.config.get("engine") or {}).get(
                                           "active_strategy", "")})

                if path == "/api/config":
                    return self._json({"ok": True, "config": redact(api.config)})
                if path == "/api/broker":
                    return self._json(api.broker())
                if path == "/api/briefing":
                    return self._json(api.briefing())
                if path == "/api/history":
                    limit = 50
                    try:
                        limit = max(1, min(500, int((query.get("limit") or ["50"])[0])))
                    except (TypeError, ValueError):
                        limit = 50
                    payload = api.history(limit)
                    payload["ok"] = True
                    return self._json(payload)
                if path.startswith("/api/"):
                    return self._error(404, "unknown endpoint %s" % path)
                return self._static(path)
            except Exception as exc:  # noqa: BLE001
                api.log.exception("GET %s failed", path)
                return self._error(500, str(exc))

        def _serve_post(self):
            path, _ = self._split()
            if not path.startswith("/api/"):
                return self._error(404, "unknown endpoint %s" % path)
            refuse = self._bad_host() or self._bad_origin()
            if refuse:
                return self._error(403, refuse)
            ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype != "application/json":
                # mandatory, not "wrong type only": a POST with no Content-Type at
                # all is a *simple* cross-origin request, so any web page can send
                # one with no preflight. Demanding the header is what forces the
                # preflight we never answer.
                return self._error(415, "send Content-Type: application/json")
            try:
                body = self._body()
            except ValueError as exc:
                return self._error(400, str(exc))
            try:
                return self._post(path, body)
            except Exception as exc:  # noqa: BLE001
                api.log.exception("POST %s failed", path)
                return self._error(500, str(exc))

        def _post(self, path: str, body: dict):
            if path == "/api/connect":
                # an explicit click overrides both the --no-connect flag and the
                # escalating reconnect backoff — the user is asking for it now.
                # The attempt itself runs on the engine thread: see
                # TradingEngine.request_connect for why it must not run here.
                return self._json(api.engine.request_connect())

            if path == "/api/disconnect":
                return self._json(api.engine.request_disconnect())

            if path == "/api/broker/link":
                # the one endpoint that takes a secret; see parse_link for the
                # rules it enforces on the way in
                result = api.link_broker(body)
                return self._json(result, 200 if result.get("ok") else 400)

            if path == "/api/engine/start":
                return self._json(api.engine.start())

            if path == "/api/engine/stop":
                return self._json(api.engine.stop())

            if path == "/api/engine/close_all":
                return self._json(api.engine.close_all_now())

            if path == "/api/engine/strategy":
                key = body.get("key")
                if not isinstance(key, str) or not key.strip():
                    return self._error(400, 'expected {"key": "strategy_key"}')
                result = api.engine.set_strategy(key.strip())
                # Persist the choice. set_strategy only writes active_strategy into
                # the in-memory config, so before this the UI's selection lasted
                # exactly as long as the process: every restart silently reverted to
                # whatever config.json said. On this account that meant reverting to
                # recovery_grid — the martingale with basket_sl_pct_equity 0.0, i.e.
                # no stop loss — after the user had picked something else. The kill
                # switch below already saves on write; there was no reason for this
                # not to. Only on success, so a rejected switch cannot write a
                # strategy the engine did not actually adopt.
                if result.get("ok"):
                    save_config(api.config, api.config_path, logger=api.log)
                return self._json(result)

            if path == "/api/risk/kill":
                on = body.get("on", True)
                if not isinstance(on, bool):
                    return self._error(400, 'expected {"on": true|false}')
                api.config.setdefault("risk", {})["kill_switch"] = on
                save_config(api.config, api.config_path, logger=api.log)
                if on:
                    api.engine.stop()
                    api.engine.close_all_now()
                return self._json({"ok": True, "kill_switch": on,
                                   "message": "Kill switch %s." % ("ON" if on else "OFF")})

            if path == "/api/config":
                patch = body.get("patch", body)
                if not isinstance(patch, dict):
                    return self._error(400, "patch must be an object")
                if "account" in patch and isinstance(patch["account"], dict):
                    if patch["account"].get("password") == "***":
                        patch["account"].pop("password")
                # Sanitize before adopting, never after. deep_merge copies the
                # request body in verbatim, so without this the string "false"
                # lands in account.allow_live_real — and because a non-empty
                # string is truthy, the REAL-account block reads it as consent
                # and a live account becomes tradeable. Same gate the loader
                # uses, so the API cannot validate less strictly than startup.
                merged = sanitize_config(deep_merge(api.config, patch), api.log)
                api.config.clear()
                api.config.update(merged)
                ok = save_config(api.config, api.config_path, logger=api.log)
                api.engine.poll_s = max(0.2, float(
                    (api.config.get("engine") or {}).get("poll_ms") or 1000) / 1000.0)
                return self._json({"ok": bool(ok), "config": redact(api.config),
                                   "message": "Saved." if ok else "Could not write config.json."})

            if path == "/api/assistant":
                message = body.get("message", "")
                if not isinstance(message, str):
                    return self._error(400, 'expected {"message": "..."}')
                if len(message) > 2000:
                    return self._error(400, "message too long")
                return self._json(api.assistant(message))

            return self._error(404, "unknown endpoint %s" % path)

        # -- helpers ------------------------------------------------------ #

        def _split(self):
            parsed = urllib.parse.urlsplit(self.path)
            path = urllib.parse.unquote(parsed.path)
            return (path or "/"), urllib.parse.parse_qs(parsed.query)

        def _stream(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self.close_connection = True  # no keep-alive after the stream ends
            last_keepalive = time.time()
            try:
                while not api.stopping.is_set():
                    payload = json.dumps(api.status(), default=str)
                    self.wfile.write(b"event: status\ndata: " + payload.encode("utf-8") + b"\n\n")
                    self.wfile.flush()
                    now = time.time()
                    if now - last_keepalive >= SSE_KEEPALIVE:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        last_keepalive = now
                    time.sleep(SSE_INTERVAL)
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                api.log.debug("SSE client disconnected")
            except Exception as exc:  # noqa: BLE001
                api.log.debug("SSE ended: %s", exc)

        def _static(self, path: str):
            if path in ("/", ""):
                path = "/index.html"
            # normalise and refuse anything that escapes the frontend directory
            clean = posixpath.normpath(path).lstrip("/")
            if not clean or clean.startswith("..") or ".." in clean.split("/"):
                return self._error(403, "forbidden")
            target = os.path.normpath(os.path.join(FRONTEND_DIR, *clean.split("/")))
            root = os.path.normpath(FRONTEND_DIR)
            if not target.startswith(root + os.sep) and target != root:
                return self._error(403, "forbidden")
            if os.path.isdir(target):
                target = os.path.join(target, "index.html")
            if not os.path.isfile(target):
                if path == "/index.html":
                    return self._missing_frontend()
                return self._error(404, "not found: %s" % path)
            try:
                with open(target, "rb") as handle:
                    body = handle.read()
            except OSError as exc:
                return self._error(500, "cannot read %s: %s" % (path, exc))
            ctype = mimetypes.guess_type(target)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype in ("application/javascript",
                                                      "application/json", "image/svg+xml"):
                ctype += "; charset=utf-8"
            cache = ("no-store" if target.lower().endswith(NO_CACHE)
                     else "public, max-age=3600")
            self._headers(200, ctype, len(body), {"Cache-Control": cache})
            self._write(body)

        def _missing_frontend(self):
            body = (
                "<!doctype html><meta charset=utf-8><title>INFINITY.100 AI</title>"
                "<style>body{background:#16181d;color:#f4f5f7;font:15px/1.6 system-ui;"
                "padding:48px;max-width:680px}code{color:#e8362a}</style>"
                "<h1>INFINITY.100 AI</h1><p>The API is running but no frontend was found at"
                " <code>%s</code>.</p><p>Live endpoints: <code>/api/status</code>,"
                " <code>/api/stream</code>, <code>/api/strategies</code>.</p>"
                % FRONTEND_DIR
            ).encode("utf-8")
            self._headers(200, "text/html; charset=utf-8", len(body),
                          {"Cache-Control": "no-store"})
            self._write(body)

    return Handler


class Server:
    """Owns the HTTP server thread."""

    def __init__(self, api: Api, host: str = HOST, port: int = PORT) -> None:
        self.api = api
        self.host = host
        self.port = int(port)
        self.httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return "http://%s:%d" % (self.host, self.port)

    def start(self) -> str:
        # Daemon threads so a wedged handler cannot hold the interpreter open.
        # Note what this costs: socketserver's _Threads.append() ignores daemon
        # threads, so server_close() joins nothing regardless of block_on_close,
        # and the stdlib will never tell us the last handler has finished.
        # Api.wait_idle() in stop() is what covers that gap — see the note there.
        ThreadingHTTPServer.daemon_threads = True
        ThreadingHTTPServer.allow_reuse_address = True
        last_error = None
        for offset in range(0, 10):
            try:
                self.httpd = ThreadingHTTPServer((self.host, self.port + offset),
                                                 make_handler(self.api))
                self.port += offset
                break
            except OSError as exc:
                last_error = exc
                self.api.log.warning("port %d unavailable (%s)", self.port + offset, exc)
        if self.httpd is None:
            raise OSError("no free port in %d-%d: %s" % (self.port, self.port + 9, last_error))
        self._thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.5},
                                        name="infinity-http", daemon=True)
        self._thread.start()
        self.api.log.info("HTTP server listening on %s", self.url)
        return self.url

    def stop(self) -> None:
        self.api.stopping.set()
        if self.httpd is not None:
            try:
                self.httpd.shutdown()
                self.httpd.server_close()
            except Exception as exc:  # noqa: BLE001
                self.api.log.debug("server close: %s", exc)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=3)
        # Only now is it safe for the caller to close the store: the listener is
        # shut but handler threads are daemons the stdlib never joins, so this is
        # the wait that keeps a request from writing into a closed database. The
        # SSE stream rechecks api.stopping every SSE_INTERVAL, so the usual cost
        # here is a fraction of a second even with a browser tab open.
        if not self.api.wait_idle(5.0):
            self.api.log.warning("a request was still running at shutdown; "
                                 "stopping anyway")
        self.api.log.info("HTTP server stopped")
