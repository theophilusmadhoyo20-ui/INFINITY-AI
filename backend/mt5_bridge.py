"""MetaTrader 5 bridge — the ONLY module in this project that imports MetaTrader5.

Everything else talks to the broker through :class:`MT5Bridge`, which returns
plain JSON-safe dicts and never raises out of a public method.
"""

from __future__ import annotations

import csv
import datetime
import io
import logging
import os
import subprocess
import threading
import time
import traceback
from typing import Any, Optional

try:  # the package is installed, but the UI must still run if it is not
    import MetaTrader5 as mt5  # type: ignore

    MT5_AVAILABLE = True
    MT5_IMPORT_ERROR = ""
except Exception as exc:  # pragma: no cover - depends on the host
    mt5 = None  # type: ignore
    MT5_AVAILABLE = False
    MT5_IMPORT_ERROR = str(exc)


# --------------------------------------------------------------------------- #
# pure helpers (testable without a broker)
# --------------------------------------------------------------------------- #

#: Plain-English explanations for the error codes this project actually hits.
ERROR_HINTS = {
    -10005: (
        "MetaTrader 5 did not answer (IPC timeout). The terminal is not running, "
        "or it is running as a different Windows user. Open MetaTrader 5 and leave it open."
    ),
    -10004: "MetaTrader 5 refused the connection. Restart the terminal and try again.",
    -10003: "The MetaTrader 5 terminal path in config.json is wrong or the exe is missing.",
    -6: (
        "MetaTrader 5 refused the login. Either the account number, the trade server name "
        "or the password is wrong, or no account is logged in inside the terminal. Check the "
        "three details in LINK BROKER against what your broker shows you — the server name "
        "has to match their spelling exactly."
    ),
    -2: "MetaTrader 5 rejected the arguments sent to it.",
    1: "No error.",
}

TIMEFRAMES = ("M1", "M5", "M15", "M30", "H1", "H4", "D1")


def decimal_places(step: float) -> int:
    """Number of decimal places implied by a volume step (0.01 -> 2)."""
    try:
        text = format(float(step), ".8f").rstrip("0")
    except (TypeError, ValueError):
        return 2
    if "." not in text:
        return 0
    return len(text.split(".", 1)[1])


def quantize(volume: float, step: float) -> float:
    """Round *volume* onto the broker's volume grid without float drift.

    ``quantize(0.030000000000000002, 0.01)`` is exactly ``0.03``.
    """
    try:
        volume = float(volume)
        step = float(step)
    except (TypeError, ValueError):
        return 0.0
    if step <= 0:
        return round(volume, 2)
    places = decimal_places(step)
    return round(round(volume / step) * step, places)


def split_volume(
    total: float,
    max_chunk: float,
    vmin: float = 0.01,
    vmax: float = 100.0,
    vstep: float = 0.01,
) -> list:
    """Split *total* lots into broker-legal chunks.

    Reproduces the reference video's identical-entry-price position groups: one
    signal for 16 lots at a 2.00 cap becomes eight 2.00 orders that all fill at
    the same price.

    ``split_volume(5.0, 2.0, 0.01, 100.0, 0.01) == [2.0, 2.0, 1.0]``
    """
    try:
        total = float(total)
    except (TypeError, ValueError):
        return []
    vstep = float(vstep) if vstep and float(vstep) > 0 else 0.01
    vmin = float(vmin) if vmin and float(vmin) > 0 else vstep
    vmax = float(vmax) if vmax and float(vmax) > 0 else 100.0

    cap = float(max_chunk) if max_chunk and float(max_chunk) > 0 else vmax
    cap = min(cap, vmax)
    cap = quantize(cap, vstep)
    if cap < vmin:
        cap = vmin

    total = quantize(total, vstep)
    if total < vmin:
        return []

    chunks: list = []
    remaining = total
    guard = 0
    while remaining >= vmin and guard < 10000:
        guard += 1
        take = quantize(min(cap, remaining), vstep)
        if take < vmin:
            break
        chunks.append(take)
        remaining = quantize(remaining - take, vstep)
    # a sub-minimum remainder is folded into the last chunk when that stays legal
    if remaining > 0 and chunks:
        merged = quantize(chunks[-1] + remaining, vstep)
        if merged <= vmax:
            chunks[-1] = merged
    return chunks


def _asdict(obj: Any) -> dict:
    """Convert an MT5 namedtuple to a plain dict."""
    if obj is None:
        return {}
    if isinstance(obj, dict):
        return dict(obj)
    try:
        return dict(obj._asdict())
    except Exception:
        return {}


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


#: The epoch MT5's ``copy_*`` functions measure a naive datetime against.
_UNIX_EPOCH = datetime.datetime(1970, 1, 1)


def _epoch_to_naive_utc(seconds: float) -> datetime.datetime:
    """A naive datetime that MT5 will read back as exactly *seconds*.

    ``datetime.utcfromtimestamp`` would do this and is deprecated from 3.12;
    ``fromtimestamp`` would apply the *local* timezone, which is the bug this
    helper exists to make impossible. Adding a timedelta to the epoch is neither
    deprecated nor localised, and it is obvious what it means.
    """
    return _UNIX_EPOCH + datetime.timedelta(seconds=float(seconds))


#: Retcodes that mean "the bracket was the problem", not "the trade was".
#: TRADE_RETCODE_INVALID_STOPS (10016) is the documented one; INVALID_PRICE (10015)
#: and INVALID (10013) show up on brokers that validate stops before price.
_STOPS_RETCODES = (10016, 10015, 10013)


def _invalid_stops(retcode: Any) -> bool:
    """True when a rejection is attributable to the attached stop/target."""
    return _i(retcode, 0) in _STOPS_RETCODES


# --------------------------------------------------------------------------- #
# finding the terminals already on this machine
#
# A blank "server" box is the single worst thing we can show someone: the trade
# server name has to match the broker's spelling exactly ("XMGlobal-MT5 10", not
# "XM Global"), it is not guessable, and getting it wrong costs a failed login
# attempt against a real account. MetaTrader 5 leaves two readable traces of
# every terminal it has ever run, so we read those instead of asking.
#
#   %APPDATA%\MetaQuotes\Terminal\<32-hex>\origin.txt   the install directory,
#                                                       UTF-16 with a BOM
#   %APPDATA%\MetaQuotes\Terminal\<32-hex>\bases\<NAME> one folder per trade
#                                                       server, named verbatim
#
# Nothing here is a credential and nothing here is a guess: a name only appears
# because that terminal really did connect to that server. Neither file is
# opened for writing, and a machine with no MetaTrader 5 at all just yields [].
# --------------------------------------------------------------------------- #

#: bases/ subfolders that are MT5's own bookkeeping, not trade servers
NON_SERVER_BASES = frozenset({"custom", "default", "signals", "history", "news",
                              "reports", "ticks"})


def _read_origin(path: str) -> str:
    """The install directory recorded in an ``origin.txt``, or ``""``.

    Written UTF-16 with a BOM by the terminal, but read as UTF-8 by anyone who
    forgets, which turns the path into interleaved NULs. Try UTF-16 first and
    strip whatever NULs survive, so a hand-edited file still resolves.
    """
    try:
        with open(path, "rb") as handle:
            raw = handle.read(4096)
    except OSError:
        return ""
    for encoding in ("utf-16", "utf-8-sig", "latin-1"):
        try:
            text = raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
        text = text.replace("\x00", "").replace("﻿", "").strip()
        if text:
            return text.splitlines()[0].strip()
    return ""


def _data_dir_for(install: str, appdata: str = None) -> str:
    """The ``%APPDATA%\\MetaQuotes\\Terminal\\<32-hex>`` folder belonging to *install*.

    The inverse of the discovery walk above: given a terminal64.exe (or its
    directory) find the instance folder whose ``origin.txt`` names it. Returns ""
    when there is no match, which is the normal answer for a terminal that has
    never been launched. Never raises.
    """
    if not install:
        return ""
    if os.path.isfile(install) or install.lower().endswith(".exe"):
        install = os.path.dirname(install)
    want = os.path.normcase(os.path.normpath(install.rstrip("\\/")))
    root = os.environ.get("APPDATA") if appdata is None else appdata
    if not root:
        return ""
    try:
        entries = list(os.scandir(os.path.join(root, "MetaQuotes", "Terminal")))
    except OSError:
        return ""
    for entry in entries:
        if len(entry.name) != 32:
            continue
        origin = _read_origin(os.path.join(entry.path, "origin.txt"))
        if not origin:
            continue
        if os.path.normcase(os.path.normpath(origin.rstrip("\\/"))) == want:
            return entry.path
    return ""


