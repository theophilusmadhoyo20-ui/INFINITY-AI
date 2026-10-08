# INFINITY.100 AI — Module Contract (authoritative)

Every module MUST conform to this file. No deviations. Python 3.10, **stdlib only**
plus `MetaTrader5` (already installed). **No pip installs** — C: drive has ~5 MB free.
Frontend is vanilla HTML/CSS/JS, **no build step, no npm**.

---

## 1. Identity

Product: **INFINITY.100 AI — AI Trading Operating System**
The aggressive recovery strategy is ONE strategy module, not the product.
User: Theophilus. The assistant greets by name and knows their history.

---

## 2. File layout

```
D:\INFINITY100-AI\
  backend\
    config.py         # load/save config.json, defaults, env overrides
    mt5_bridge.py     # ALL MetaTrader5 API contact. Nothing else touches mt5.
    risk.py           # RiskManager — guards, stop-out math, kill switch
    engine.py         # TradingEngine — owns the loop, drives strategies
    persona.py        # AIPersona — briefings, narration, intent replies
    store.py          # SQLite: trades, baskets, equity curve, sessions
    server.py         # stdlib HTTP + SSE. Serves frontend/ and /api/*
    strategies\
      base.py         # Strategy ABC + Signal/Intent dataclasses + REGISTRY
      recovery_grid.py# the video's strategy (class S)
      ai_scalper.py
      trend_ai.py
      mean_reversion.py
  frontend\ index.html, css\, js\, assets\
  data\ infinity.db   logs\ engine.log   config.json
```

## 3. config.json (single source of truth)

```json
{
  "account": {
    "login": 0, "password": "", "server": "",
    "terminal_path": "C:\\Program Files\\MetaTrader 5\\terminal64.exe",
    "connect_timeout_ms": 10000,
    "allow_live_real": false
  },
  "symbol": {"name": "auto", "candidates": ["XAUUSDm","XAUUSD","GOLD","GOLD.","XAUUSD.a","XAUUSDc"]},
  "engine": {"enabled": false, "active_strategy": "recovery_grid", "poll_ms": 1000, "magic": 100100},
  "risk": {
    "min_margin_level_pct": 300.0, "max_drawdown_pct": 30.0,
    "daily_loss_limit_pct": 20.0, "equity_floor": 0.0,
    "max_basket_layers": 12, "max_total_lots": 0.0, "kill_switch": false
  },
  "strategies": {
    "recovery_grid": {
      "base_lot": 0.01,
      "lot_tiers": [[0,0.01],[100,0.02],[200,0.05],[500,0.10],[1400,0.50],[20000,2.0],[150000,3.0],[200000,4.0]],
      "grid_step_usd": 0.35, "max_layers": 9, "layer_multiplier": 1.0,
      "basket_tp_usd": 0.0, "basket_tp_pct_equity": 1.5,
      "basket_sl_pct_equity": 0.0, "volume_split_max_lot": 2.0,
      "max_spread_usd": 0.60, "cooldown_s": 20,
      "direction_mode": "momentum", "momentum_tf": "M5", "momentum_period": 20
    }
  },
  "user": {"name": "Theophilus", "risk_profile": "Moderate",
           "sessions": ["London","New York"], "favourite_symbol": "Gold"}
}
```

`account.password` may also come from env `INFINITY_MT5_PASSWORD` (preferred).
When it does, `save_config` writes a blank password to the file, so the
environment stays the only place the live secret exists.

Everything in `account` can be set from the UI (Link Broker panel →
`POST /api/broker/link`), so nobody has to hand-edit this file to start trading.
`load_config` heals a hostile file rather than raising: a non-finite number
(`Infinity`, `NaN`, `1e400`) falls back to the default, an absurd finite one is
clamped, and a wrong-typed section is replaced — the app is the user's only way
back in, so it has to start.

## 4. mt5_bridge.py — the ONLY module importing MetaTrader5

