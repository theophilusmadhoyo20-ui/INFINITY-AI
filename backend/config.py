"""INFINITY.100 AI - configuration loader / saver (contract section 3).

Single source of truth is ``D:\\INFINITY100-AI\\config.json``.

Public surface (other modules depend on exactly these names)::

    DEFAULTS                       # full default config, mirrors config.json
    CONFIG_PATH                    # pathlib.Path to config.json
    BASE_DIR, BACKEND_DIR, DATA_DIR, LOGS_DIR, FRONTEND_DIR, DB_PATH, LOG_PATH
    PASSWORD_ENV                   # "INFINITY_MT5_PASSWORD"
    deep_merge(base, patch)        -> dict   (new dict, inputs untouched)
    load_config(path=None, logger=None) -> dict
    save_config(config, path=None, logger=None) -> bool
    redact(config)                 -> dict   (deep copy, secrets replaced by "***")
    coerce_types(value, defaults)  -> dict   (string "300" -> 300.0 etc.)
    ensure_dirs(logger=None)       -> None

Standard library only. Python 3.10.
"""

from __future__ import annotations

import copy
import json
import logging
import os
import os.path
import shutil
import threading
import time
from pathlib import Path
from typing import Any, Optional, Union

__all__ = [
    "DEFAULTS",
    "CONFIG_PATH",
    "BASE_DIR",
    "BACKEND_DIR",
    "DATA_DIR",
    "LOGS_DIR",
    "FRONTEND_DIR",
    "DB_PATH",
    "LOG_PATH",
    "PASSWORD_ENV",
    "REDACTED",
    "deep_merge",
    "load_config",
    "save_config",
    "redact",
    "coerce_types",
    "sanitize_config",
    "ensure_dirs",
    "config_path",
]

# --------------------------------------------------------------------------- #
# logging (library code never prints)
# --------------------------------------------------------------------------- #

_LOG = logging.getLogger("infinity.config")
_LOG.addHandler(logging.NullHandler())


def _log(logger: Optional[logging.Logger]) -> logging.Logger:
    """Return the caller's logger, or a silent module logger."""
    return logger if isinstance(logger, logging.Logger) else _LOG


# --------------------------------------------------------------------------- #
# paths
# --------------------------------------------------------------------------- #

# Fallback root, spelled without forward slashes so Windows drive semantics hold.
_FALLBACK_BASE = os.path.join("D:", os.sep, "INFINITY100-AI")

_ENV_HOME = os.environ.get("INFINITY_HOME", "").strip()
if _ENV_HOME:
    BASE_DIR: Path = Path(_ENV_HOME).expanduser()
else:
    # backend/config.py -> backend -> project root
    BASE_DIR = Path(__file__).resolve().parents[1]
    if not BASE_DIR.name:  # pragma: no cover - defensive
        BASE_DIR = Path(_FALLBACK_BASE)

BACKEND_DIR: Path = BASE_DIR / "backend"
DATA_DIR: Path = BASE_DIR / "data"
LOGS_DIR: Path = BASE_DIR / "logs"
FRONTEND_DIR: Path = BASE_DIR / "frontend"
CONFIG_PATH: Path = BASE_DIR / "config.json"
DB_PATH: Path = DATA_DIR / "infinity.db"
LOG_PATH: Path = LOGS_DIR / "engine.log"

PASSWORD_ENV = "INFINITY_MT5_PASSWORD"
REDACTED = "***"

# keys whose values are never written to logs or the API
_SECRET_KEYS = ("password", "passwd", "secret", "token", "api_key", "apikey", "investor")

_SAVE_LOCK = threading.Lock()

# sentinel: "no default exists for this key" (None is a valid default value)
_MISSING = object()

# poll_ms guard rails (a 0 ms loop would spin a core; 60 s is already unusable)
_POLL_MS_MIN = 100
_POLL_MS_MAX = 60000


# --------------------------------------------------------------------------- #
# defaults - mirrors config.json byte for byte in meaning (contract section 3)
# --------------------------------------------------------------------------- #