#: log lines worth quoting back: the terminal's own verdict on a login attempt
_LOGIN_LOG_MARKERS = ("authorization", "authorisation", "invalid account",
                      "invalid password", "account disabled", "login failed")


def _terminal_log_reason(install: str, login: int = 0, appdata: str = None) -> str:
    """The terminal's own last word on a login attempt, or ``""``.

    The Python API collapses every authorisation problem into ``-6 Terminal:
    Authorization failed``. The terminal itself is far more specific and writes
    the difference to ``<data dir>/logs/<YYYYMMDD>.log``::

        '12345678': authorization on Broker-Demo failed (Invalid account)

    "Invalid account" and "Invalid password" are completely different problems --
    the first means the server has never heard of that number, so no amount of
    retyping the password will help, and the account number or the server name is
    what is wrong. Guessing between them is exactly what this project must not do,
    and the answer is sitting in a file we can read. Logs are UTF-16 with a BOM and
    tab-separated; only the tail is read, because they grow all day.
    """
    data_dir = _data_dir_for(install, appdata=appdata)
    if not data_dir:
        return ""
    try:
        logs = [e.path for e in os.scandir(os.path.join(data_dir, "logs"))
                if e.name.lower().endswith(".log")]
    except OSError:
        return ""
    if not logs:
        return ""
    try:
        newest = max(logs, key=os.path.getmtime)
        with open(newest, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 65536))
            raw = handle.read()
    except OSError:
        return ""
    text = ""
    for encoding in ("utf-16-le", "utf-8", "latin-1"):
        try:
            text = raw.decode(encoding, "replace")
        except (LookupError, UnicodeDecodeError):  # pragma: no cover
            continue
        if text.count("\t") > 2:
            break
    # a mid-file seek can land inside a UTF-16 code unit; the first partial line
    # is discarded either way because we only ever want the newest match
    tag = str(int(login)) if login else ""
    best = ""
    for line in text.replace("\x00", "").splitlines():
        low = line.lower()
        if not any(marker in low for marker in _LOGIN_LOG_MARKERS):
            continue
        if "failed" not in low and "invalid" not in low:
            continue
        if tag and tag not in line:
            continue
        parts = [p for p in line.split("\t") if p.strip()]
        best = (parts[-1] if parts else line).strip()
    return best


def _servers_in(data_dir: str) -> list:
    """Trade server names this terminal has connected to, alphabetically."""
    names = []
    try:
        entries = os.scandir(os.path.join(data_dir, "bases"))
    except OSError:
        return names
    with entries:
        for entry in entries:
            try:
                if not entry.is_dir():
                    continue
            except OSError:  # pragma: no cover - a vanished entry
                continue
            if entry.name.lower() in NON_SERVER_BASES or entry.name.startswith("."):
                continue
            names.append(entry.name)
    return sorted(names, key=str.lower)


#: exes that live in a MetaTrader 5 install directory but are not the terminal
_NOT_THE_TERMINAL = ("metaeditor", "uninstall", "updater", "setup", "install")


def _find_terminal_exe(install: str) -> str:
    """The terminal executable inside *install*, whatever the broker called it.

    ``terminal64.exe`` first, then ``terminal.exe``, and only then a scan of the
    directory. The scan is the part that matters: brokers white-label MetaTrader
    5 and ship the terminal under their own name — ``MetaTrader5-Deriv-Terminal64.exe``
    is a real example — and looking only for the two stock names reports such an
    install as missing, offers a path to a file that is not there, and (through
    :meth:`MT5Bridge._terminal_running`) refuses to connect to a terminal that is
    running perfectly well. Returns "" when nothing in the directory qualifies.
    """
    for candidate in ("terminal64.exe", "terminal.exe"):
        probe = os.path.join(install, candidate)
        if os.path.isfile(probe):
            return probe
    matches = []
    try:
        for entry in os.scandir(install):
            name = entry.name.lower()
            if not name.endswith(".exe") or "terminal" not in name:
                continue
            if any(word in name for word in _NOT_THE_TERMINAL):
                continue
            try:
                if entry.is_file():
                    matches.append(entry.path)
            except OSError:  # pragma: no cover
                continue
    except OSError:
        return ""
    # deterministic: 64-bit build first, then shortest name, then alphabetical,
    # so the same install never resolves two different ways between runs
    matches.sort(key=lambda p: ("terminal64" not in os.path.basename(p).lower(),
                                len(os.path.basename(p)),
                                os.path.basename(p).lower()))
    return matches[0] if matches else ""


def find_terminals(appdata: Optional[str] = None) -> list:
    """Every MetaTrader 5 terminal installed for this Windows user.

    Each entry is ``{"path", "dir", "name", "exists", "servers", "data_dir"}``
    where ``path`` is the terminal64.exe to put in ``account.terminal_path`` and
    ``servers`` is the list to offer for ``account.server``. Sorted so terminals
    whose exe is really there, with servers to offer, come first.

    Never raises: discovery failing must not stop anyone typing the details in
    by hand, which is always allowed.
    """
    root = os.environ.get("APPDATA") if appdata is None else appdata
    if not root:
        return []
    base = os.path.join(root, "MetaQuotes", "Terminal")
    found: list = []
    seen = set()
    try:
        entries = list(os.scandir(base))
    except OSError:
        return []

    for entry in entries:
        # Common/, Community/, Help/ are shared folders, not terminals; a real
        # terminal folder is a 32-character instance id
        if len(entry.name) != 32:
            continue
        try:
            if not entry.is_dir():
                continue
        except OSError:  # pragma: no cover
            continue
        install = _read_origin(os.path.join(entry.path, "origin.txt"))
        if not install:
            continue
        exe = _find_terminal_exe(install)
        path = exe or os.path.join(install, "terminal64.exe")
        key = path.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append({
            "path": path,
            "dir": install,
            "name": os.path.basename(install.rstrip("\\/")) or install,
            "exists": bool(exe),
            "servers": _servers_in(entry.path),
            "data_dir": entry.path,
        })

    # usable and informative first, then alphabetical so the order never jitters
    found.sort(key=lambda t: (not t["exists"], not t["servers"], t["name"].lower()))
    return found


def known_servers(terminals: Optional[list] = None) -> list:
    """Every distinct trade server name across *terminals*, de-duplicated."""
    out: list = []
    lowered = set()
    for terminal in (terminals if terminals is not None else find_terminals()):
        for name in terminal.get("servers") or []:
            if name.lower() not in lowered:
                lowered.add(name.lower())
                out.append(name)
    return sorted(out, key=str.lower)


# --------------------------------------------------------------------------- #
# bridge
# --------------------------------------------------------------------------- #


class BridgeReadError(RuntimeError):
    """The terminal could not be read — distinct from it answering "nothing".

    Only raised by reads whose empty answer would otherwise be acted on as fact.
    A caller that catches this should do nothing at all for that cycle: the
    correct response to "I don't know what you're holding" is never to decide.
    """