```python
class MT5Bridge:
    def __init__(self, config: dict, logger) -> None
    # lifecycle
    def connect(self) -> dict      # {ok, stage, error, account, terminal, symbol}
    def disconnect(self) -> None
    def is_connected(self) -> bool
    # state  (all return plain dicts/lists — JSON-safe, never namedtuples)
    def account(self) -> dict|None # login,server,currency,balance,equity,margin,
                                   # margin_free,margin_level,leverage,trade_mode,
                                   # trade_allowed,is_real,so_call,so_so
    def symbol_info(self) -> dict|None  # name,bid,ask,spread_usd,point,digits,
                                        # volume_min,volume_max,volume_step,
                                        # trade_contract_size,trade_mode
    def tick(self) -> dict|None    # bid, ask, time, spread_usd
    def positions(self) -> list[dict]  # ticket,symbol,type('buy'/'sell'),volume,
                                       # price_open,price_current,profit,swap,magic,time
    def candles(self, timeframe: str, count: int) -> list[dict]  # time,open,high,low,close
    # execution — every method returns {ok, retcode, comment, tickets:[], detail}
    def market_order(self, side: str, volume: float, comment: str) -> dict
    def modify_stops(self, ticket: int, sl: float = 0.0, tp: float = 0.0) -> dict
    def close_position(self, ticket: int) -> dict
    def close_all(self, magic: int|None = None) -> dict
```

Rules:
- `connect()` resolves the symbol from `symbol.candidates` against the broker's real
  list (brokers rename Gold). It must `symbol_select()` before quoting.
- If `account.trade_mode == 2` (REAL) and `allow_live_real` is False →
  `connect()` returns `{ok: False, stage: "safety", error: "..."}`. Never trade.
- `market_order` splits `volume` into chunks of `min(volume_split_max_lot, volume_max)`
  rounded to `volume_step`, and returns every resulting ticket. This reproduces the
  video's identical-entry-price position groups.
- `modify_stops` takes **absolute levels**, not distances, and `0.0` on a side means
  "leave that side as it is". MT5 reads a zero in an SLTP request as *remove this
  stop*, so the side that is not changing must be read off the position and sent
  back unchanged. Levels are clamped against the **current** price, not the entry —
  that is what the terminal validates, and a trailing stop is by definition near price.
- Retry filling modes: try FOK, then IOC, then RETURN. Report which worked.
- Never raise. Always return the result dict.

## 5. strategies/base.py

```python
@dataclass
class Intent:            # what the strategy wants done
    action: str          # 'open' | 'add_layer' | 'close_basket' | 'hold'
    side: str = ""       # 'buy' | 'sell'
    volume: float = 0.0
    reason: str = ""
    sl_distance: float = 0.0   # bracket to set at the broker, as a distance in
    tp_distance: float = 0.0   # PRICE from the entry. 0.0 = do not set one.

@dataclass
class StopMove:          # a change to the bracket on one open position
    ticket: int = 0
    sl: float = 0.0      # ABSOLUTE levels, not distances. 0.0 on either side
    tp: float = 0.0      # means "leave that side alone" — never "remove it".
    reason: str = ""

@dataclass
class Ctx:               # everything a strategy may look at
    account: dict; symbol: dict; tick: dict
    positions: list; candles: list; basket: dict; params: dict
    risk: dict           # the risk block, so a strategy can size against the
    now: float           # same margin floor the engine will judge it by

class Strategy(ABC):
    key: str; title: str; klass: str      # klass in 'S','A','B'
    stats: dict                            # attack/defence/accuracy/dexterity 0-300
    skills: list[str]
    @abstractmethod
    def decide(self, ctx: Ctx) -> Intent: ...
    def manage(self, ctx: Ctx) -> list[StopMove]: ...   # default: []

REGISTRY: dict[str, type[Strategy]]        # populated by @register decorator
```

`stats` drive the roster card bars in the UI. `klass` drives the S/A grouping.

Rules:
- One `Intent` per tick. `manage()` runs first and only when the book is not
  empty, so a stop that should move to break-even is never lost to a tick that
  ends in an entry error.
- `manage()` must be **idempotent** (it is called once a second for the life of
  the position) and must **ratchet** — only ever move a stop in the direction
  that reduces risk. Enforce this in the strategy: `TradingEngine._manage`
  forwards every move with a ticket verbatim and does not check.
