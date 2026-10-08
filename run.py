"""INFINITY.100 AI — single entry point.

    python run.py                 start everything on http://127.0.0.1:8100
    python run.py --port 9000     use another port
    python run.py --no-connect    start the UI without touching MetaTrader 5
    python run.py --open          open the browser when it is ready

The server always comes up, even when MetaTrader 5 is unreachable — the UI is
where you are told what is wrong, so it must never depend on the broker.
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import sys
import time
import webbrowser

BASE = os.path.dirname(os.path.abspath(__file__))
if BASE not in sys.path:
    sys.path.insert(0, BASE)

from backend.config import DB_PATH, LOG_PATH, ensure_dirs, load_config  # noqa: E402
from backend.engine import TradingEngine  # noqa: E402
from backend.mt5_bridge import MT5Bridge  # noqa: E402
from backend.persona import AIPersona  # noqa: E402
from backend.risk import RiskManager  # noqa: E402
from backend.server import Api, Server  # noqa: E402
from backend.store import Store  # noqa: E402

BANNER = r"""
  ___ _  _ ___ ___ _  _ ___ _______   _   ___   ___
 |_ _| \| | __|_ _| \| |_ _|_   _\ \ / / / _ \ / _ \    AI TRADING
  | || .` | _| | || .` || |  | |  \ V / | | | | | | |   OPERATING
 |___|_|\_|_| |___|_|\_|___| |_|   |_|  |_| |_|\___/    SYSTEM
"""


def setup_logging(verbose: bool = False) -> logging.Logger:
    ensure_dirs()
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-18s %(message)s",
                            "%Y-%m-%d %H:%M:%S")
    try:
        rotating = logging.handlers.RotatingFileHandler(
            LOG_PATH, maxBytes=4 * 1024 * 1024, backupCount=3, encoding="utf-8")
        rotating.setFormatter(fmt)
        rotating.setLevel(logging.DEBUG)
        root.addHandler(rotating)
    except OSError as exc:
        print("warning: cannot write %s (%s)" % (LOG_PATH, exc))

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(levelname)-7s %(message)s"))
    console.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(console)
    logging.getLogger("infinity.server").setLevel(logging.INFO)
    return logging.getLogger("infinity")


def _default_port() -> int:
    """The port to bind when ``--port`` is not given.

    ``PORT`` in the environment wins over the built-in 8100 so a supervisor that
    assigns the port (the preview harness does) does not need the flag at all.
    An unreadable or out-of-range value falls back rather than raising: the
    server coming up on the usual port is always more useful than a traceback.
    """
    try:
        port = int(os.environ.get("PORT", "") or 0)
    except (TypeError, ValueError):
        return 8100
    return port if 0 < port <= 65535 else 8100


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="INFINITY.100 AI", add_help=True)
    parser.add_argument("--port", type=int, default=_default_port(),
                        help="HTTP port (default $PORT, else 8100)")
    parser.add_argument("--no-connect", action="store_true",
                        help="do not attempt a MetaTrader 5 connection at startup")
    parser.add_argument("--open", action="store_true", help="open the UI in your browser")
    parser.add_argument("--verbose", action="store_true", help="debug logging")
    args = parser.parse_args(argv)

    log = setup_logging(args.verbose)
    print(BANNER)

    config = load_config(logger=log)
    store = Store(DB_PATH, logging.getLogger("infinity.store"))
    bridge = MT5Bridge(config, logging.getLogger("infinity.mt5"))
    risk = RiskManager(config, logging.getLogger("infinity.risk"))
    persona = AIPersona(config, store, logging.getLogger("infinity.persona"))
    engine = TradingEngine(config, bridge, risk, store, persona,
                           logging.getLogger("infinity.engine"))
    api = Api(config, bridge, engine, risk, store, persona,
              logging.getLogger("infinity.server"))
    server = Server(api, port=args.port)

    # The HTTP server binds FIRST, on purpose. mt5.initialize() and mt5.login()
    # are native calls that hold the GIL for their whole timeout, so a stalled
    # broker connection freezes every Python thread in this process. Bind the
    # socket before anything can touch MetaTrader 5 and the UI always exists.
    url = server.start()

    if args.no_connect:
        log.info("skipping MetaTrader 5 connection (--no-connect)")
        engine.offline = True
    else:
        result = engine.connect_now()
        if result.get("ok"):
            account = result.get("account") or {}
            log.info("MT5 connected: %s account %s on %s, %s %.2f, symbol %s",
                     account.get("trade_mode"), account.get("login"), account.get("server"),
                     account.get("currency"), float(account.get("equity") or 0.0),
                     bridge.symbol_name)
            # a real account without explicit consent connects read-only, so say
            # so loudly here rather than letting the banner imply it can trade
            if result.get("blocked"):
                log.warning("READ ONLY: %s", result.get("block_reason") or
                            "trading is blocked on this account")
        else:
            log.warning("MT5 not connected (stage=%s): %s",
                        result.get("stage"), result.get("message"))

    engine.begin()

    print("")
    print("  UI          %s" % url)
    mt5_line = "not connected"
    if bridge.is_connected():
        mt5_line = "connected · %s" % bridge.symbol_name
        if bridge.blocked()[0]:
            mt5_line += "  (READ ONLY — real-money trading not permitted)"
    print("  MT5         %s" % mt5_line)
    print("  strategy    %s (%s)" % (engine._strategy.title, engine._strategy.key))
    print("  database    %s" % DB_PATH)
    print("  log         %s" % LOG_PATH)
    print("  automation  idle — press DEPLOY in the UI to trade")
    print("")
    print("  Ctrl+C to stop.")
    print("")

    if args.open:
        try:
            webbrowser.open(url)
        except Exception as exc:  # noqa: BLE001
            log.debug("browser open failed: %s", exc)

    try:
        while True:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("")
        log.info("shutting down")
    finally:
        # Order matters and is load-bearing: automation off, engine thread joined,
        # then the HTTP server (whose stop() joins any handler still mid-request),
        # then the broker, and the database last — nothing may still be able to
        # reach the store when it closes.
        engine.stop()
        engine.shutdown()
        server.stop()
        bridge.disconnect()
        store.close()
        log.info("stopped cleanly")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