DEFAULTS: dict = {
    "account": {
        "login": 0,
        "password": "",
        "server": "",
        "terminal_path": os.path.join("C:", os.sep, "Program Files", "MetaTrader 5", "terminal64.exe"),
        # mt5.initialize()/login() hold the GIL for their whole timeout, so this is
        # deliberately short — a stalled broker must not freeze the UI.
        "connect_timeout_ms": 10000,
        "allow_live_real": False,
    },
    "symbol": {
        "name": "auto",
        "candidates": [
            "XAUUSDm",
            "XAUUSD",
            "GOLD",
            "GOLD.",
            "XAUUSD.a",
            "XAUUSDc",
            "XAUUSD.raw",
            "GOLDmicro",
        ],
    },
    "engine": {
        "enabled": False,
        "active_strategy": "recovery_grid",
        "poll_ms": 1000,
        "magic": 100100,
    },
    "risk": {
        "min_margin_level_pct": 300.0,
        "max_drawdown_pct": 30.0,
        "daily_loss_limit_pct": 20.0,
        "equity_floor": 0.0,
        "max_basket_layers": 12,
        "max_total_lots": 0.0,
        "kill_switch": False,
    },
    "strategies": {
        "recovery_grid": {
            "base_lot": 0.01,
            "lot_tiers": [
                [0, 0.01],
                [100, 0.02],
                [200, 0.05],
                [500, 0.10],
                [1400, 0.50],
                [20000, 2.0],
                [150000, 3.0],
                [200000, 4.0],
            ],
            "grid_step_usd": 0.35,
            "max_layers": 9,
            "layer_multiplier": 1.0,
            "basket_tp_usd": 0.0,
            "basket_tp_pct_equity": 1.5,
            "basket_sl_pct_equity": 0.0,
            "volume_split_max_lot": 2.0,
            "max_spread_usd": 0.60,
            "cooldown_s": 20,
            "direction_mode": "momentum",
            "momentum_tf": "M5",
            "momentum_period": 20,
        },
        "ai_scalper": {
            "base_lot": 0.01,
            # The rung is base_lot; lot_tiers survives because roster() reads it
            # for the card's lot-range label, but AI SCALPER no longer sizes rungs
            # off the ladder. Growth adds rungs instead — which is what the
            # reference recording shows, and the only version that stays
            # consistent with a static volume_split_max_lot.
            "lot_tiers": [[0, 0.01], [500, 0.05], [5000, 0.20]],
            # tp_usd and sl_usd are per 0.01 lot of open volume, not flat cash,
            # and they bracket the *basket* on aggregate P/L — that is the primary
            # exit. On gold 0.01 lot is one ounce, so they read directly as price
            # distance.
            #
            # sl_usd was 0.80. Converted to price that is a stop 0.80 of gold from
            # entry against a measured M1 ATR(14) of 3.806 — 0.21x ATR, one fifth
            # of a typical one-minute candle, so ordinary noise closed it. The
            # record: 41 closed baskets, 5 wins, 36 losses, -$77.66, a 12.2% win
            # rate against the 38.7% that bracket needed, and avg_layers of 1.0
            # across all 41 because min_spacing_usd vetoed every burst rung.
            #
            # 1.80 / 3.60 is 0.47x and 0.95x that ATR. The honest cost of the
            # change is visible in the arithmetic: a 0.50 reward/risk needs 67% of
            # baskets to win where the old pair needed 39%. That is not a worse
            # bet, it is the shape of a basket that closes on a small aggregate
            # profit and sits through drawdown — the thing being reproduced. What
            # it removes is a stop that could not be reached without a loss.
            "tp_usd": 1.80,
            "sl_usd": 3.60,
            # Was 0.25, which no mainstream broker can satisfy on gold. Deriv
            # quotes a *fixed* 0.27 (129 of 129 sampled ticks, zero variance), so
            # the filter rejected every tick of every session and the strategy ran
            # 1002 polls without opening a single position — indistinguishable,
            # from the outside, from the engine being broken.
            #
            # 0.30 clears a fixed-0.27 or raw-spread broker with room to spare and
            # keeps the target at 6.0x the cap, comfortably above the
            # MIN_TARGET_OVER_SPREAD floor of 3.0. It is deliberately not pushed
            # to the arithmetic maximum: 0.35 lands exactly on a normal gold
            # spread (the integration fixture's own figure), so a cap there trades
            # through the very condition the filter exists to decline.
            "max_spread_usd": 0.30,
            "cooldown_s": 45,
            "ema_fast": 8,
            "ema_slow": 21,
            "timeframe": "M1",
            # Ceiling on rungs in one burst. The count actually opened is derived
            # from the broker's own margin rate against risk.min_margin_level_pct,
            # so this is the cap and not the target: three rungs fit on this
            # account, eight on a high-leverage one, from the same setting.
            "max_positions": 5,
            # Was 0.60, and it is why avg_layers never left 1.0: a burst is
            # *supposed* to share one entry price, and 0.60 of required separation
            # refused every rung of it. 0 lets the burst be a burst; a user who
            # wants rungs spread across a move can still set it.
            "min_spacing_usd": 0.0,
            "min_sep_atr": 0.30,
            # One order of unit x layers, cut into one order per rung by
            # market_order's splitter, so they all fill at the same price. This is
            # the reference recording's identical-entry-price group, built with the
            # splitter that already existed for it rather than a new engine
            # contract. Matches base_lot on purpose — see _plan().
            "volume_split_max_lot": 0.01,
            # The broker-side bracket is a catastrophe net, not the exit: it is
            # what remains if this app is closed, the machine sleeps or the link
            # drops. Scaled to ATR and then floored at broker_net_mult x the
            # basket's own cash cap, so "basket decides first, broker decides
            # last" holds at every volatility rather than only at the measured
            # one. Setting all three to 0 restores the pre-rebuild behaviour of
            # quoting the net straight off tp_usd/sl_usd.
            "sl_atr_mult": 1.60,
            "tp_atr_mult": 4.00,
            "broker_net_mult": 1.50,
            # Insurance against the one way a basket bracket fails badly: the
            # group runs to within a fraction of its cash target, reverses, and
            # gives it all back to a net placed 1.6x ATR away at entry.
            "breakeven_at_frac": 0.60,
            "breakeven_lock_usd": 0.10,
            # A burst commits several rungs at one price, so it is far more
            # exposed to a cross that is really chop than a single position was.
            "min_adx": 15.0,
            # RiskManager stops opening at min_margin_level_pct x 1.25 and
            # force-closes at the floor itself, so a burst sized against the bare
            # floor is one its own risk manager refuses to extend and then
            # liquidates. Size against the band it enforces.
            "entry_margin_band": 1.25,
            # Which generation of the three keys above this block is. Bumped only
            # when a release changes what those numbers MEAN, so
            # _migrate_scalper_revision can move a file forward once and then
            # leave it alone forever. See SCALPER_BRACKET_REV.
            "bracket_rev": 2,
        },
        "trend_ai": {
            "base_lot": 0.01,
            "lot_tiers": [[0, 0.01], [500, 0.05], [5000, 0.20]],
            "tp_pct_equity": 2.5,
            "sl_pct_equity": 1.2,
            "max_spread_usd": 0.45,
            "cooldown_s": 300,
            "ema_fast": 21,
            "ema_slow": 55,
            "adx_min": 20.0,
            "timeframe": "M15",
        },
        "mean_reversion": {
            "base_lot": 0.01,
            "lot_tiers": [[0, 0.01], [500, 0.05], [5000, 0.20]],
            "tp_pct_equity": 1.0,
            "sl_pct_equity": 1.5,
            "bb_period": 20,
            "bb_dev": 2.0,
            "max_spread_usd": 0.40,
            "cooldown_s": 120,
            "timeframe": "M5",
        },
    },
    "user": {
        "name": "Theophilus",
        "risk_profile": "Moderate",
        "sessions": ["London", "New York"],
        "favourite_symbol": "Gold",
    },
}


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #


def config_path(path: Union[str, "os.PathLike[str]", None] = None) -> Path:
    """Resolve *path* to a concrete config file Path (defaults to CONFIG_PATH)."""
    if path is None or path == "":
        return CONFIG_PATH
    return Path(path)