- A bracket a strategy states in cash must be converted to price before it
  reaches `Intent`: money and price are only the same number on gold, where
  `0.01 lot x contract 100` makes the divisor exactly 1.0.

## 6. strategies/recovery_grid.py — reproduce the video

Observed and REQUIRED behaviour (from frame analysis of the reference video):
1. **Basket** = all engine positions on the symbol, same direction, managed as a unit.
2. **Open**: when flat and cooldown elapsed and spread <= `max_spread_usd`, open in
   the `direction_mode` direction with `lot_for_equity(equity)`.
3. **Volume split**: a target volume above `volume_split_max_lot` is sent as several
   orders → several positions share one entry price (video: 8 × sell 2.00 @ 4394.558).
4. **Add layer**: if price moved `grid_step_usd` against the basket and
   `layers < max_layers`, add another position at the same or multiplied lot.
5. **Close basket**: when basket floating P/L >= target
   (`basket_tp_usd` or `basket_tp_pct_equity` × equity) → close every position at once
   (video: all positions share one exit price).
6. **No stop loss by default** — it absorbs drawdown (video held −333.90 on 1489).
7. **Lot ladder** from `lot_tiers`: highest tier whose threshold <= equity.
   Video ladder: 0.01→0.02→0.05→0.10→0.50→2.00→3.00→4.00.

The strategy returns Intents only. It never calls the bridge.

## 6b. strategies/ai_scalper.py — the second recording

Rebuilt from a second reference recording, which shows the same burst mechanic on
a much shorter timeframe. Observed and REQUIRED behaviour:

1. **Burst entry**: one signal becomes several same-side positions at the *same*
   entry price, uniform lot, opened together — not a ladder spread across a move.
   Implemented as one `Intent` of `unit x layers`, which `market_order` splits.
2. **No noise-tight stop**: the floating loss on an individual leg breathes. The
   shipped version of this strategy stopped each position at `sl_usd` 0.80
   converted to price against an M1 ATR(14) of 3.806 — **0.21x ATR** — and lost
   36 of 41 baskets, −$77.66, at a 12.2% win rate against the 38.7% it needed.
3. **Whole-basket exit**: the group closes on *aggregate* P/L (`tp_usd` / `sl_usd`
   per 0.01 lot × open lots), never leg by leg.
4. **Uniform lot**: every rung is `base_lot`. No martingale, and deliberately not
   `lot_for_equity` — growth adds rungs, it does not inflate them.

Two brackets, and the ordering between them is the whole design:

- the **basket** money bracket is the primary exit and the number `_expectancy`
  judges, so what the UI reports is what trades;
- the **broker-side** bracket is a catastrophe net for the case this app is not
  running to enforce the basket. It is `sl_atr_mult × ATR(14)`, floored at
  `broker_net_mult ×` the basket's own bracket on **both** legs so the net stays
  outside the basket exit at every volatility — a net tighter than the basket
  stops being a net and becomes the strategy.

**Layer count is derived, never hard-coded.** The recording's eight positions are
a property of its broker: eight rungs cost almost nothing at 1:500. The count
comes from `symbol.margin_per_lot` (the broker's own `order_calc_margin` answer,
preferred over the leverage formula) against `risk.min_margin_level_pct` × the
1.25 band `RiskManager` actually enforces, then capped by `max_positions` and
`risk.max_basket_layers`. Measured on Deriv, that is 3 rungs at $2,714 equity,
not 8. `0` rungs means "the floor leaves no room"; a broker whose margin cannot
be priced gets `1`, never a burst.

`min_spacing_usd` defaults to `0.0`: a burst is *supposed* to share one price,
and a 0.60 spacing veto is what held `avg_layers` at exactly 1.0 across all 41
baskets, so the stacking never fired once.

## 7. risk.py