class MT5Bridge:
    """Thread-safe wrapper around the MetaTrader 5 terminal API."""

    def __init__(self, config: dict, logger: Optional[logging.Logger] = None) -> None:
        self.config = config or {}
        self.log = logger or logging.getLogger("infinity.bridge")
        self._lock = threading.RLock()
        self._connected = False
        self._blocked = False
        self._block_reason = ""
        self._real_account = False
        self._filling: Optional[int] = None
        self.symbol_name = ""
        self.last_stage = "idle"
        self.last_error = ""
        #: (login, server, unix_time) of the last connection that actually worked.
        #: Kept so a later refusal can be told apart from a wrong detail: if these
        #: same three details authorised an hour ago, "check your account number"
        #: is the wrong advice and sends the user hunting for a typo that is not
        #: there. Deliberately in memory only — it is evidence about this run.
        self._last_good: Optional[tuple] = None

    # -- internals ---------------------------------------------------------- #

    def _err(self) -> tuple:
        """(code, message, friendly) for the last MT5 error."""
        code, msg = -1, "unknown"
        try:
            code, msg = mt5.last_error()
        except Exception:
            pass
        hint = ERROR_HINTS.get(_i(code, -1), "")
        friendly = f"{msg} ({code})" if not hint else hint
        return _i(code, -1), str(msg), friendly

    def _with_log_reason(self, friendly: str, path: str, login: int = 0) -> str:
        """*friendly*, plus the terminal's own verdict when it recorded one.

        ``ERROR_HINTS`` can only describe a code, and ``-6`` covers "nobody is
        logged in", "wrong password" and "that account does not exist on that
        server" alike — three problems with three different fixes. The terminal
        writes which one it was to its own log, so quote it rather than make the
        person work through all three. Silent when there is nothing to add.
        """
        try:
            reason = _terminal_log_reason(path, login)
        except Exception:  # pragma: no cover - diagnosis must never break connect
            return friendly
        if not reason:
            return friendly
        self.log.error("terminal log says: %s", reason)
        return "%s\n\nMetaTrader 5's own log says: %s" % (friendly.rstrip(), reason)

    def _worked_before_note(self, login: int, server: str) -> str:
        """A correction to the generic ``-6`` advice when these details do work.

        ``-6`` sends the user to check the account number, the trade server name
        and the password. That is right for someone who has never got in, and
        exactly wrong for someone whose link has just dropped: after
        ``mt5.shutdown()`` the terminal is signed out, and a broker that refuses
        to re-authorise the same login from a cold API session reports it as
        *Invalid account* — the same words it uses for a number that does not
        exist. Telling someone to re-type credentials that authorised minutes ago
        wastes their time and invites them to edit settings that were correct.

        Only claimed when the login and server match a connection this process
        genuinely completed, and only for a real login: with ``login`` 0 nothing
        was ever authorised, so there is no such evidence to offer.
        """
        good = self._last_good
        if not good or not login:
            return ""
        if (int(login), str(server or "")) != (good[0], good[1]):
            return ""
        return (
            "\n\nThese exact details connected at %s, so the account number, the "
            "server name and the password are almost certainly not the problem — "
            "the terminal has been signed out and the broker is refusing to "
            "re-authorise from here. Log in to %s on %s inside the MetaTrader 5 "
            "window (File > Login to Trade Account), or close MetaTrader 5 and "
            "let this app start it again, then connect."
            % (time.strftime("%H:%M", time.localtime(good[2])),
               login, server or "the trade server")
        )

    def _fail(self, stage: str, error: str, **extra) -> dict:
        self.last_stage = stage
        self.last_error = error
        # "message" mirrors "error" so every caller (server, run.py, the UI notice)
        # gets the plain-English reason from whichever key it happens to read.
        out = {"ok": False, "stage": stage, "error": error, "message": error,
               "account": None, "terminal": None, "symbol": ""}
        out.update(extra)
        return out

    def _strategy_params(self) -> dict:
        key = str(self.config.get("engine", {}).get("active_strategy") or "")
        return dict(self.config.get("strategies", {}).get(key) or {})

    def _split_cap(self) -> float:
        return _f(self._strategy_params().get("volume_split_max_lot"), 0.0)

    def _magic(self) -> int:
        return _i(self.config.get("engine", {}).get("magic"), 100100)

    def _known_terminal_names(self) -> list:
        """Image names a MetaTrader 5 terminal on this machine could be using.

        The configured path first, then the two stock names, then the real
        basename of every install discovery can see — which is where a
        white-labelled ``MetaTrader5-Deriv-Terminal64.exe`` enters the picture.
        """
        names = []
        configured = os.path.basename(str((self.config.get("account") or {})
                                          .get("terminal_path") or "")).strip()
        if configured:
            names.append(configured)
        names.extend(("terminal64.exe", "terminal.exe"))
        try:
            for terminal in find_terminals():
                base = os.path.basename(str(terminal.get("path") or "")).strip()
                if base:
                    names.append(base)
        except Exception as exc:  # noqa: BLE001 - discovery must never decide this
            self.log.debug("terminal discovery failed during probe: %s", exc)
        out, seen = [], set()
        for name in names:
            if name.lower() not in seen:
                seen.add(name.lower())
                out.append(name)
        return out

    def _terminal_running(self) -> bool:
        """Is a MetaTrader 5 terminal process alive right now?

        Cheap guard in front of ``mt5.initialize()``, which holds the GIL for its
        whole timeout. ``tasklist`` runs in its own process, so this costs the UI
        nothing. If the check itself cannot run we return True and let MT5 decide
        — a missing answer must never be read as "definitely closed".

        One unfiltered dump, matched here, rather than one ``/FI IMAGENAME eq``
        probe per name we thought to guess. Guessing was the bug: brokers ship
        MetaTrader 5 under their own name, and a probe list of
        ``("terminal64.exe", "terminal.exe")`` answers "no terminal" about a
        running ``MetaTrader5-Deriv-Terminal64.exe`` — after which the app
        declines to connect to a broker that works fine. So a process counts if
        it matches a name we know about *or* simply looks like a terminal, and
        any terminal that has ever been launched is registered under
        ``%APPDATA%\\MetaQuotes\\Terminal`` for discovery to find.

        ``/FO CSV`` is not cosmetic either: the default table format pads the
        Image Name column to 25 characters and *truncates* anything longer, so
        that same 32-character Deriv name comes back as
        ``MetaTrader5-Deriv-Termina`` and no comparison can match it. Verified
        against a real process of that name: table format missed it, CSV matched.
        """
        if os.name != "nt":
            return True
        known = {n.lower() for n in self._known_terminal_names()}
        try:
            out = subprocess.run(
                ["tasklist", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, timeout=8,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            ).stdout or ""
        except (OSError, subprocess.SubprocessError) as exc:
            self.log.debug("tasklist probe failed (%s); letting MT5 decide", exc)
            return True

        for row in csv.reader(io.StringIO(out)):
            if not row:
                continue
            image = row[0].strip()
            lowered = image.lower()
            if not lowered.endswith(".exe"):
                continue
            if lowered in known:
                return True
            # a name nobody told us about, but there is only one program this
            # can be; excluding metaeditor keeps the IDE from counting
            if "terminal" in lowered and not any(
                    word in lowered for word in _NOT_THE_TERMINAL):
                self.log.info("found a terminal under an unfamiliar name: %s", image)
                return True

        self.log.info("no MetaTrader 5 process found (looked for %s and anything "
                      "else named like a terminal) — skipping initialize()",
                      ", ".join(sorted(known)))
        return False

    # -- lifecycle ---------------------------------------------------------- #

    def connect(self) -> dict:
        """Initialise the terminal, log in, verify safety, resolve the symbol."""
        with self._lock:
            self._connected = False
            self._blocked = False
            self._block_reason = ""
            self._real_account = False
            self._filling = None

            if not MT5_AVAILABLE:
                return self._fail(
                    "import",
                    "The MetaTrader5 Python package is not importable: " + MT5_IMPORT_ERROR,
                )

            acct = dict(self.config.get("account") or {})
            path = str(acct.get("terminal_path") or "").strip()

            # mt5.initialize() is a native call that holds the GIL until it either
            # connects or times out — nothing else in this process runs meanwhile.
            # So keep the timeout short enough that a stall is a blip, not a hang.
            timeout_ms = _i(acct.get("connect_timeout_ms"), 10000)
            timeout_ms = max(2000, min(60000, timeout_ms))

            exe_there = bool(path) and os.path.exists(path)
            if not self._terminal_running():
                # initialize(path=...) starts the terminal itself, and that is the
                # whole point of asking for terminal_path: someone who has just
                # typed their three details into LINK BROKER should not also have
                # to go and launch MetaTrader 5 by hand. Refusing here made the app
                # unable to connect from cold, which reads as "the link form does
                # nothing". Only refuse when there is no exe to launch.
                if not exe_there:
                    return self._fail(
                        "terminal",
                        "MetaTrader 5 is not running and I have no terminal to start: %s. "
                        "Open MetaTrader 5, or put the path to its terminal64.exe in "
                        "LINK BROKER." % (path or "no terminal_path set"),
                    )
                # a cold launch is slower than attaching to a live terminal, and a
                # 10s default times out mid-splash-screen
                timeout_ms = max(timeout_ms, 30000)
                self.log.info("no terminal running — asking initialize() to start %s", path)

            kwargs: dict = {"timeout": timeout_ms}
            if exe_there:
                kwargs["path"] = path
            elif path:
                self.log.warning("terminal_path does not exist: %s", path)

            login = _i(acct.get("login"), 0)
            password = str(acct.get("password") or "")
            server = str(acct.get("server") or "")

            # initialize() authorises by itself when it is handed credentials, and
            # that is the only way into a terminal nobody has logged into yet.
            # Without them it fails with "no trading account is logged in" — and
            # because it fails, the mt5.login() call below never runs, so the
            # login/password/server someone typed into LINK BROKER were only ever
            # able to *switch* accounts on a terminal a human had already signed
            # into by hand. A freshly installed terminal is exactly the state a new
            # user is in, so the credentials have to be available here too.
            if login:
                kwargs["login"] = login
                if password:
                    kwargs["password"] = password
                if server:
                    kwargs["server"] = server

            try:
                started = bool(mt5.initialize(**kwargs))
            except Exception as exc:
                return self._fail("init", "initialize() raised: %s" % exc)

            if not started:
                code, msg, friendly = self._err()
                self.log.error("mt5.initialize failed: %s %s", code, msg)
                return self._fail("init",
                                  self._with_log_reason(friendly, path, login)
                                  + self._worked_before_note(login, server),
                                  code=code, raw=msg)

            # Still worth doing after a credentialed initialize(): it is what
            # re-points an already-running terminal that is signed into a
            # different account, and it re-authorises idempotently otherwise.
            if login:
                try:
                    logged_in = bool(
                        mt5.login(login, password=password, server=server,
                                  timeout=timeout_ms)
                    )
                except Exception as exc:
                    return self._fail("login", "login() raised: %s" % exc)
                if not logged_in:
                    code, msg, friendly = self._err()
                    self.log.error("mt5.login failed for %s@%s: %s %s", login, server, code, msg)
                    return self._fail(
                        "login",
                        self._with_log_reason(
                            "Login to %s on %s was refused. %s"
                            % (login, server or "(no server)", friendly), path, login)
                        + self._worked_before_note(login, server),
                        code=code,
                        raw=msg,
                    )

            account = self.account()
            if not account:
                code, msg, friendly = self._err()
                return self._fail(
                    "login",
                    friendly if code in ERROR_HINTS else
                    "The terminal answered but no account information is available. "
                    "Log in to an account inside MetaTrader 5, or set login / password / "
                    "server in config.json.",
                    code=code,
                    raw=msg,
                )

            terminal = _asdict(mt5.terminal_info())
            terminal = {
                "company": str(terminal.get("company", "")),
                "name": str(terminal.get("name", "")),
                "path": str(terminal.get("path", "")),
                "connected": bool(terminal.get("connected", False)),
                "trade_allowed": bool(terminal.get("trade_allowed", False)),
                "build": _i(terminal.get("build"), 0),
            }

            # `is not True`, not `not bool(...)`. Consent to trade real money is
            # the one flag in this app that must be the literal boolean True and
            # nothing else: `bool("false")` is True, so a coercion missed
            # anywhere upstream would turn the word "no" into permission. This
            # check is deliberately redundant with sanitize_config() — the point
            # of a safety interlock is that it does not depend on the caller.
            self._real_account = bool(account.get("is_real"))
            if self._real_account and acct.get("allow_live_real") is not True:
                self._blocked = True
                self._block_reason = (
                    "Account %s on %s is a REAL money account. Trading is blocked because "
                    "account.allow_live_real is false in config.json. Set it to true only if "
                    "you intend to trade real money."
                    % (account.get("login"), account.get("server"))
                )
                self.log.error("SAFETY BLOCK: %s", self._block_reason)
                # and then carry on connecting. This used to return here, which
                # meant _connected stayed False and the UI showed "Not connected"
                # while telling the user "A real account will still connect and be
                # shown" — the interface lied, and worse, the refusal to connect
                # was the *only* thing standing between a real account and
                # automated trading. Both are fixed by connecting read-only: the
                # account, its equity and its positions are visible, and the three
                # independent interlocks below (_guard on every order, blocked() on
                # every deploy, and the live re-check in _real_money_block) are
                # what actually keep the money still.

            symbol = self._resolve_symbol()
            if not symbol:
                return self._fail(
                    "symbol",
                    "None of the configured symbols could be found or quoted on this broker. "
                    "Checked: %s" % ", ".join(self._candidates()),
                    account=account,
                    terminal=terminal,
                )

            self.symbol_name = symbol
            self._connected = True
            # What was *asked for*, not what came back: the next attempt sends
            # these same two values, so these are what a later refusal has to be
            # compared against. They differ when login is 0 and the terminal
            # supplies the account itself.
            self._last_good = (int(login), str(server or ""), time.time())
            # "ok" means the link is up, not that trading is permitted. A blocked
            # real account is a successful read-only connection and says so, so
            # nothing downstream mistakes visibility for consent.
            self.last_stage = "read_only" if self._blocked else "ok"
            self.last_error = self._block_reason
            self.log.info(
                "connected%s: %s on %s (%s) symbol=%s",
                " read-only" if self._blocked else "",
                account.get("login"), account.get("server"),
                account.get("trade_mode"), symbol,
            )
            return {
                "ok": True,
                "stage": self.last_stage,
                "error": self._block_reason,
                "account": account,
                "terminal": terminal,
                "symbol": symbol,
                "blocked": self._blocked,
                "block_reason": self._block_reason,
            }

    def disconnect(self) -> None:
        with self._lock:
            self._connected = False
            if MT5_AVAILABLE:
                try:
                    mt5.shutdown()
                except Exception:
                    pass

    def is_connected(self) -> bool:
        if not self._connected or not MT5_AVAILABLE:
            return False
        try:
            return mt5.terminal_info() is not None
        except Exception:
            return False

    def blocked(self) -> tuple:
        # the live re-check as well as the cached flag, so the deploy gate in
        # TradingEngine.start() and the UI banner both react the moment consent is
        # revoked — not on the next reconnect
        if self._blocked:
            return True, self._block_reason
        reason = self._real_money_block()
        return (True, reason) if reason else (False, "")

    # -- symbol resolution -------------------------------------------------- #

    def _candidates(self) -> list:
        cfg = dict(self.config.get("symbol") or {})
        wanted = str(cfg.get("name") or "auto").strip()
        cands = [str(c) for c in (cfg.get("candidates") or []) if str(c).strip()]
        if wanted and wanted.lower() != "auto":
            cands.insert(0, wanted)
        seen, out = set(), []
        for c in cands:
            if c.lower() not in seen:
                seen.add(c.lower())
                out.append(c)
        return out

    #: How long to let a freshly selected symbol produce its first tick.
    #: symbol_select() only adds the symbol to Market Watch; the quote arrives
    #: afterwards, over the network, on the terminal's own schedule. Demanding a
    #: price in the same breath as the select is a race, and a cold-launched
    #: terminal loses it every time — which failed the whole connect at the
    #: "symbol" stage on a broker whose gold was live and tradable the entire time.
    _QUOTE_SETTLE_S = 2.5

    def _quoted(self, name: str) -> bool:
        """Is *name* showing a price right now?

        The tick is the live truth, but symbol_info carries the last known bid/ask
        too and has it slightly sooner after a select, so accept either.
        """
        try:
            tick = mt5.symbol_info_tick(name)
            if tick is not None and (_f(getattr(tick, "bid", 0)) > 0
                                     or _f(getattr(tick, "ask", 0)) > 0):
                return True
            info = mt5.symbol_info(name)
            return bool(info) and (_f(getattr(info, "bid", 0)) > 0
                                   or _f(getattr(info, "ask", 0)) > 0)
        except Exception:
            return False

    def _try_select(self, name: str, settle_s: float = 0.0,
                    require_quote: bool = True) -> bool:
        """Put *name* in Market Watch and decide whether it is usable.

        With ``require_quote`` the symbol has to actually price within
        *settle_s*; without it, being selectable and tradable is enough. The
        second form exists for genuinely closed markets: a broker that lists gold,
        allows trading it, and simply is not quoting at 2am on a Sunday should
        still let someone link their account and see it — refusing to connect
        until a tick appears makes the whole app unusable outside market hours.
        """
        try:
            if not mt5.symbol_select(name, True):
                return False
            info = mt5.symbol_info(name)
            if info is None:
                return False
        except Exception:
            return False
        if not require_quote:
            # 0 is SYMBOL_TRADE_MODE_DISABLED; anything else can hold a position
            return _i(getattr(info, "trade_mode", 0)) != 0
        deadline = time.monotonic() + max(0.0, settle_s)
        while True:
            if self._quoted(name):
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.05)

    def _resolve_symbol(self) -> str:
        cands = self._candidates()
        try:
            names = [s.name for s in (mt5.symbols_get() or [])]
        except Exception:
            names = []
        lookup = {n.lower(): n for n in names}

        def search(require_quote: bool, settle_s: float) -> str:
            # only the first probe of a pass waits out the settle window; once one
            # symbol has quoted, the terminal's feed is up and the rest are instant
            budget = [settle_s]

            def ok(name: str) -> bool:
                good = self._try_select(name, settle_s=budget[0],
                                        require_quote=require_quote)
                budget[0] = 0.0
                return good

            for c in cands:  # exact, case-insensitive
                real = lookup.get(c.lower())
                if real and ok(real):
                    return real
            for c in cands:  # broker suffix, e.g. XAUUSD -> XAUUSD.pro
                cl = c.lower()
                for n in names:
                    if n.lower().startswith(cl) and ok(n):
                        return n
            for n in names:  # last resort: anything that looks like gold
                nl = n.lower()
                if ("xau" in nl or "gold" in nl) and ok(n):
                    return n
            for c in cands:  # broker may not expose symbols_get() but still quote it
                if ok(c):
                    return c
            return ""

        found = search(require_quote=True, settle_s=self._QUOTE_SETTLE_S)
        if found:
            return found
        # nothing is pricing. Take a tradable one anyway and say so: the engine
        # has its own staleness checks and will not trade on a dead quote, but the
        # account, its equity and its positions are worth showing either way.
        found = search(require_quote=False, settle_s=0.0)
        if found:
            self.log.warning(
                "%s selected but not quoting — linking anyway (market closed?)", found)
        return found

    # -- state -------------------------------------------------------------- #

    def account(self) -> Optional[dict]:
        if not MT5_AVAILABLE:
            return None
        try:
            raw = _asdict(mt5.account_info())
        except Exception:
            return None
        if not raw:
            return None
        trade_mode = _i(raw.get("trade_mode"), 0)
        return {
            "login": _i(raw.get("login"), 0),
            "name": str(raw.get("name", "")),
            "server": str(raw.get("server", "")),
            "company": str(raw.get("company", "")),
            "currency": str(raw.get("currency", "USD")),
            "balance": _f(raw.get("balance")),
            "equity": _f(raw.get("equity")),
            "profit": _f(raw.get("profit")),
            "margin": _f(raw.get("margin")),
            "margin_free": _f(raw.get("margin_free")),
            "margin_level": _f(raw.get("margin_level")),
            "leverage": _i(raw.get("leverage"), 0),
            "trade_mode": {0: "DEMO", 1: "CONTEST", 2: "REAL"}.get(trade_mode, "DEMO"),
            "trade_mode_code": trade_mode,
            "is_real": trade_mode == 2,
            "trade_allowed": bool(raw.get("trade_allowed", False)),
            "so_call": _f(raw.get("margin_so_call"), 100.0),
            "so_so": _f(raw.get("margin_so_so"), 50.0),
        }

    def symbol_info(self) -> Optional[dict]:
        if not MT5_AVAILABLE or not self.symbol_name:
            return None
        try:
            raw = _asdict(mt5.symbol_info(self.symbol_name))
        except Exception:
            return None
        if not raw:
            return None
        bid, ask = _f(raw.get("bid")), _f(raw.get("ask"))
        return {
            "name": str(raw.get("name", self.symbol_name)),
            "description": str(raw.get("description", "")),
            "bid": bid,
            "ask": ask,
            "spread_usd": round(ask - bid, 5) if ask and bid else 0.0,
            "point": _f(raw.get("point"), 0.01),
            "digits": _i(raw.get("digits"), 2),
            "volume_min": _f(raw.get("volume_min"), 0.01),
            "volume_max": _f(raw.get("volume_max"), 100.0),
            "volume_step": _f(raw.get("volume_step"), 0.01),
            "trade_contract_size": _f(raw.get("trade_contract_size"), 100.0),
            "trade_mode": _i(raw.get("trade_mode"), 0),
            "trade_tick_value": _f(raw.get("trade_tick_value"), 0.0),
            # Minimum distance the broker will accept between price and a stop, in
            # points. Sending a closer stop is rejected with "Invalid stops", so a
            # bracket has to be clamped to it rather than assumed to fit.
            "stops_level": _i(raw.get("trade_stops_level"), 0),
            "freeze_level": _i(raw.get("trade_freeze_level"), 0),
            "margin_per_lot": self._margin_per_lot(
                ask or bid, _f(raw.get("volume_min"), 0.01)
            ),
        }

    def _margin_per_lot(self, price: float, volume_min: float) -> float:
        """What the broker will actually charge to hold one lot, or 0.0 if unknown.

        The risk manager used to work this out as ``price * contract_size /
        leverage``, which is the textbook formula and is wrong on any broker that
        sets a per-symbol margin rate — which is most of them. Measured on this
        Deriv account: gold costs 5.00% of notional (1:20 effective) while the
        account reports 1:30, so the formula under-charged by exactly 1.500x at
        every volume tested. Sizing on it produced positions the margin floor
        force-closed one tick later, over and over, at a spread a time.

        ``order_calc_margin`` is the terminal answering for the broker, so it needs
        no per-broker table and no guessing. Returning 0.0 when it cannot be called
        keeps the old formula reachable as a fallback rather than blocking trading
        on a terminal that will not answer.

        Margin is assumed linear in volume, which is what makes a per-*lot* number
        meaningful at all. Brokers with volume-tiered rates break that assumption,
        so 1 lot is quoted directly where possible rather than scaled up from the
        minimum: it is closer to the sizes actually traded.
        """
        if not MT5_AVAILABLE or not self.symbol_name or price <= 0:
            return 0.0
        for volume, scale in ((1.0, 1.0), (volume_min, 1.0 / volume_min if volume_min else 0.0)):
            if volume <= 0 or scale <= 0:
                continue
            try:
                cost = mt5.order_calc_margin(
                    mt5.ORDER_TYPE_BUY, self.symbol_name, volume, price
                )
            except Exception:
                continue
            cost = _f(cost)
            if cost > 0:
                return round(cost * scale, 4)
        return 0.0

    def tick(self) -> Optional[dict]:
        if not MT5_AVAILABLE or not self.symbol_name:
            return None
        try:
            raw = _asdict(mt5.symbol_info_tick(self.symbol_name))
        except Exception:
            return None
        if not raw:
            return None
        bid, ask = _f(raw.get("bid")), _f(raw.get("ask"))
        return {
            "bid": bid,
            "ask": ask,
            "last": _f(raw.get("last")),
            "time": _i(raw.get("time"), 0),
            "spread_usd": round(ask - bid, 5) if ask and bid else 0.0,
        }

    def positions(self, magic: Optional[int] = None) -> list:
        """Open positions, or raise :class:`BridgeReadError` if they can't be read.

        Raising rather than returning ``[]`` is the whole point. An empty list is
        a claim — "this account is flat" — and every caller acts on it: the engine
        books a basket as closed and narrates it, and ``close_all`` reports
        "already flat" and stops trying. A failed read must not be able to make
        either of those statements.

        ``positions_get`` signals failure by returning None and an empty account by
        returning an empty tuple, so the two are separated here rather than being
        flattened together by ``raw or []``.
        """
        if not MT5_AVAILABLE:
            return []
        try:
            raw = mt5.positions_get(symbol=self.symbol_name) if self.symbol_name else mt5.positions_get()
        except Exception as exc:  # noqa: BLE001
            raise BridgeReadError("positions_get raised: %s" % exc) from exc
        if raw is None:
            raise BridgeReadError("positions_get failed: %s" % (self._err()[2],))
        out = []
        for p in raw:
            d = _asdict(p)
            if magic is not None and _i(d.get("magic"), 0) != int(magic):
                continue
            out.append(
                {
                    "ticket": _i(d.get("ticket"), 0),
                    "symbol": str(d.get("symbol", "")),
                    "type": "buy" if _i(d.get("type"), 0) == 0 else "sell",
                    "volume": _f(d.get("volume")),
                    "price_open": _f(d.get("price_open")),
                    "price_current": _f(d.get("price_current")),
                    "profit": _f(d.get("profit")),
                    "swap": _f(d.get("swap")),
                    # The bracket the broker is actually holding. 0.0 means there is
                    # none — which is the state every position opened before the
                    # order path attached one was in, and the state a trailing stop
                    # ratchets away from. A strategy cannot decide whether moving a
                    # stop reduces risk without being able to see where it is.
                    "sl": _f(d.get("sl")),
                    "tp": _f(d.get("tp")),
                    "magic": _i(d.get("magic"), 0),
                    "time": _i(d.get("time"), 0),
                    "comment": str(d.get("comment", "")),
                }
            )
        out.sort(key=lambda p: p["time"])
        return out

    def candles(self, timeframe: str = "M5", count: int = 200) -> list:
        if not MT5_AVAILABLE or not self.symbol_name:
            return []
        tf_map = {
            "M1": getattr(mt5, "TIMEFRAME_M1", 1),
            "M5": getattr(mt5, "TIMEFRAME_M5", 5),
            "M15": getattr(mt5, "TIMEFRAME_M15", 15),
            "M30": getattr(mt5, "TIMEFRAME_M30", 30),
            "H1": getattr(mt5, "TIMEFRAME_H1", 16385),
            "H4": getattr(mt5, "TIMEFRAME_H4", 16388),
            "D1": getattr(mt5, "TIMEFRAME_D1", 16408),
        }
        tf = tf_map.get(str(timeframe).upper(), tf_map["M5"])
        try:
            rates = mt5.copy_rates_from_pos(self.symbol_name, tf, 0, max(2, int(count)))
        except Exception:
            return []
        out = []
        for r in rates if rates is not None else []:
            try:
                out.append(
                    {
                        "time": _i(r[0], 0),
                        "open": _f(r[1]),
                        "high": _f(r[2]),
                        "low": _f(r[3]),
                        "close": _f(r[4]),
                    }
                )
            except Exception:
                continue
        return out

    #: How far back a tick request reaches when the caller does not say. Two
    #: minutes of gold in a live session is a few hundred ticks; on a quiet one it
    #: is a dozen, and a dozen is the honest answer rather than a padded one.
    TICK_WINDOW_S = 120.0

    def ticks(self, count: int = 300, window_s: float = 0.0) -> list:
        """The most recent *count* ticks, oldest first. ``[]`` when there are none.

        Candles are the wrong instrument for a scalper. An M1 bar is a summary
        written 60 seconds after the fact, so a strategy that reads velocity or
        tick-direction imbalance off closes is measuring something that has already
        stopped existing. This is the raw stream the terminal keeps anyway.

        The window is anchored on the **last tick's own timestamp**, never on this
        machine's clock. ``copy_ticks_*`` read a naive datetime as UTC while MT5
        stamps ticks in the broker's server time — Deriv runs UTC+3 — so anchoring
        on ``utcnow()`` asks for a window three hours in the past and gets stale
        ticks or none at all, with nothing in the result to say which. Asking the
        terminal when its own last tick was takes the offset out of the arithmetic
        entirely, for any broker in any timezone.

        Two calls, because they fail in different places: ``copy_ticks_range`` is
        the one that can express "the newest N", and ``copy_ticks_from`` is the one
        that makes the terminal fetch history it has not cached yet. Failure is
        ``[]``, exactly as :meth:`candles` — a broker that serves no tick history is
        a real configuration, and the caller's job is to say so out loud rather than
        to be handed invented ticks.

        **Timestamps are returned exactly as the terminal reports them, and the
        caller must check them.** Measured on this Deriv-Demo terminal: the range
        call came back empty while ``copy_ticks_from`` answered with 300 ticks at
        6/second — a perfectly well-formed buffer whose newest tick was 5 hours 28
        minutes and 27 points of gold behind the live quote, because the terminal's
        tick cache for the symbol simply ended there. Nothing in the shape of that
        list says so. A reader compares ``ticks[-1]["time"]`` against
        ``tick()["time"]`` — both are broker server time, so the difference is
        staleness in seconds and nothing else — and declines to trade on it. That
        check is not defensive coding; it is the difference between a scalper
        trading now and a scalper trading this morning.
        """
        if not MT5_AVAILABLE or not self.symbol_name:
            return []
        want = max(2, int(count or 0))
        try:
            window = float(window_s or 0.0)
        except (TypeError, ValueError):
            window = 0.0
        window = window if window > 0 else self.TICK_WINDOW_S

        anchor = 0.0
        try:
            latest = mt5.symbol_info_tick(self.symbol_name)
        except Exception:
            latest = None
        if latest is not None:
            anchor = (_f(getattr(latest, "time_msc", 0)) / 1000.0
                      or _f(getattr(latest, "time", 0)))
        if anchor <= 0:
            anchor = time.time()

        flags = getattr(mt5, "COPY_TICKS_ALL", 3)
        start = _epoch_to_naive_utc(anchor - window)
        # +60s past the anchor: the terminal keeps ticking while this runs, and an
        # end bound sitting exactly on the anchor drops the newest tick — the one a
        # scalper cares about more than all the others.
        end = _epoch_to_naive_utc(anchor + 60.0)
        rows = None
        try:
            rows = mt5.copy_ticks_range(self.symbol_name, start, end, flags)
        except Exception:
            rows = None
        if rows is None or len(rows) < 2:
            # Nothing cached for this window yet. ``copy_ticks_from`` is the call
            # that makes the terminal go and download, but on a cold cache it
            # answers first and asks questions later: measured here, it returned
            # the tail of a five-hour-old cache. So it is used as a *warm-up* and
            # the range call is put again — which is the call that either has the
            # freshly downloaded ticks or honestly has nothing. Only if the second
            # range call is still short does the warm-up's own answer go back, with
            # its real timestamps, for the caller to judge.
            warm = None
            try:
                warm = mt5.copy_ticks_from(self.symbol_name, start, want, flags)
            except Exception:
                warm = None
            try:
                rows = mt5.copy_ticks_range(self.symbol_name, start, end, flags)
            except Exception:
                rows = None
            if rows is None or len(rows) < 2:
                rows = warm

        out = []
        for r in rows if rows is not None else []:
            try:
                bid, ask = _f(r[1]), _f(r[2])
                if bid <= 0 and ask <= 0:
                    continue
                msc = _f(r[5])
                out.append(
                    {
                        "time": (msc / 1000.0) if msc > 0 else _f(r[0]),
                        "bid": bid,
                        "ask": ask,
                        # Both derived here rather than in every reader: a tick with
                        # only one side quoted is normal on an illiquid feed, and the
                        # fallback has to be the same one everywhere or two callers
                        # disagree about what the price was.
                        "mid": ((bid + ask) / 2.0) if (bid > 0 and ask > 0) else (bid or ask),
                        "spread_usd": round(ask - bid, 6) if (bid > 0 and ask > 0) else 0.0,
                    }
                )
            except Exception:
                continue
        out.sort(key=lambda t: t["time"])
        return out[-want:]

    # -- execution ---------------------------------------------------------- #

    def _filling_modes(self) -> list:
        order = [
            getattr(mt5, "ORDER_FILLING_FOK", 0),
            getattr(mt5, "ORDER_FILLING_IOC", 1),
            getattr(mt5, "ORDER_FILLING_RETURN", 2),
        ]
        if self._filling is not None and self._filling in order:
            order.remove(self._filling)
            order.insert(0, self._filling)
        return order

    def _real_money_block(self) -> Optional[str]:
        """Reason this real account may not be traded, or None.

        Deliberately independent of ``_blocked``. That field is set once, during
        connect(), from the config as it read at that moment; this is re-derived on
        every single order from the *live* config, so revoking consent through
        ``POST /api/config`` stops the next order rather than the next reconnect.
        ``_real_account`` is what the broker itself reported at login — never a
        config value — so a config edit cannot talk the app out of the fact that
        real money is at stake.
        """
        if not self._real_account:
            return None
        acct = self.config.get("account") or {}
        if acct.get("allow_live_real") is True:
            return None
        return ("Refusing to trade real-money account %s: account.allow_live_real "
                "is not enabled." % (acct.get("login") or "?"))

    def _guard(self) -> Optional[dict]:
        """Refuse execution when unsafe. Returns an error dict or None."""
        if not MT5_AVAILABLE:
            return {"ok": False, "retcode": 0, "comment": "MetaTrader5 package unavailable",
                    "tickets": [], "detail": MT5_IMPORT_ERROR}
        if self._blocked:
            return {"ok": False, "retcode": 0, "comment": "blocked by safety policy",
                    "tickets": [], "detail": self._block_reason}
        # Second, independent pass over the same question. _blocked above is a
        # cached decision; this one is fresh. Both must agree before real money
        # moves, and there is no code path to an order that skips this function.
        reason = self._real_money_block()
        if reason:
            self.log.error("SAFETY BLOCK: %s", reason)
            return {"ok": False, "retcode": 0, "comment": "blocked by safety policy",
                    "tickets": [], "detail": reason}
        if not self.is_connected():
            return {"ok": False, "retcode": 0, "comment": "not connected",
                    "tickets": [], "detail": self.last_error or "bridge is not connected"}
        return None

    def _send(self, request: dict) -> dict:
        """Send one deal request, trying each filling mode in turn."""
        last = {"retcode": 0, "comment": "no attempt", "order": 0, "deal": 0, "price": 0.0}
        for mode in self._filling_modes():
            req = dict(request)
            req["type_filling"] = mode
            try:
                result = mt5.order_send(req)
            except Exception as exc:
                last = {"retcode": -1, "comment": "order_send raised: %s" % exc,
                        "order": 0, "deal": 0, "price": 0.0}
                continue
            if result is None:
                code, msg, friendly = self._err()
                last = {"retcode": code, "comment": friendly, "order": 0, "deal": 0, "price": 0.0}
                continue
            r = _asdict(result)
            retcode = _i(r.get("retcode"), 0)
            last = {
                "retcode": retcode,
                "comment": str(r.get("comment", "")),
                "order": _i(r.get("order"), 0),
                "deal": _i(r.get("deal"), 0),
                "price": _f(r.get("price")),
                "volume": _f(r.get("volume")),
                "filling": mode,
            }
            if retcode == getattr(mt5, "TRADE_RETCODE_DONE", 10009):
                self._filling = mode
                last["ok"] = True
                return last
            # only a filling-mode rejection is worth retrying with another mode
            if retcode not in (
                getattr(mt5, "TRADE_RETCODE_INVALID_FILL", 10030),
                getattr(mt5, "TRADE_RETCODE_UNSUPPORTED_FILL_POLICY", 10030),
            ):
                break
        last["ok"] = False
        return last

    def market_order(self, side: str, volume: float, comment: str = "INFINITY.100",
                     sl_distance: float = 0.0, tp_distance: float = 0.0) -> dict:
        """Open at market, splitting *volume* into broker-legal chunks.

        ``sl_distance``/``tp_distance`` are distances in *price*, not money, and
        attach a real stop and target to each chunk at the broker. Passing 0 leaves
        that side unset.

        Attaching them matters more than it looks. Before this the engine was the
        only thing enforcing a stop, by polling once a second and then sending a
        close — so a stop was never a stop, it was a request checked up to a second
        late. Measured on this account: a 2.40 stop closed at -3.21 and -4.08, i.e.
        34% and 70% past where it was supposed to, which inverts a bracket designed
        to risk 2.40 to make 3.60. A broker-side stop is enforced tick by tick, and
        survives the app crashing, the connection dropping or the machine sleeping —
        none of which the polled version does.
        """
        guard = self._guard()
        if guard:
            return guard
        side = str(side).lower().strip()
        if side not in ("buy", "sell"):
            return {"ok": False, "retcode": 0, "comment": "bad side %r" % side,
                    "tickets": [], "detail": "side must be 'buy' or 'sell'"}

        with self._lock:
            si = self.symbol_info()
            tk = self.tick()
            if not si or not tk:
                return {"ok": False, "retcode": 0, "comment": "no quote",
                        "tickets": [], "detail": "symbol info or tick unavailable"}

            chunks = split_volume(
                volume, self._split_cap(), si["volume_min"], si["volume_max"], si["volume_step"]
            )
            if not chunks:
                return {"ok": False, "retcode": 0, "comment": "volume too small",
                        "tickets": [],
                        "detail": "%.4f lots is below the broker minimum of %.2f"
                                  % (_f(volume), si["volume_min"])}

            order_type = (getattr(mt5, "ORDER_TYPE_BUY", 0) if side == "buy"
                          else getattr(mt5, "ORDER_TYPE_SELL", 1))
            magic = self._magic()
            tickets, fills, errors = [], [], []
            sl_distance, tp_distance = _f(sl_distance), _f(tp_distance)

            for chunk in chunks:
                price = self.tick() or tk
                entry = price["ask"] if side == "buy" else price["bid"]
                req = {
                    "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
                    "symbol": self.symbol_name,
                    "volume": chunk,
                    "type": order_type,
                    "price": entry,
                    "deviation": 20,
                    "magic": magic,
                    "comment": str(comment)[:31],
                    "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
                }
                stops = self._stops_for(si, side, entry, sl_distance, tp_distance)
                req.update(stops)
                res = self._send(req)
                if not res.get("ok") and stops and _invalid_stops(res.get("retcode")):
                    # Some brokers refuse a bracket on the deal itself, or measure
                    # the minimum distance differently than symbol_info reports.
                    # Getting filled without a stop and then adding one is better
                    # than not trading; getting filled and staying naked is not, so
                    # the follow-up is attempted immediately below.
                    self.log.warning("broker rejected the attached bracket (%s) — "
                                     "opening bare and setting stops after the fill",
                                     res.get("comment"))
                    res = self._send({k: v for k, v in req.items()
                                      if k not in ("sl", "tp")})
                    if res.get("ok"):
                        self._set_stops(res["order"], si, side,
                                        _f(res.get("price")) or entry,
                                        sl_distance, tp_distance)
                if res.get("ok"):
                    tickets.append(res["order"])
                    fills.append({"ticket": res["order"], "volume": chunk,
                                  "price": res.get("price", 0.0)})
                else:
                    errors.append("%.2f lots: %s (%s)"
                                  % (chunk, res.get("comment", ""), res.get("retcode")))
                    self.log.error("order chunk failed: %s", errors[-1])

            ok = bool(tickets)
            detail = "%d/%d chunks filled" % (len(tickets), len(chunks))
            if errors:
                detail += " | partial: " + "; ".join(errors[:4])
            return {
                "ok": ok,
                "retcode": getattr(mt5, "TRADE_RETCODE_DONE", 10009) if ok else 0,
                "comment": "%s %s %.2f lots" % (side, self.symbol_name, sum(chunks)),
                "tickets": tickets,
                "fills": fills,
                "detail": detail,
            }

    def _stops_for(self, si: dict, side: str, entry: float,
                   sl_distance: float, tp_distance: float) -> dict:
        """Absolute sl/tp levels for an entry, or ``{}`` when neither was asked for.

        Each distance is widened to the broker's own minimum if it is tighter, and
        rounded to the symbol's digits. A stop inside the minimum is not "nearly
        allowed" — the whole order is rejected — so silently widening it is the only
        way to keep a tight bracket tradable, and the caller is told nothing because
        there is nothing it could usefully do differently.
        """
        if sl_distance <= 0 and tp_distance <= 0:
            return {}
        point = _f(si.get("point"), 0.01) or 0.01
        digits = _i(si.get("digits"), 2)
        floor = _i(si.get("stops_level"), 0) * point
        out = {}
        if sl_distance > 0:
            gap = max(sl_distance, floor)
            out["sl"] = round(entry - gap if side == "buy" else entry + gap, digits)
        if tp_distance > 0:
            gap = max(tp_distance, floor)
            out["tp"] = round(entry + gap if side == "buy" else entry - gap, digits)
        return out

    def _set_stops(self, ticket: int, si: dict, side: str, entry: float,
                   sl_distance: float, tp_distance: float) -> bool:
        """Attach stops to an already-open position. Used only as a fallback."""
        stops = self._stops_for(si, side, entry, sl_distance, tp_distance)
        if not stops:
            return False
        req = {
            "action": getattr(mt5, "TRADE_ACTION_SLTP", 7),
            "symbol": self.symbol_name,
            "position": int(ticket),
            "sl": stops.get("sl", 0.0),
            "tp": stops.get("tp", 0.0),
        }
        res = self._send(req)
        if not res.get("ok"):
            # Loud, because the position is now open with no broker-side protection
            # and only the engine's polling stands behind it.
            self.log.error("position %s is open WITHOUT a broker-side stop: %s (%s)",
                           ticket, res.get("comment"), res.get("retcode"))
        return bool(res.get("ok"))

    def modify_stops(self, ticket: int, sl: float = 0.0, tp: float = 0.0) -> dict:
        """Move the broker-held bracket on an open position to absolute levels.

        This is what makes break-even and trailing real rather than advisory: the
        new level lives at the broker, so it is enforced tick by tick and survives
        this process, the network and the machine.

        ``0.0`` on either side means "leave it as it is", which is why the current
        levels are read off the position and sent back unchanged rather than as
        zero — MT5 reads a zero in an SLTP request as *remove this stop*, so a
        naive "only send what changed" would silently strip the other side.

        Levels are clamped to the broker's minimum distance from the **current**
        price, not from the entry: that is what the terminal validates an SLTP
        request against, and a trailing stop is by definition close to price.
        """
        guard = self._guard()
        if guard:
            return guard
        try:
            ticket = int(ticket)
        except (TypeError, ValueError):
            return {"ok": False, "retcode": -1, "comment": "bad ticket %r" % (ticket,)}
        if _f(sl) <= 0 and _f(tp) <= 0:
            return {"ok": False, "retcode": -1, "comment": "nothing to change"}

        try:
            open_now = self.positions()
        except BridgeReadError as exc:
            return {"ok": False, "retcode": -1, "comment": "could not read positions",
                    "detail": str(exc)}
        held = next((p for p in open_now if _i(p.get("ticket")) == ticket), None)
        if held is None:
            return {"ok": False, "retcode": -1, "comment": "position %d is not open" % ticket}

        si = self.symbol_info() or {}
        point = _f(si.get("point"), 0.01) or 0.01
        digits = _i(si.get("digits"), 2)
        floor = _i(si.get("stops_level"), 0) * point
        bid, ask = _f(si.get("bid")), _f(si.get("ask"))
        side = str(held.get("type") or "").lower()

        # Start from what the position already carries, so an unchanged side is
        # re-sent as itself instead of as a removal.
        new_sl = _f(sl) or _f(held.get("sl"))
        new_tp = _f(tp) or _f(held.get("tp"))
        if side == "buy":
            if new_sl > 0 and bid > 0:
                new_sl = min(new_sl, bid - floor)
            if new_tp > 0 and ask > 0:
                new_tp = max(new_tp, ask + floor)
        else:
            if new_sl > 0 and ask > 0:
                new_sl = max(new_sl, ask + floor)
            if new_tp > 0 and bid > 0:
                new_tp = min(new_tp, bid - floor)
        new_sl = round(max(new_sl, 0.0), digits)
        new_tp = round(max(new_tp, 0.0), digits)

        if (abs(new_sl - _f(held.get("sl"))) < point / 2.0
                and abs(new_tp - _f(held.get("tp"))) < point / 2.0):
            # Clamping can flatten a real request into the level already set. Saying
            # so beats sending the terminal a no-op it answers with retcode 10025.
            return {"ok": False, "retcode": 0, "comment": "already at that level",
                    "sl": new_sl, "tp": new_tp}

        res = self._send({
            "action": getattr(mt5, "TRADE_ACTION_SLTP", 7),
            "symbol": self.symbol_name,
            "position": ticket,
            "sl": new_sl,
            "tp": new_tp,
        })
        res["sl"], res["tp"] = new_sl, new_tp
        if not res.get("ok"):
            self.log.warning("could not move the bracket on %d to sl=%.2f tp=%.2f: %s (%s)",
                             ticket, new_sl, new_tp, res.get("comment"), res.get("retcode"))
        return res

    def close_position(self, ticket: int) -> dict:
        guard = self._guard()
        if guard:
            return guard
        with self._lock:
            try:
                found = mt5.positions_get(ticket=int(ticket))
            except Exception as exc:
                return {"ok": False, "retcode": 0, "comment": "lookup failed",
                        "tickets": [], "detail": str(exc)}
            if not found:
                return {"ok": True, "retcode": 0, "comment": "already closed",
                        "tickets": [], "detail": "position %s not found" % ticket}
            p = _asdict(found[0])
            symbol = str(p.get("symbol", self.symbol_name))
            is_buy = _i(p.get("type"), 0) == 0
            try:
                t = _asdict(mt5.symbol_info_tick(symbol))
            except Exception:
                t = {}
            price = _f(t.get("bid")) if is_buy else _f(t.get("ask"))
            req = {
                "action": getattr(mt5, "TRADE_ACTION_DEAL", 1),
                "symbol": symbol,
                "volume": _f(p.get("volume")),
                "type": (getattr(mt5, "ORDER_TYPE_SELL", 1) if is_buy
                         else getattr(mt5, "ORDER_TYPE_BUY", 0)),
                "position": _i(p.get("ticket"), 0),
                "price": price,
                "deviation": 20,
                "magic": _i(p.get("magic"), self._magic()),
                "comment": "INFINITY close",
                "type_time": getattr(mt5, "ORDER_TIME_GTC", 0),
            }
            res = self._send(req)
            return {
                "ok": bool(res.get("ok")),
                "retcode": res.get("retcode", 0),
                "comment": res.get("comment", ""),
                "tickets": [_i(p.get("ticket"), 0)] if res.get("ok") else [],
                "detail": "closed %.2f lots at %.5f" % (_f(p.get("volume")), _f(res.get("price")))
                if res.get("ok") else str(res.get("comment", "")),
                "profit": _f(p.get("profit")),
            }

    def close_all(self, magic: Optional[int] = None) -> dict:
        guard = self._guard()
        if guard:
            return guard
        # a read failure here must not become "already flat": that answer stops the
        # caller trying again, which on the kill-switch path leaves the account open
        try:
            positions = self.positions(magic=magic)
        except BridgeReadError as exc:
            return {"ok": False, "retcode": -1, "comment": "could not read positions",
                    "tickets": [], "detail": str(exc), "profit": 0.0}
        if not positions:
            return {"ok": True, "retcode": 0, "comment": "already flat", "tickets": [],
                    "detail": "no open positions", "profit": 0.0}
        closed, failed, profit = [], [], 0.0
        for p in sorted(positions, key=lambda x: x["time"], reverse=True):
            res = self.close_position(p["ticket"])
            if res.get("ok"):
                closed.append(p["ticket"])
                profit += _f(p.get("profit"))
            else:
                failed.append("%s: %s" % (p["ticket"], res.get("comment", "")))
        return {
            "ok": not failed,
            "retcode": 0,
            "comment": "closed %d of %d positions" % (len(closed), len(positions)),
            "tickets": closed,
            "detail": ("; ".join(failed[:4]) if failed else "all positions closed"),
            "profit": round(profit, 2),
        }