def ensure_dirs(logger: Optional[logging.Logger] = None) -> None:
    """Create data/ and logs/ if missing. Never raises."""
    for folder in (DATA_DIR, LOGS_DIR):
        try:
            folder.mkdir(parents=True, exist_ok=True)
        except Exception as exc:  # pragma: no cover - permissions / read-only disk
            _log(logger).warning("could not create %s: %s", folder, exc)


def _is_secret(key: Any) -> bool:
    return isinstance(key, str) and any(token in key.lower() for token in _SECRET_KEYS)


def deep_merge(base: Any, patch: Any) -> Any:
    """Recursively merge *patch* over *base* and return a NEW structure.

    Dicts merge key by key; every other type (including lists) is replaced
    wholesale, so ``lot_tiers`` patches behave predictably. Neither argument is
    mutated - callers get a deep copy they own.
    """
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch if patch is not None else base)

    merged: dict = copy.deepcopy(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _to_bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return bool(value)
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("1", "true", "yes", "y", "on"):
            return True
        if low in ("0", "false", "no", "n", "off", ""):
            return False
    return None


def _finite(value: float, fallback: float) -> float:
    """*value* unless it is NaN or an infinity, in which case *fallback*.

    ``json.loads`` accepts the bare literals ``Infinity`` and ``NaN``, and
    ``1e400`` overflows to ``inf`` silently, so a hand-edited config.json can
    put either into any numeric field. Neither survives contact with the
    engine: ``inf`` lots, an ``inf`` margin floor that makes every tick
    critical, a ``nan`` limit that compares False against everything.
    """
    if value != value or value in (float("inf"), float("-inf")):  # NaN != NaN
        return fallback
    return value


def coerce_types(value: Any, defaults: Any = _MISSING) -> Any:
    """Coerce scalars in *value* to the types used by *defaults*.

    The frontend posts JSON where numbers sometimes arrive as strings
    (``"300"``, ``"true"``). This walks both trees in parallel and repairs the
    obvious cases so the rest of the backend can trust ``float``/``int``/``bool``.
    Lists are left exactly as given (lot_tiers etc.).

    Keys absent from *defaults* (a user-added strategy, say) have no type to
    coerce against, so their whole subtree passes through untouched. ``None``
    is a legitimate default value, hence the ``_MISSING`` sentinel rather than
    ``defaults is None``.
    """
    if defaults is _MISSING:
        defaults = DEFAULTS

    if isinstance(defaults, dict) and isinstance(value, dict):
        out: dict = {}
        for key, item in value.items():
            if key in defaults:
                out[key] = coerce_types(item, defaults[key])
            else:
                out[key] = copy.deepcopy(item)
        return out

    # bool must be tested before int - bool is a subclass of int
    if isinstance(defaults, bool):
        coerced = _to_bool(value)
        return defaults if coerced is None else coerced

    if isinstance(defaults, float):
        try:
            return _finite(float(value), defaults)
        except (TypeError, ValueError, OverflowError):
            return defaults

    if isinstance(defaults, int):
        try:
            if isinstance(value, str):
                text = value.strip()
                return int(_finite(float(text), float(defaults))) if text else defaults
            if isinstance(value, float):
                return int(round(_finite(value, float(defaults))))
            if isinstance(value, bool):
                return int(value)
            return int(value)
        except (TypeError, ValueError, OverflowError):
            # OverflowError is the one that bites: int(round(inf)) raises it, and
            # inf arrives for free from the literal Infinity or from 1e400
            return defaults

    if isinstance(defaults, str) and value is None:
        return defaults

    return value


def _section(config: dict, key: str, logger: logging.Logger) -> dict:
    """The named section, guaranteed to be a dict.

    ``config.setdefault("engine", {})`` hands back whatever is already there, so
    ``"engine": []`` in config.json turns the next ``.get()`` into an
    AttributeError and the app will not start at all. A section of the wrong
    shape carries no usable settings, so it is replaced with the defaults and
    the user is told which one was thrown away.
    """
    value = config.get(key)
    if isinstance(value, dict):
        return value
    if value is not None:
        logger.warning("%s must be an object, found %s - using defaults for it",
                       key, type(value).__name__)
    config[key] = copy.deepcopy(DEFAULTS.get(key) or {})
    return config[key]


def _sanitize(config: dict, logger: Optional[logging.Logger] = None) -> dict:
    """Clamp values that would break the engine if taken literally."""
    log = _log(logger)

    engine = _section(config, "engine", log)
    try:
        poll = int(engine.get("poll_ms", DEFAULTS["engine"]["poll_ms"]))
    except (TypeError, ValueError, OverflowError):
        poll = DEFAULTS["engine"]["poll_ms"]
    clamped = max(_POLL_MS_MIN, min(_POLL_MS_MAX, poll))
    if clamped != poll:
        log.warning("engine.poll_ms %s clamped to %s ms", poll, clamped)
    engine["poll_ms"] = clamped

    try:
        engine["magic"] = int(engine.get("magic", DEFAULTS["engine"]["magic"]))
    except (TypeError, ValueError, OverflowError):
        engine["magic"] = DEFAULTS["engine"]["magic"]
    if not 0 < engine["magic"] <= 2147483647:
        # MT5 stores the magic in a 32-bit field; a bigger number is rejected at
        # order time, and every position we open would be unfindable afterwards
        engine["magic"] = DEFAULTS["engine"]["magic"]

    strategies = config.get("strategies")
    if not isinstance(strategies, dict) or not strategies:
        config["strategies"] = copy.deepcopy(DEFAULTS["strategies"])
        strategies = config["strategies"]
    for key in list(strategies.keys()):
        # every strategy is a bag of params; anything else would break build()
        if isinstance(strategies[key], dict):
            continue
        log.warning("strategies.%s must be an object, found %s", key,
                    type(strategies[key]).__name__)
        default = (DEFAULTS["strategies"].get(key) if isinstance(
            DEFAULTS.get("strategies"), dict) else None)
        if isinstance(default, dict):
            strategies[key] = copy.deepcopy(default)
        else:
            strategies.pop(key)
    if not strategies:
        config["strategies"] = copy.deepcopy(DEFAULTS["strategies"])
        strategies = config["strategies"]
    active = engine.get("active_strategy")
    if not isinstance(active, str) or active not in strategies:
        fallback = DEFAULTS["engine"]["active_strategy"]
        if fallback not in strategies:
            fallback = sorted(strategies.keys())[0]
        if active != fallback:
            log.warning("engine.active_strategy %r unknown - using %r", active, fallback)
        engine["active_strategy"] = fallback

    symbol = _section(config, "symbol", log)
    name = symbol.get("name")
    symbol["name"] = name.strip() if isinstance(name, str) and name.strip() else "auto"
    cands = symbol.get("candidates")
    if not isinstance(cands, list):
        cands = []
    clean: list = []
    for item in cands:
        if isinstance(item, str) and item.strip() and item.strip() not in clean:
            clean.append(item.strip())
    if not clean:
        clean = list(DEFAULTS["symbol"]["candidates"])
    symbol["candidates"] = clean

    account = _section(config, "account", log)
    try:
        account["login"] = int(account.get("login", 0) or 0)
    except (TypeError, ValueError, OverflowError):
        account["login"] = 0
    if not 0 <= account["login"] <= 9223372036854775807:
        account["login"] = 0  # MT5 carries the login in a signed 64-bit field
    for key in ("password", "server", "terminal_path"):
        if not isinstance(account.get(key), str):
            account[key] = DEFAULTS["account"][key]
    try:
        account["connect_timeout_ms"] = max(2000, min(60000, int(
            account.get("connect_timeout_ms") or DEFAULTS["account"]["connect_timeout_ms"])))
    except (TypeError, ValueError, OverflowError):
        account["connect_timeout_ms"] = DEFAULTS["account"]["connect_timeout_ms"]
    # Normalise to a real bool. The *authority* on this flag is
    # _explicit_live_consent(), called from sanitize_config() before
    # coerce_types() has had a chance to widen a bare number into True; by the
    # time this runs the value is already a bool, and `is True` keeps it one.
    account["allow_live_real"] = account.get("allow_live_real") is True

    risk = _section(config, "risk", log)
    risk["kill_switch"] = bool(_to_bool(risk.get("kill_switch")) or False)
    for key in ("min_margin_level_pct", "max_drawdown_pct", "daily_loss_limit_pct",
                "equity_floor", "max_total_lots"):
        fallback = float(DEFAULTS["risk"][key])
        try:
            risk[key] = max(0.0, _finite(float(risk.get(key, fallback)), fallback))
        except (TypeError, ValueError, OverflowError):
            risk[key] = fallback
    try:
        risk["max_basket_layers"] = max(1, min(200, int(risk.get("max_basket_layers", 12))))
    except (TypeError, ValueError, OverflowError):
        risk["max_basket_layers"] = DEFAULTS["risk"]["max_basket_layers"]

    return config


def _explicit_live_consent(config: Any, log: logging.Logger) -> bool:
    """Was permission to trade real money given *deliberately*?

    Read before any coercion, because coercion is too generous for this one
    flag. ``_to_bool(1)`` is True, so a client sending a count, a status code or
    an enum where it meant a boolean would switch real-money trading on. The
    contract says "explicitly true", so only the literal ``True`` and the words
    that unambiguously mean yes count; everything else is no, said out loud
    rather than decided quietly.
    """
    account = config.get("account") if isinstance(config, dict) else None
    raw = account.get("allow_live_real") if isinstance(account, dict) else None
    if raw is True:
        return True
    if isinstance(raw, str) and _to_bool(raw) is True:
        return True
    if raw not in (False, None, "") and not (isinstance(raw, str) and _to_bool(raw) is False):
        log.warning("account.allow_live_real %r is not an explicit true - "
                    "real-money trading stays blocked", raw)
    return False


def sanitize_config(config: dict, logger: Optional[logging.Logger] = None) -> dict:
    """Coerce types and clamp values on a whole config. Returns a new dict.

    This is the single gate every config must pass before the app is allowed to
    believe it, and it exists as a public name for one reason: ``load_config``
    used to be the only caller, so ``POST /api/config`` merged a request body
    straight into the live config and adopted it untouched. That let a patch of
    ``{"account": {"allow_live_real": "false"}}`` through as the *string*
    ``"false"`` — which is truthy in Python, so the guard reading
    ``not bool(allow_live_real)`` saw permission granted and a real-money
    account became tradeable for the rest of the session. The string that means
    "no" turning into "yes" is exactly the failure the contract calls out, and
    no amount of care at the call site fixes a second code path; there must not
    be a second code path.

    Anything that adopts a config — the loader, the API, a future importer —
    goes through here, so validation cannot drift away from the loader again.
    """
    log = _log(logger)
    # decided on the raw value, before coerce_types() can widen it
    consent = _explicit_live_consent(config, log)
    out = _sanitize(coerce_types(config, DEFAULTS), logger)
    out.setdefault("account", {})["allow_live_real"] = consent
    return out


def _env_password() -> str:
    """Password supplied through the environment (preferred over the file)."""
    return os.environ.get(PASSWORD_ENV, "") or ""


def _missing_paths(defaults: Any, data: Any, prefix: str = "") -> list:
    """Dotted paths present in *defaults* but absent from *data*."""
    out: list = []
    if not isinstance(defaults, dict):
        return out
    if not isinstance(data, dict):
        return [prefix.rstrip(".")] if prefix else ["<root>"]
    for key, value in defaults.items():
        dotted = f"{prefix}{key}"
        if key not in data:
            out.append(dotted)
        elif isinstance(value, dict):
            out.extend(_missing_paths(value, data.get(key), dotted + "."))
    return out


# --------------------------------------------------------------------------- #
# load / save
# --------------------------------------------------------------------------- #


#: A scalp target must be at least this many times the widest spread the strategy
#: will trade through. Below it the cost of entry is most of the prize and no
#: plausible win rate pays for the trade: at tp 0.40 against a 0.30 spread cap the
#: spread is 75% of the target, and 1206 measured baskets returned 1.7% winners.
#: The shipping default sits at 4.0x. Note this bounds the cap from above at
#: ``tp / 3.0`` — a filter cannot be *loosened* past the point where the spread
#: eats a third of the target, so widening it for a wide-spread broker means
#: raising the target too, not just the cap.
MIN_TARGET_OVER_SPREAD = 3.0

#: Current generation of the ``ai_scalper`` bracket. Bumped when a release changes
#: what those numbers mean or what shape of trade they describe — not when one is
#: merely repaired. Revision 2 is the rebuild around measured volatility.
SCALPER_BRACKET_REV = 2

#: What revision 1 shipped for the keys revision 2 moves. The test is equality
#: against these, not "is it different from the new default", and that distinction
#: is the whole point: a saved value equal to one of these was never chosen by
#: anyone, it is the previous release's default sitting in the file, so it is safe
#: to move on. Any other value is a hand-edit and is kept — the same principle
#: _migrate_scalper_bracket already follows for a deliberately wide target.
_SCALPER_REV1 = {"tp_usd": 1.20, "sl_usd": 0.80, "min_spacing_usd": 0.60}


def _migrate_scalper_revision(raw: dict, log: logging.Logger) -> bool:
    """Move an ``ai_scalper`` block from bracket revision 1 to revision 2.

    The arithmetic migration below cannot do this job, and it is worth being
    precise about why: its gate is ``tp >= cap * MIN_TARGET_OVER_SPREAD and
    sl > cap``, and revision 1's bracket (tp 1.20 / sl 0.80 against a 0.30 cap)
    *passes* that gate. It is spread-viable. What was wrong with it is not
    visible in the spread at all — an 0.80 stop is 0.21x the measured M1 ATR(14)
    on gold, so ordinary noise reached it before the trade could work. That is a
    statement about volatility, and there is no volatility in config.json to
    compare against, so no arithmetic on this file can detect it.

    So this migration is explicit rather than inferred: revision 1 files are
    identified by the absence of ``bracket_rev``, moved forward on the three keys
    revision 2 changed, and stamped. Being explicit is what makes it terminate —
    it runs once per file, and a stamped file is never touched again however the
    user later tunes it.

    Returns True when something changed, so the caller persists the file.
    """
    strategies = raw.get("strategies")
    block = strategies.get("ai_scalper") if isinstance(strategies, dict) else None
    if not isinstance(block, dict):
        return False

    try:
        rev = int(float(block.get("bracket_rev", 0) or 0))
    except (TypeError, ValueError):
        rev = 0
    if rev >= SCALPER_BRACKET_REV:
        return False

    fresh = DEFAULTS["strategies"]["ai_scalper"]
    moved = []
    for key, was in _SCALPER_REV1.items():
        try:
            have = float(block[key])
        except (KeyError, TypeError, ValueError):
            continue  # absent or unreadable: the missing-key heal owns it
        if abs(have - was) < 1e-9:
            block[key] = fresh[key]
            moved.append("%s %.2f -> %.2f" % (key, have, float(fresh[key])))

    block["bracket_rev"] = SCALPER_BRACKET_REV
    if moved:
        log.warning(
            "ai_scalper: moved the saved bracket from revision %d to %d (%s). The old "
            "stop sat 0.21x the measured M1 ATR from entry, so noise closed it: 41 "
            "closed baskets, 5 wins, -$77.66. Values you had changed yourself were "
            "kept.", rev, SCALPER_BRACKET_REV, "; ".join(moved))
    else:
        log.info("ai_scalper: stamped bracket revision %d; every value was a "
                 "hand-edit and was kept.", SCALPER_BRACKET_REV)
    return True


def _migrate_scalper_bracket(raw: dict, log: logging.Logger) -> bool:
    """Rewrite an ``ai_scalper`` bracket that arithmetic says cannot win.

    ``tp_usd``/``sl_usd`` changed meaning. They used to be a flat amount of
    account currency; they are now quoted per 0.01 lot of open volume. Read the
    old way an $0.80 stop sits 0.16 of price from entry at the 0.05-lot tier,
    which is *inside the spread*, so the position opened already past its own stop
    and lost before price moved at all.

    The tempting test is provenance — ``max_positions`` did not exist before the
    change, so its absence would date the block. That marker is not trustworthy:
    ``load_config`` heals missing keys by writing them back, so merely starting the
    app once stamps ``max_positions`` onto a file whose money keys are still the old
    ones. That is the worst state of the three, five concurrent positions on a
    bracket that cannot win, and provenance would call it migrated.

    So the test is arithmetic instead, on the merged values: a target smaller than
    :data:`MIN_TARGET_OVER_SPREAD` times the spread cap is spending most of the
    prize on the entry. It catches the old file, the half-healed file and a
    hand-edit, it needs no history, and it is idempotent — the values it writes
    pass it.

    Only the three money keys move, and only the ones that actually fail. EMA
    periods, cooldown, timeframe and the lot ladder mean the same thing before and
    after, so whatever was chosen there is kept. Returns True when something
    changed, so the caller persists the file.
    """
    strategies = raw.get("strategies")
    block = strategies.get("ai_scalper") if isinstance(strategies, dict) else None
    if not isinstance(block, dict):
        return False

    fresh = DEFAULTS["strategies"]["ai_scalper"]

    def num(key):
        try:
            return float(block.get(key, fresh[key]))
        except (TypeError, ValueError):
            return float(fresh[key])

    tp, sl, cap = num("tp_usd"), num("sl_usd"), num("max_spread_usd")
    if cap <= 0:
        return False  # no spread filter configured; nothing to measure against
    if tp >= cap * MIN_TARGET_OVER_SPREAD and sl > cap:
        return False

    # Something failed. Which one decides what to touch — this function used to
    # reset all three money keys whenever *any* of them failed, so a deliberately
    # wide target (tp 5.00 against a 2.00 spread cap) was silently pulled back to
    # the 1.20 default on every single load. A migration that overrides a choice
    # the user made on purpose is a bug, not a repair.
    #
    # The floor is the shipping spread cap: it is the tightest filter this app
    # considers tradeable, so a target that cannot clear even *that* is not a
    # target quoted per 0.01 lot at all — it is the old flat amount, and the whole
    # bracket has to be replaced.
    changed = []
    if tp < fresh["max_spread_usd"] * MIN_TARGET_OVER_SPREAD:
        was = "tp %.2f / sl %.2f / max_spread %.2f" % (tp, sl, cap)
        for key in ("tp_usd", "sl_usd", "max_spread_usd"):
            block[key] = fresh[key]
        log.warning(
            "ai_scalper: the saved bracket (%s) cannot pay for its own entry - the "
            "spread is %.0f%% of the target. tp/sl are quoted per 0.01 lot now, so it "
            "was rewritten to tp %.2f / sl %.2f / max_spread %.2f.",
            was, 100.0 * cap / tp if tp > 0 else 100.0,
            fresh["tp_usd"], fresh["sl_usd"], fresh["max_spread_usd"])
        return True

    # The target is viable, so keep it and tighten what is around it. Narrowing the
    # spread filter only means declining to trade an expensive moment; lowering the
    # target changes the strategy the user asked for.
    if cap > tp / MIN_TARGET_OVER_SPREAD:
        # truncate rather than round, so the value written always satisfies the
        # test that sent us here — rounding up would fail it again next load
        new_cap = int(100.0 * tp / MIN_TARGET_OVER_SPREAD) / 100.0
        changed.append("max_spread %.2f -> %.2f" % (cap, new_cap))
        block["max_spread_usd"] = cap = new_cap
    if sl <= cap:
        # a stop inside the spread means the position opens already past it
        new_sl = max(float(fresh["sl_usd"]), round(cap * 2.0, 2))
        changed.append("sl %.2f -> %.2f" % (sl, new_sl))
        block["sl_usd"] = new_sl
    if not changed:
        return False
    log.warning("ai_scalper: kept the %.2f target and repaired what failed around "
                "it (%s).", tp, "; ".join(changed))
    return True


def load_config(
    path: Union[str, "os.PathLike[str]", None] = None,
    logger: Optional[logging.Logger] = None,
) -> dict:
    """Load config.json, self-healing anything missing or corrupt.

    Order of operations:

    1. read the file (missing or unparsable -> defaults, corrupt file is kept
       aside as ``config.json.corrupt-<epoch>``),
    2. ``deep_merge`` over :data:`DEFAULTS` so new keys always exist,
    3. coerce scalar types and clamp unusable values,
    4. write the healed file back **only** when keys were genuinely missing,
    5. finally apply the ``INFINITY_MT5_PASSWORD`` env override in memory.

    The env password is never persisted (see :func:`save_config`).
    Never raises - a broken disk still yields a usable config.
    """
    log = _log(logger)
    target = config_path(path)
    ensure_dirs(logger)

    raw: dict = {}
    healed_reason = ""

    try:
        if target.is_file():
            text = target.read_text(encoding="utf-8-sig")
            if text.strip():
                parsed = json.loads(text)
                if isinstance(parsed, dict):
                    raw = parsed
                else:
                    healed_reason = "config.json is not a JSON object"
            else:
                healed_reason = "config.json is empty"
        else:
            healed_reason = "config.json not found"
    except json.JSONDecodeError as exc:
        healed_reason = f"config.json is not valid JSON ({exc})"
    except OSError as exc:
        healed_reason = f"config.json unreadable ({exc})"
    except Exception as exc:  # pragma: no cover - defensive
        healed_reason = f"config.json failed to load ({exc})"

    if healed_reason:
        log.warning("%s - falling back to defaults", healed_reason)
        if target.exists() and "not found" not in healed_reason:
            quarantine = target.with_name(f"{target.name}.corrupt-{int(time.time())}")
            try:
                shutil.copy2(str(target), str(quarantine))
                log.warning("kept the unusable file as %s", quarantine)
            except Exception as exc:  # pragma: no cover
                log.warning("could not preserve the unusable config: %s", exc)

    missing = _missing_paths(DEFAULTS, raw)
    # revision first, arithmetic second: the revision migration installs the new
    # shape, then the arithmetic gate gets to validate what was just written. The
    # values revision 2 writes pass that gate, so the pair is still idempotent.
    migrated = _migrate_scalper_revision(raw, log)
    migrated = _migrate_scalper_bracket(raw, log) or migrated
    merged = deep_merge(DEFAULTS, raw)
    merged = sanitize_config(merged, logger)

    if healed_reason or missing or migrated:
        if missing and not healed_reason:
            log.info("config.json missing %d key(s) %s - healing file",
                     len(missing), missing[:8])
        # persist the file-truth version (no env password inside)
        save_config(merged, target, logger)

    env_pw = _env_password()
    if env_pw:
        merged.setdefault("account", {})["password"] = env_pw
        log.info("using MT5 password from %s (file value ignored)", PASSWORD_ENV)

    return merged


def save_config(
    config: dict,
    path: Union[str, "os.PathLike[str]", None] = None,
    logger: Optional[logging.Logger] = None,
) -> bool:
    """Write *config* to disk atomically. Returns True on success.

    Atomic means: serialise first, write a sibling temp file, ``flush`` +
    ``fsync``, then :func:`os.replace` over the target - a crash mid-write can
    never leave a half-written config.json. The previous file is copied to
    ``config.json.bak`` first.

    If ``account.password`` currently equals the ``INFINITY_MT5_PASSWORD`` env
    value, a blank password is written instead: the environment stays the
    single place the live secret lives.
    """
    log = _log(logger)
    target = config_path(path)

    if not isinstance(config, dict):
        log.error("save_config refused: config is %s, not dict", type(config).__name__)
        return False

    payload = copy.deepcopy(config)
    env_pw = _env_password()
    account = payload.get("account")
    if isinstance(account, dict):
        if env_pw and account.get("password") == env_pw:
            account["password"] = ""
        if account.get("password") == REDACTED:
            # never let a redacted view round-trip back onto disk
            account["password"] = ""

    try:
        text = json.dumps(payload, indent=2, ensure_ascii=False, sort_keys=False)
    except (TypeError, ValueError) as exc:
        log.error("save_config refused: config is not JSON serialisable (%s)", exc)
        return False

    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    with _SAVE_LOCK:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(text)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if target.is_file():
                try:
                    shutil.copy2(str(target), str(target.with_name(target.name + ".bak")))
                except Exception as exc:  # pragma: no cover
                    log.debug("no config backup written: %s", exc)
            os.replace(str(tmp), str(target))
            log.debug("config saved to %s (%d bytes)", target, len(text))
            return True
        except Exception as exc:
            log.error("save_config failed for %s: %s", target, exc)
            return False
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:  # pragma: no cover
                pass


def redact(config: Any) -> Any:
    """Deep copy of *config* with every secret value replaced by ``"***"``.

    ``account.password`` becomes ``"***"`` when a password is actually set and
    stays ``""`` when none is - so the UI can tell "hidden" from "not
    configured" without ever receiving the secret. Any nested key that looks
    like a credential (password / secret / token / api_key) is covered too.
    """
    if isinstance(config, dict):
        out: dict = {}
        for key, value in config.items():
            if _is_secret(key):
                if isinstance(value, str):
                    out[key] = REDACTED if value.strip() else ""
                elif value in (None, 0, False):
                    out[key] = value
                else:
                    out[key] = REDACTED
            else:
                out[key] = redact(value)
        return out
    if isinstance(config, (list, tuple)):
        return [redact(item) for item in config]
    return config


# --------------------------------------------------------------------------- #
# self-test - runs without a broker and without touching the real config.json
# --------------------------------------------------------------------------- #

if __name__ == "__main__":  # pragma: no cover
    import sys

    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stdout,
        format="%(levelname)-7s %(name)s: %(message)s",
    )
    log = logging.getLogger("selftest.config")

    print("=" * 72)
    print("INFINITY.100 AI - config.py self-test")
    print("=" * 72)
    print(f"BASE_DIR    : {BASE_DIR}")
    print(f"CONFIG_PATH : {CONFIG_PATH}  (exists={CONFIG_PATH.is_file()})")
    print(f"DB_PATH     : {DB_PATH}")
    print(f"LOG_PATH    : {LOG_PATH}")

    # ---- deep_merge -------------------------------------------------------
    base = {"a": 1, "b": {"c": 2, "d": 3}, "list": [1, 2]}
    patch = {"b": {"d": 99, "e": 5}, "list": [9], "new": True}
    merged = deep_merge(base, patch)
    assert merged == {"a": 1, "b": {"c": 2, "d": 99, "e": 5}, "list": [9], "new": True}, merged
    assert base == {"a": 1, "b": {"c": 2, "d": 3}, "list": [1, 2]}, "deep_merge mutated base"
    assert deep_merge(base, {})["b"] is not base["b"], "deep_merge must deep copy"
    print("[ok]  deep_merge: nested merge, list replace, inputs untouched")

    # ---- coerce_types -----------------------------------------------------
    coerced = coerce_types(
        {
            "risk": {"min_margin_level_pct": "250", "kill_switch": "true",
                     "max_basket_layers": "7"},
            "engine": {"poll_ms": "1500", "magic": 100100},
            "account": {"allow_live_real": "false", "login": "12345"},
        },
        DEFAULTS,
    )
    assert coerced["risk"]["min_margin_level_pct"] == 250.0
    assert coerced["risk"]["kill_switch"] is True
    assert coerced["risk"]["max_basket_layers"] == 7
    assert coerced["engine"]["poll_ms"] == 1500
    assert coerced["account"]["allow_live_real"] is False
    assert coerced["account"]["login"] == 12345
    print("[ok]  coerce_types: '250'->250.0, 'true'->True, '12345'->12345")

    # unknown keys must pass through untouched, never be coerced against the
    # wrong subtree of DEFAULTS
    custom = coerce_types(
        {"strategies": {"my_strat": {"risk": {"max_drawdown_pct": "keep-me"},
                                     "engine": {"poll_ms": "9"}, "lot": 0.5}}},
        DEFAULTS,
    )
    mine = custom["strategies"]["my_strat"]
    assert mine["risk"]["max_drawdown_pct"] == "keep-me", mine
    assert mine["engine"]["poll_ms"] == "9", mine
    assert mine["lot"] == 0.5
    print("[ok]  coerce_types: unknown subtrees pass through unmodified")

    # ---- redact -----------------------------------------------------------
    live = deep_merge(DEFAULTS, {"account": {"password": "s3cret-live-pw", "login": 42}})
    view = redact(live)
    assert view["account"]["password"] == REDACTED, view["account"]
    assert live["account"]["password"] == "s3cret-live-pw", "redact mutated the source"
    assert redact(DEFAULTS)["account"]["password"] == "", "unset password must stay empty"
    dumped = json.dumps(view)
    assert "s3cret-live-pw" not in dumped
    print('[ok]  redact: set password -> "***", unset stays "", source untouched')

    # ---- load / save round trip on a scratch file (D: only, never C:) -----
    ensure_dirs(log)
    scratch = DATA_DIR / "_selftest_config.json"
    for stale in (scratch, scratch.with_name(scratch.name + ".bak")):
        try:
            stale.unlink()
        except FileNotFoundError:
            pass

    healed = load_config(scratch, log)
    assert scratch.is_file(), "missing file should have been created"
    assert healed["engine"]["magic"] == 100100
    assert healed["user"]["name"] == "Theophilus"
    assert healed["strategies"]["recovery_grid"]["volume_split_max_lot"] == 2.0
    print(f"[ok]  load_config: created {scratch.name} from DEFAULTS")

    healed["risk"]["max_drawdown_pct"] = 22.5
    assert save_config(healed, scratch, log) is True
    again = load_config(scratch, log)
    assert again["risk"]["max_drawdown_pct"] == 22.5
    print("[ok]  save_config: atomic write survived a round trip")

    # partial file -> defaults fill the gaps, file is healed on disk
    scratch.write_text('{"engine": {"poll_ms": 250}}', encoding="utf-8")
    partial = load_config(scratch, log)
    assert partial["engine"]["poll_ms"] == 250
    assert partial["engine"]["magic"] == 100100
    assert "recovery_grid" in partial["strategies"]
    on_disk = json.loads(scratch.read_text(encoding="utf-8"))
    assert on_disk["engine"]["magic"] == 100100, "partial file was not healed on disk"
    print("[ok]  load_config: partial file healed, missing keys restored")

    # corrupt file -> quarantined + rebuilt
    scratch.write_text("{ this is not json ", encoding="utf-8")
    recovered = load_config(scratch, log)
    assert recovered["engine"]["active_strategy"] == "recovery_grid"
    quarantined = sorted(DATA_DIR.glob("_selftest_config.json.corrupt-*"))
    assert quarantined, "corrupt file was not preserved"
    print(f"[ok]  load_config: corrupt file quarantined as {quarantined[-1].name}")

    # clamping / unknown strategy
    scratch.write_text(
        json.dumps({"engine": {"poll_ms": 5, "active_strategy": "nope", "magic": 0},
                    "symbol": {"name": "  ", "candidates": []}}),
        encoding="utf-8",
    )
    clamped = load_config(scratch, log)
    assert clamped["engine"]["poll_ms"] == _POLL_MS_MIN
    assert clamped["engine"]["active_strategy"] == "recovery_grid"
    assert clamped["engine"]["magic"] == 100100
    assert clamped["symbol"]["name"] == "auto"
    assert clamped["symbol"]["candidates"][0] == "XAUUSDm"
    print("[ok]  _sanitize: poll_ms/magic/strategy/symbol guard rails applied")

    # ---- hostile values ---------------------------------------------------
    # json.loads accepts the bare literals Infinity and NaN, and 1e400 silently
    # overflows to inf, so any number in this file can arrive non-finite. A
    # section can also be the wrong *shape* entirely. None of it may stop the
    # app from starting, because the user's only way back in is the app itself.
    scratch.write_text(
        '{"engine": {"poll_ms": Infinity, "magic": 1e400},'
        ' "risk": {"min_margin_level_pct": Infinity, "max_drawdown_pct": NaN,'
        '          "equity_floor": -5, "max_basket_layers": 1e400},'
        ' "account": {"login": 1e400, "connect_timeout_ms": NaN}}',
        encoding="utf-8",
    )
    hostile = load_config(scratch, log)
    # a non-finite number carries no intent, so it falls back to the default;
    # a merely absurd finite one is clamped to the edge of the usable range
    assert hostile["engine"]["poll_ms"] == DEFAULTS["engine"]["poll_ms"], \
        hostile["engine"]["poll_ms"]
    assert hostile["engine"]["magic"] == 100100, hostile["engine"]["magic"]
    assert hostile["risk"]["min_margin_level_pct"] == DEFAULTS["risk"]["min_margin_level_pct"]
    assert hostile["risk"]["max_drawdown_pct"] == DEFAULTS["risk"]["max_drawdown_pct"]
    assert hostile["risk"]["equity_floor"] == 0.0, hostile["risk"]["equity_floor"]
    assert hostile["risk"]["max_basket_layers"] == DEFAULTS["risk"]["max_basket_layers"]
    assert hostile["account"]["login"] == 0, hostile["account"]["login"]
    assert hostile["account"]["connect_timeout_ms"] == \
        DEFAULTS["account"]["connect_timeout_ms"]
    for key, value in hostile["risk"].items():
        assert not isinstance(value, float) or value == value, "%s is NaN" % key
    print("[ok]  load_config: Infinity / NaN / 1e400 fall back instead of raising")

    scratch.write_text(json.dumps({"engine": {"poll_ms": 999999999, "magic": 99999999999}}),
                       encoding="utf-8")
    absurd = load_config(scratch, log)
    assert absurd["engine"]["poll_ms"] == _POLL_MS_MAX, absurd["engine"]["poll_ms"]
    assert absurd["engine"]["magic"] == 100100, absurd["engine"]["magic"]
    print("[ok]  _sanitize: an absurd but finite poll_ms clamps, a 33-bit magic resets")

    scratch.write_text(
        json.dumps({"engine": [], "risk": "off", "account": 7, "symbol": None,
                    "strategies": {"recovery_grid": "broken", "junk": 3}}),
        encoding="utf-8",
    )
    shaped = load_config(scratch, log)
    for section in ("engine", "risk", "account", "symbol", "strategies"):
        assert isinstance(shaped[section], dict), "%s: %r" % (section, shaped[section])
    assert isinstance(shaped["strategies"]["recovery_grid"], dict)
    assert "junk" not in shaped["strategies"], shaped["strategies"].keys()
    assert shaped["engine"]["active_strategy"] in shaped["strategies"]
    print("[ok]  load_config: a section of the wrong type is replaced, not crashed on")

    # ---- env override -----------------------------------------------------
    os.environ[PASSWORD_ENV] = "env-pw-123"
    try:
        with_env = load_config(scratch, log)
        assert with_env["account"]["password"] == "env-pw-123"
        assert save_config(with_env, scratch, log) is True
        text_on_disk = scratch.read_text(encoding="utf-8")
        assert "env-pw-123" not in text_on_disk, "env password leaked to disk"
        assert json.loads(text_on_disk)["account"]["password"] == ""
        print(f"[ok]  {PASSWORD_ENV} override applied in memory, never written to disk")
    finally:
        os.environ.pop(PASSWORD_ENV, None)

    # a redacted view must not overwrite a stored password either
    scratch.write_text(json.dumps({"account": {"password": "on-disk-pw"}}), encoding="utf-8")
    view_only = redact(load_config(scratch, log))
    assert save_config(view_only, scratch, log) is True
    assert json.loads(scratch.read_text(encoding="utf-8"))["account"]["password"] == ""
    print('[ok]  save_config: refuses to persist the literal "***" placeholder')

    # ---- clean up scratch files ------------------------------------------
    for leftover in list(DATA_DIR.glob("_selftest_config.json*")):
        try:
            leftover.unlink()
        except OSError:
            pass

    # ---- the real config.json (read-only sanity) --------------------------
    real = load_config(logger=log)
    print("-" * 72)
    print("real config.json summary")
    print(f"  strategies      : {sorted(real['strategies'])}")
    print(f"  active strategy : {real['engine']['active_strategy']}")
    print(f"  magic / poll_ms : {real['engine']['magic']} / {real['engine']['poll_ms']}")
    print(f"  symbol          : {real['symbol']['name']} {real['symbol']['candidates']}")
    print(f"  allow_live_real : {real['account']['allow_live_real']}")
    print(f"  password (view) : {redact(real)['account']['password']!r}")
    print("=" * 72)
    print("config.py self-test PASSED")