```python
class RiskManager:
    def __init__(self, config, logger)
    def evaluate(self, account: dict, positions: list, day_start_equity: float) -> dict
    # -> {allow_open: bool, force_close: bool, kill: bool,
    #     margin_level: float, stop_out_distance_pct: float,
    #     drawdown_pct: float, daily_pl_pct: float,
    #     level: 'safe'|'caution'|'danger'|'critical', reasons: [str]}
```
- `force_close` when margin_level <= `min_margin_level_pct`, drawdown breach,
  daily loss breach, or equity below floor.
- `kill` when `config.risk.kill_switch` is true → engine closes all and stops.
- `stop_out_distance_pct` = how far above the broker's `so_so` level we are.
  The reference account hit **80.38%** margin level. Surface it, never hide it.

## 8. engine.py

```python
class TradingEngine:
    def __init__(self, config, bridge, risk, store, persona, logger)
    def start(self) -> dict; def stop(self) -> dict
    def status(self) -> dict     # the SSE payload, see §10
    def set_strategy(self, key: str) -> dict
    def close_all_now(self) -> dict
```
One background thread. Each tick: gather Ctx → RiskManager first (risk overrides
strategy) → strategy.decide() → execute via bridge → persist to store → publish.
Engine must survive bridge errors: log, back off, keep running.

## 9. persona.py

```python
class AIPersona:
    def briefing(self, ctx: dict) -> dict   # {greeting, lines[], suggestions[]}
    def narrate(self, event: dict) -> str
    def reply(self, message: str, ctx: dict) -> dict
```
Briefing must read like:
> Hi Theophilus. Welcome back to INFINITY.100 AI. You usually trade Gold during the
> London and New York sessions. Your risk profile is Moderate. Yesterday the AI closed
> three positions for a total gain of 2.1%. Gold is consolidating below resistance.

Suggestions: scan opportunities / continue automation / explain today's market /
review yesterday's trades. Deterministic templates — no external LLM calls.

## 10. server.py — stdlib only

`http.server.ThreadingHTTPServer`, port **8100**. Serves `frontend/` at `/`.

| Method | Route | Body / returns |
|---|---|---|
| GET | `/api/status` | full status snapshot |
| GET | `/api/stream` | **SSE**, `event: status`, one JSON per second |
| GET | `/api/strategies` | roster: key,title,klass,stats,skills,level,equity_tag |
| GET | `/api/config` | config with password redacted to `"***"` |
| POST | `/api/config` | merge patch, save, return config |
| GET | `/api/broker` | account view (no password), discovered terminals + server names, connect state |
| POST | `/api/broker/link` | `{login,server,password,terminal_path,allow_live_real,connect}` — all optional, see below |
| POST | `/api/connect` | `{}` → asks the poll thread to connect, returns `pending` at once |
| POST | `/api/disconnect` | `{}` → stops automation, drops the link, stays offline |
| POST | `/api/engine/start` \| `/stop` | engine control |
| POST | `/api/engine/strategy` | `{"key": "..."}` |
| POST | `/api/engine/close_all` | flatten now |
| POST | `/api/risk/kill` | `{"on": true}` |
| GET | `/api/history?limit=` | closed baskets + equity curve |
| POST | `/api/assistant` | `{"message": "..."}` → persona.reply |

`/api/connect` and `/api/disconnect` must never call the bridge on the HTTP
thread: `mt5.initialize()` holds the GIL for its whole timeout, so that freezes
the UI, the SSE stream and every other request together. They call
`engine.request_connect()` / `request_disconnect()`, which only set intent — the
poll thread does the work and the UI watches `mt5_stage` / `notice`.

`/api/broker/link` rules (`server.parse_link`):
* an omitted field keeps its saved value; `password: "***"` means unchanged
* an unknown field is a 400, so a typo cannot be silently ignored
* `login` must be digits (`0` = "use whoever is logged in to the terminal")
* `allow_live_real` must be a real boolean — the string `"false"` is a 400
* a login with no password anywhere is a 400 **before** MT5 sees it, so a
  guess never costs a failed attempt against a real account
* changing login/server/password/terminal\_path while connected stops
  automation and drops the link first — a strategy tracks baskets by ticket on
  the account that opened them
* the password is never in any response; `password_set` / `password_from_env`
  say whether one exists and where it comes from