# --------------------------------------------------------------------------- #
# self-test — runs without a broker
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import json
    import sys

    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s %(message)s")

    print("=" * 72)
    print("INFINITY.100 AI - mt5_bridge.py self-test")
    print("=" * 72)
    print("MetaTrader5 importable :", MT5_AVAILABLE, MT5_IMPORT_ERROR)

    cases = [
        # (total, cap, vmin, vmax, step, expected)
        (5.0, 2.0, 0.01, 100.0, 0.01, [2.0, 2.0, 1.0]),
        (16.0, 2.0, 0.01, 100.0, 0.01, [2.0] * 8),
        (0.03, 2.0, 0.01, 100.0, 0.01, [0.03]),
        (0.01, 2.0, 0.01, 100.0, 0.01, [0.01]),
        (0.004, 2.0, 0.01, 100.0, 0.01, []),
        (4.0, 0.0, 0.01, 3.0, 0.01, [3.0, 1.0]),
        (1.0, 0.3, 0.01, 100.0, 0.01, [0.3, 0.3, 0.3, 0.1]),
        (2.5, 1.0, 0.1, 100.0, 0.1, [1.0, 1.0, 0.5]),
    ]
    failed = 0
    for total, cap, vmin, vmax, step, expect in cases:
        got = split_volume(total, cap, vmin, vmax, step)
        ok = got == expect and abs(sum(got) - (sum(expect))) < 1e-9
        exact = all(repr(v) == repr(round(v, decimal_places(step))) for v in got)
        status = "ok  " if (ok and exact) else "FAIL"
        if not (ok and exact):
            failed += 1
        print("[%s] split_volume(%.4f, cap=%.2f, step=%.2f) -> %s" % (status, total, cap, step, got))

    print("[%s] quantize(0.030000000000000002, 0.01) -> %r"
          % ("ok  " if quantize(0.030000000000000002, 0.01) == 0.03 else "FAIL",
             quantize(0.030000000000000002, 0.01)))
    print("[%s] decimal_places(0.01) == 2" % ("ok  " if decimal_places(0.01) == 2 else "FAIL"))

    from backend.config import load_config

    cfg = load_config()
    bridge = MT5Bridge(cfg, logging.getLogger("selftest"))
    print("-" * 72)
    print("live connect attempt (expected to fail cleanly without credentials):")
    result = bridge.connect()
    print(json.dumps({k: v for k, v in result.items() if k != "terminal"}, indent=2)[:1200])
    print("-" * 72)
    print("guarded order while disconnected:", bridge.market_order("buy", 0.01)["comment"])
    bridge.disconnect()
    print("=" * 72)
    print("split/quantize failures:", failed)

