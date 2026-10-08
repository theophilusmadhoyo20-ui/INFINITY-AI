# INFINITY.100 AI — AI Trading Operating System

A self-hosted trading desk for MetaTrader 5. One Python process runs a strategy
engine, a risk manager and a local web UI; the UI is glass over a robot hero and
shows every number the engine is acting on — margin level, distance to stop-out,
basket P/L, and what each strategy needs to win in order to break even.

Python 3.10, **standard library only** plus the `MetaTrader5` package. The
frontend is vanilla HTML/CSS/JS with no build step and no npm.

---

## Status: this is a research rig, not a money printer

The app's own measured record, read from `data/infinity.db` on 2026-10-06:

| strategy | baskets | wins | win rate | net | needs to win |
|---|---|---|---|---|---|
| Recovery Grid | 118 | 25 | 21.2% | −997.61 | 31.5% |
| AI SCALPER | 47 | 7 | 14.9% | −108.40 | 41.8% |
| Mean Reversion | 4 | 1 | 25.0% | −48.77 | 47.6% |

All three are **losing**, and the UI says so on the strategy card rather than
hiding it. `required_win_pct = risk / (risk + reward)` is computed from the
configuration before a single trade, so a bracket that cannot pay is visible on
day one instead of after the account is down. Once 30 baskets have closed, the
measured reward/risk replaces the configured one — spread and swap included.

Run it on a demo account. The `allow_live_real` flag exists so that pointing it
at real money has to be a deliberate act.

---

## Setup

```bash
git clone <your-repo-url> INFINITY100-AI
cd INFINITY100-AI
pip install MetaTrader5
cp config.example.json config.json
```

Then either put your broker details in `config.json`, or leave it alone and use
the **LINK BROKER** button in the UI — it writes the same file.

Keep the password out of the file if you can:

```bash
set INFINITY_MT5_PASSWORD=your-password        # Windows
export INFINITY_MT5_PASSWORD=your-password     # bash
```

When that variable is set, `save_config` writes a blank password to disk, so the
environment stays the only place the live secret exists. `config.json` is
gitignored either way.

## Running

```bash
python run.py
```

The UI comes up on <http://127.0.0.1:8100>. Useful flags:

| flag | effect |
|---|---|
| `--port 9000` | bind another port (also reads `$PORT`) |
| `--no-connect` | start the UI without touching MetaTrader 5 |
| `--open` | open a browser when it is ready |
| `--verbose` | debug logging |

The HTTP server binds **before** anything touches MetaTrader 5, on purpose:
`mt5.initialize()` is a native call that holds the GIL for its whole timeout, so
a stalled broker connection would otherwise freeze the UI along with it.
Automation stays idle until you press **DEPLOY**.

## Architecture

```
run.py                 single entry point, wires everything, owns shutdown order
backend/
  config.py            load/save config.json, defaults, env overrides, migrations
  mt5_bridge.py        the ONLY module that imports MetaTrader5
  risk.py              margin floor, drawdown, daily loss, kill switch
  engine.py            one background thread: gather → risk → decide → execute
  persona.py           deterministic briefings and narration, no external LLM
  store.py             SQLite: baskets, fills, equity curve, events
  server.py            stdlib HTTP + SSE, serves frontend/ and /api/*
  strategies/
    base.py            Strategy ABC, Intent/StopMove/Ctx, indicators, TickView
    recovery_grid.py   class S — layered basket, no stop by default
    ai_scalper.py      class A — burst entry, basket exit
    trend_ai.py        class A — EMA trend with an ADX filter
    mean_reversion.py  class B — Bollinger reversion
frontend/              index.html + css/app.css + js/app.js, no build step
docs/CONTRACT.md       the authoritative module contract — read this first
tools/                 dry-run harness, preview builder, parameter sweeps
```

`docs/CONTRACT.md` is authoritative. Every module conforms to it; where code and
contract disagree, that is a bug in one of them.

### The rules that matter

- **Only `mt5_bridge.py` imports MetaTrader5.** Everything else gets plain dicts.
- **Strategies return intent, never orders.** A strategy emits one `Intent` per
  tick and a list of `StopMove`s. It never calls the bridge.
- **`manage()` must be idempotent and must ratchet.** It is called once a second
  for the life of a position, and `TradingEngine._manage` forwards every move
  verbatim without checking — so a stop that must only move in the risk-reducing
  direction has to enforce that in the strategy.
- **`StopMove` levels are absolute; `0.0` means "leave that side alone".** MT5
  reads a zero in an SLTP request as *remove this stop*, so the side that is not
  changing is read off the position and sent back unchanged.
- **`Intent.sl_distance` / `tp_distance` are distances in price, not cash.** Money
  and price are only the same number on gold, where `0.01 lot × contract 100`
  makes the divisor exactly 1.0.
- **Risk overrides strategy.** `RiskManager.evaluate()` runs before `decide()`, and
  a `force_close` flattens regardless of what the strategy wanted.
- **The engine survives bridge errors.** Log, back off, keep running.

## Self-tests

No pytest, no dependencies — each module tests itself:

```bash
python -m backend.config        # config healing, migrations, redaction
python -m backend.risk          # margin maths, stop-out distance
python -m backend.engine_test   # engine + strategies against a fake bridge
python -m backend.strategies     # strategy contracts on synthetic contexts
```

`python -m backend.mt5_bridge` also exists, but it **connects to the terminal** —
back-to-back runs can wedge MetaTrader 5 into an IPC timeout that hangs
`initialize()` for minutes. Run it deliberately, once, never in a loop.

## Security

The server binds `127.0.0.1` only, but that keeps out the network, not *web
pages* — any site you visit can aim a request at `http://localhost:8100`. So on
top of the bind:

- every method, GET included, needs a loopback `Host` (DNS rebinding otherwise
  lets `evil.com` resolve to 127.0.0.1 and be treated as same-origin)
- every POST needs `Content-Type: application/json`, which forces a preflight
  that is never answered
- a POST carrying `Origin` needs a loopback one, and `Sec-Fetch-Site` must not be
  cross-site; `Origin: null` is refused

The password is redacted from every response and every log line. The link
dialog's password field is write-only — the API will say *a password is set*, and
never what it is.

## Licence

No licence yet. All rights reserved by default until one is chosen.