### The door — who may talk to this server at all
Binding to 127.0.0.1 keeps the network out but not *web pages*: any site the
user visits can aim a request at `http://localhost:8100`. So on top of the bind:

* **every** method, including GET, needs a loopback `Host` — DNS rebinding means
  a page on evil.com resolving to 127.0.0.1 reaches this server *as* evil.com,
  and the browser then treats the reply as same-origin (403 otherwise)
* every POST needs `Content-Type: application/json` — a bodiless or
  `text/plain` POST is a *simple* cross-origin request that needs no preflight,
  so demanding the header is what forces the preflight we never answer (415)
* a POST carrying `Origin` needs a loopback one, and `Sec-Fetch-Site` must not
  be cross-site; `Origin: null` (a `file://` page or sandboxed frame) is refused

Status snapshot shape (frontend depends on exactly this):
```json
{"ts":0,"connected":false,"engine":{"running":false,"strategy":"recovery_grid"},
 "account":{"login":0,"server":"","currency":"USD","balance":0,"equity":0,
            "margin":0,"margin_free":0,"margin_level":0,"leverage":0,
            "is_real":false,"trade_mode":"DEMO"},
 "symbol":{"name":"","bid":0,"ask":0,"spread_usd":0},
 "basket":{"open":false,"side":"","layers":0,"lots":0,"profit":0,"avg_price":0,
           "tp_target":0,"positions":[]},
 "risk":{"level":"safe","margin_level":0,"stop_out_distance_pct":0,
         "drawdown_pct":0,"daily_pl_pct":0,"reasons":[]},
 "stats":{"baskets_closed":0,"wins":0,"losses":0,"net_pl":0,"win_rate":0,
          "peak_equity":0,"start_equity":0},
 "equity_curve":[[ts,equity]],
 "notice":"","blocked":false,"connecting":false,"mt5_stage":"","mt5_error":"",
 "uptime_s":0}
```
`notice`, `connecting`, `mt5_stage` and `mt5_error` are present in **every**
snapshot, connected or not, so the UI can bind to them without first checking
whether there is a broker. `risk.kill` likewise survives a disconnect — it is a
config setting, not a broker fact, and dropping it let the kill switch be armed
and never disarmed.

Redact the password everywhere. Bind **127.0.0.1 only**.

## 11. Frontend — glass over the robot hero

Recreate the shared reference screen's structure, mapped to trading:

| Reference | INFINITY.100 AI |
|---|---|
| "Your Robots" | **Your AI Traders** |
| CLASSES S — A | STRATEGY CLASSES S — A |
| Robot list (BERSERKER…) | strategy roster cards |
| "Robot 2300.10" | strategy id + equity tag |
| 51 LEVEL | strategy level |
| ATTACK / DEFENCE / ACCURACY / DEXTRETY | Aggression / Risk control / Accuracy / Execution |
| 3 skill tiles | active modules |
| coin pill 2523 | live equity pill |
| ADD ROBOTS + | ADD STRATEGY + |
| **START** | **DEPLOY** (start engine) |
| wifi glyph | MT5 connection state |

Requirements:
- Background: `frontend/assets/hero.png` if present, else the procedural CSS/SVG
  hero (angular white helmet, red ring eyes, black hood, pale grey studio gradient).
  Same composition either way — figure centre, roster left, stats right.
- **Glassmorphism throughout**: `backdrop-filter: blur(20px) saturate(160%)`,
  translucent white fills, 1px light borders, layered soft shadows, specular top edge.
  Must degrade gracefully where `backdrop-filter` is unsupported.
- Palette from the reference: near-white `#f4f5f7`, charcoal `#16181d`,
  red accent `#e8362a` (the eyes), plus green `#28c76f` for profit.
- Live: subscribe to `/api/stream`, bind every field, animate stat bars and equity.
- Risk panel is always visible: margin level + distance to stop-out, colour-coded.
- Responsive to 1280×800 and up; degrade to single column under 980px.
- Accessible: real buttons, `aria-live` on the assistant, visible focus rings,
  contrast-safe text on glass.
