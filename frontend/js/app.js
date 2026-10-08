/* ═══════════════════════════════════════════════════════════════════════════
   INFINITY.100 AI — frontend controller
   Vanilla ES2018, no build step, no CDN. Every DOM lookup is guarded so a
   missing element degrades one widget instead of killing the whole page.
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  "use strict";

  // ── tiny DOM helpers ─────────────────────────────────────────────────── //
  var $ = function (id) { return document.getElementById(id); };

  function text(el, value) {
    if (el && el.textContent !== value) { el.textContent = value; }
  }
  function cls(el, name, on) {
    if (el) { el.classList.toggle(name, !!on); }
  }
  function attr(el, name, value) {
    if (el) { el.setAttribute(name, value); }
  }
  function num(v, fallback) {
    var n = typeof v === "number" ? v : parseFloat(v);
    return isFinite(n) ? n : (fallback === undefined ? 0 : fallback);
  }
  function clamp(v, lo, hi) { return v < lo ? lo : (v > hi ? hi : v); }

  // ── formatting ───────────────────────────────────────────────────────── //
  var CURRENCY = "USD";
  var moneyFmt = null;

  function currency(code) {
    if (code && code !== CURRENCY) { CURRENCY = code; moneyFmt = null; }
    if (!moneyFmt) {
      try {
        moneyFmt = new Intl.NumberFormat(undefined, {
          style: "currency", currency: CURRENCY,
          minimumFractionDigits: 2, maximumFractionDigits: 2
        });
      } catch (e) {                                   // unknown currency code
        moneyFmt = new Intl.NumberFormat(undefined, {
          minimumFractionDigits: 2, maximumFractionDigits: 2
        });
      }
    }
    return moneyFmt;
  }
  function money(v) { return currency().format(num(v)); }
  function signed(v) {
    var n = num(v);
    return (n > 0 ? "+" : "") + money(n);
  }
  function pct(v, digits) {
    return num(v).toFixed(digits === undefined ? 2 : digits) + "%";
  }
  function price(v, digits) {
    return num(v).toFixed(digits === undefined ? 2 : digits);
  }
  function lots(v) { return num(v).toFixed(2); }
  function clock(ts) {
    var d = new Date(num(ts) * 1000);
    if (isNaN(d.getTime())) { return ""; }
    return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit" });
  }
  function tone(el, n) {
    if (!el) { return; }
    el.classList.remove("up", "down", "flat");
    el.classList.add(n > 0 ? "up" : (n < 0 ? "down" : "flat"));
  }

  // ── HTTP ─────────────────────────────────────────────────────────────── //
  function getJSON(path) {
    return fetch(path, { headers: { Accept: "application/json" } })
      .then(function (r) {
        if (!r.ok) { throw new Error("HTTP " + r.status + " on " + path); }
        return r.json();
      });
  }
  function postJSON(path, body) {
    return fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body || {})
    }).then(function (r) {
      return r.json().catch(function () { return { ok: r.ok }; });
    });
  }

  // ── state ────────────────────────────────────────────────────────────── //
  var S = {
    snap: null,
    roster: [],
    selected: "",          // key of the card shown in the centre
    activeKey: "",         // key the engine is actually configured with
    lastActivityTs: 0,
    briefed: false,
    busy: false,
    stream: null,
    streamTries: 0,
    pollTimer: null,
    dockMode: ""           // last applied assistant layout: rail/overlay/static
  };

  // ══ 1. hero backdrop: use assets/hero.png when it really loads ════════ //
  (function heroProbe() {
    var probe = new Image();
    probe.onload = function () {
      if (!probe.naturalWidth) { return; }
      var stage = document.querySelector(".stage__photo");
      if (stage) {
        stage.style.backgroundImage = 'url("assets/hero.png")';
      }
      document.documentElement.setAttribute("data-hero", "photo");
    };
    probe.onerror = function () {
      document.documentElement.setAttribute("data-hero", "procedural");
    };
    probe.src = "assets/hero.png";
  }());

  // ══ 2. roster ═════════════════════════════════════════════════════════ //
  function loadRoster() {
    return getJSON("/api/strategies").then(function (data) {
      S.roster = (data && data.strategies) || [];
      S.activeKey = (data && data.active) || "";
      if (!S.selected || !cardFor(S.selected)) {
        S.selected = S.activeKey || (S.roster[0] && S.roster[0].key) || "";
      }
      renderRoster();
      renderSelected();
    }).catch(function (err) {
      say("I could not load the strategy roster: " + err.message, "ai");
    });
  }

  function cardFor(key) {
    for (var i = 0; i < S.roster.length; i++) {
      if (S.roster[i] && S.roster[i].key === key) { return S.roster[i]; }
    }
    return null;
  }

  function renderRoster() {
    var host = $("roster");
    text($("rosterCount"), String(S.roster.length));
    rosterClasses();
    if (!host) { return; }
    host.textContent = "";
    S.roster.forEach(function (card) {
      var li = document.createElement("li");
      var b = document.createElement("button");
      b.type = "button";
      b.className = "rbtn";
      // the key the row stands for, so select() can find this button again after
      // the list has been rebuilt underneath it
      b.setAttribute("data-key", String(card.key || ""));
      b.setAttribute("aria-current", card.key === S.selected ? "true" : "false");

      var k = document.createElement("span");
      k.className = "rbtn__k";
      k.setAttribute("data-k", String(card.klass || ""));
      k.textContent = String(card.klass || "?");

      var body = document.createElement("span");
      body.className = "rbtn__body";
      var name = document.createElement("span");
      name.className = "rbtn__name";
      name.textContent = String(card.title || card.key || "—");
      if (card.key === S.activeKey) {
        var live = document.createElement("span");
        live.className = "rbtn__live";
        live.title = "configured strategy";
        name.appendChild(live);
      }
      var tag = document.createElement("span");
      tag.className = "rbtn__tag";
      tag.textContent = String(card.equity_tag || "");
      body.appendChild(name);
      body.appendChild(tag);

      // a strategy the arithmetic says cannot pay should be visible in the list,
      // not only after you click into it
      var verdict = String((card.expectancy || {}).verdict || "");
      if (verdict === "losing" || verdict === "cost-heavy") {
        var warn = document.createElement("span");
        warn.className = "rbtn__warn";
        warn.textContent = "!";
        warn.title = String((card.expectancy || {}).note || "");
        name.appendChild(warn);
      }

      var lv = document.createElement("span");
      lv.className = "rbtn__lv";
      lv.textContent = String(num(card.level));
      var small = document.createElement("small");
      small.textContent = "LEVEL";
      lv.appendChild(small);

      b.appendChild(k);
      b.appendChild(body);
      b.appendChild(lv);
      b.addEventListener("click", function () { select(card.key); });
      li.appendChild(b);
      host.appendChild(li);
    });
  }

  //: The roster subtitle was hardcoded "STRATEGY CLASSES S — A" in the markup.
  //: That was true of the two strategies that shipped first and wrong the moment
  //: a class B arrived, so derive it from the roster the backend actually sent.
  function rosterClasses() {
    var el = $("rosterSub");
    if (!el) { return; }
    var seen = [];
    S.roster.forEach(function (card) {
      var k = String((card && card.klass) || "").trim().toUpperCase();
      if (k && seen.indexOf(k) < 0) { seen.push(k); }
    });
    var ORDER = "SABCDEF";
    seen.sort(function (a, b) {
      var ia = ORDER.indexOf(a), ib = ORDER.indexOf(b);
      return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib);
    });
    el.textContent = seen.length ? "STRATEGY CLASSES " : "STRATEGY CLASSES";
    seen.forEach(function (k, i) {
      if (i) { el.appendChild(document.createTextNode(" — ")); }
      var b = document.createElement("b");
      b.textContent = k;
      el.appendChild(b);
    });
  }

  function select(key) {
    S.selected = key;
    // renderRoster() replaces every button in the list, which drops focus to
    // <body> — a keyboard user lands back at the top of the document after every
    // choice. Note where focus was and put it back on the row they picked.
    var active = document.activeElement;
    var hadFocus = !!(active && active.classList && active.classList.contains("rbtn"));
    renderRoster();
    renderSelected();
    if (hadFocus) {
      var rows = document.querySelectorAll(".rbtn");
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].getAttribute("data-key") === key) { rows[i].focus(); break; }
      }
    }
  }

  var ATTR_MAX = 300;   // roster stats are 0-300 in the strategy describe()

  function renderSelected() {
    var card = cardFor(S.selected) || S.roster[0] || null;
    if (!card) { return; }
    text($("selTitle"), String(card.title || card.key || "—"));
    text($("selTag"), String(card.equity_tag || ""));
    text($("selLevel"), String(num(card.level)));
    text($("selBlurb"), String(card.blurb || ""));
    text($("klassLetter"), String(card.klass || "?"));
    attr($("klassBadge"), "data-k", String(card.klass || ""));

    var stats = card.stats || {};
    var boxes = document.querySelectorAll(".attr");
    for (var i = 0; i < boxes.length; i++) {
      var box = boxes[i];
      var value = num(stats[box.getAttribute("data-k")]);
      var fill = box.querySelector(".attr__fill");
      var out = box.querySelector(".attr__num");
      if (fill) { fill.style.setProperty("--w", clamp(value / ATTR_MAX * 100, 0, 100) + "%"); }
      text(out, String(Math.round(value)));
    }

    var host = $("skills");
    if (host) {
      host.textContent = "";
      (card.skills || []).slice(0, 3).forEach(function (name) {
        var tile = document.createElement("div");
        tile.className = "skill";
        var dot = document.createElement("span");
        dot.className = "skill__dot";
        var label = document.createElement("span");
        label.className = "skill__name";
        label.textContent = String(name);
        tile.appendChild(dot);
        tile.appendChild(label);
        host.appendChild(tile);
      });
    }
    renderExpectancy(card);
    syncDeployLabel();
  }

  //: how each verdict reads on the card. The wording is deliberately flat — a
  //: strategy that cannot pay for its entry should not be dressed up, and one
  //: that is working should not be sold either.
  var VERDICTS = {
    "working": { word: "On track", tone: "ok" },
    "losing": { word: "Losing money", tone: "bad" },
    "cost-heavy": { word: "Spread eats the target", tone: "bad" },
    "no-stop": { word: "No stop loss", tone: "warn" },
    "unproven": { word: "Not proven yet", tone: "idle" }
  };

  //: whole percents only. The note beside these numbers says "needs 40%", and a
  //: headline reading 40.1% next to it implies a precision a break-even estimate
  //: does not have. Named pctWhole because this used to be a second `function
  //: pct` — hoisting made it replace the general pct(v, digits) for the whole
  //: closure, so every `digits` argument in apply() was discarded and a -0.004%
  //: drawdown rendered as "-0%".
  function pctWhole(value) {
    return (value === null || value === undefined || value !== value)
      ? "—" : Math.round(Number(value)) + "%";
  }

  function renderExpectancy(card) {
    var host = $("expc");
    if (!host) { return; }
    var e = (card && card.expectancy) || null;
    // nothing measured and no bracket to reason about: showing an empty panel of
    // dashes would imply the app knows something it doesn't
    if (!e || (e.required_win_pct === undefined && !e.no_stop && !e.baskets_closed)) {
      host.hidden = true;
      return;
    }
    host.hidden = false;
    var v = VERDICTS[String(e.verdict)] || VERDICTS.unproven;
    attr(host, "data-tone", v.tone);
    text($("expcVerdict"), v.word);
    text($("expcFrom"), e.required_from === "trades"
      ? "from " + e.baskets_closed + " closed trades"
      : (e.required_from === "settings" ? "from the settings" : ""));
    text($("expcNeed"), e.no_stop ? "n/a" : pctWhole(e.required_win_pct));
    text($("expcGot"), e.baskets_closed ? pctWhole(e.win_pct) : "—");
    text($("expcRR"), e.reward_risk ? (Math.round(e.reward_risk * 100) / 100) + "×" : "—");
    text($("expcNote"), String(e.note || ""));
  }

  function syncDeployLabel() {
    var btn = $("deployBtn");
    if (!btn) { return; }
    var running = !!(S.snap && S.snap.engine && S.snap.engine.running);
    var switching = S.selected && S.activeKey && S.selected !== S.activeKey;
    cls(btn, "is-live", running);
    btn.textContent = running ? "STOP" : (switching ? "DEPLOY THIS" : "DEPLOY");
    btn.disabled = !!S.busy;
  }

  // ══ 3. live snapshot binding ══════════════════════════════════════════ //
  function apply(snap) {
    if (!snap || typeof snap !== "object") { return; }
    applyDockMode();   // cheap; catches window resizes even if the event is lost
    S.snap = snap;
    var acct = snap.account || {};
    var sym = snap.symbol || {};
    var basket = snap.basket || {};
    var risk = snap.risk || {};
    var stats = snap.stats || {};
    var eng = snap.engine || {};
    var digits = num(sym.digits, 2);
    var live = !!snap.connected;

    currency(acct.currency);

    // -- connection + mode ------------------------------------------------ //
    var conn = $("connBtn");
    if (conn) {
      conn.classList.remove("is-live", "is-off", "is-busy");
      conn.classList.add(snap.connected ? "is-live" : "is-off");
      if (S.busy) { conn.classList.add("is-busy"); }
    }
    text($("connText"), snap.connected ? "MT5 live" : "offline");
    var mode = snap.connected ? String(acct.trade_mode || "DEMO") : "NO LINK";
    text($("modeText"), mode);
    cls($("modePill"), "real", /REAL/i.test(mode));
    linkSync(snap);   // the way in stays visible for as long as there is no link

    // -- equity pill ------------------------------------------------------ //
    text($("equityPill"), snap.connected ? money(acct.equity) : "—");
    var start = num(stats.start_equity);
    var delta = start > 0 ? (num(acct.equity) - start) / start * 100 : 0;
    var deltaEl = $("equityDelta");
    if (deltaEl) {
      var showDelta = live && start > 0;
      deltaEl.textContent = showDelta
        ? (delta >= 0 ? "+" : "") + delta.toFixed(2) + "%" : "";
      deltaEl.classList.remove("up", "down");
      // no text -> no tone. The class stayed on while disconnected, so the pill
      // carried a green "up" state for a percentage it was not showing.
      if (showDelta) { deltaEl.classList.add(delta >= 0 ? "up" : "down"); }
    }

    // -- account panel ---------------------------------------------------- //
    // Every number here comes from the broker, so with no link there is no
    // number. $0.00 for an account nobody has read is a claim the UI cannot
    // back: an em dash says "unknown", a zero says "empty, and I checked".
    text($("symbolChip"), String(sym.name || "—"));
    text($("acEquity"), live ? money(acct.equity) : "—");
    text($("acBalance"), live ? money(acct.balance) : "—");
    var floating = num(basket.profit);
    var floatEl = $("acFloat");
    text(floatEl, live ? signed(floating) : "—");
    tone(floatEl, live ? floating : 0);
    text($("acFree"), live ? money(acct.margin_free) : "—");
    text($("acBid"), live ? price(sym.bid, digits) : "—");
    text($("acAsk"), live ? price(sym.ask, digits) : "—");
    text($("acSpread"), live ? price(sym.spread_usd, digits) : "—");
    text($("acLev"), live && num(acct.leverage) ? "1:" + num(acct.leverage) : "—");
    drawCurve(snap.equity_curve || []);

    // -- risk ------------------------------------------------------------- //
    var panel = $("riskPanel");
    var level = String(risk.level || "safe").toLowerCase();
    attr(panel, "data-level", level);
    text($("riskLevel"), level.toUpperCase());
    var exposed = num(acct.margin) > 0;
    text($("riskMargin"), exposed ? pct(risk.margin_level, 1) : "—");
    text($("riskClear"), exposed ? pct(risk.stop_out_distance_pct, 1) : "no exposure");
    var fill = $("riskFill");
    if (fill) {
      var width = exposed ? clamp(num(risk.stop_out_distance_pct), 0, 100) : 100;
      fill.style.setProperty("--w", width + "%");
    }
    var dd = $("riskDd");
    text(dd, "-" + pct(Math.abs(num(risk.drawdown_pct))));
    tone(dd, -Math.abs(num(risk.drawdown_pct)));
    var day = $("riskDay");
    var dayPl = num(risk.daily_pl_pct);
    text(day, (dayPl >= 0 ? "+" : "") + pct(dayPl));
    tone(day, dayPl);
    var reasons = $("riskReasons");
    if (reasons) {
      var list = risk.reasons && risk.reasons.length
        ? risk.reasons : [live ? "Nothing to flag." : "No broker connection."];
      list = list.slice(0, 4).map(String);
      // Same signature-compare as linkTerminals(): these four lines change maybe
      // once a minute, and tearing down the <li>s every second restarted their
      // animation and threw away anything the user had selected mid-sentence.
      var sig = list.join("\n");
      if (reasons.getAttribute("data-sig") !== sig) {
        reasons.setAttribute("data-sig", sig);
        reasons.textContent = "";
        list.forEach(function (line) {
          var li = document.createElement("li");
          li.textContent = line;
          reasons.appendChild(li);
        });
      }
    }

    // -- basket ----------------------------------------------------------- //
    var side = basket.open ? String(basket.side || "").toUpperCase() : "flat";
    text($("basketSide"), side || "flat");
    text($("bkLayers"), String(num(basket.layers)));
    text($("bkLots"), lots(basket.lots));
    text($("bkAvg"), num(basket.avg_price) ? price(basket.avg_price, digits) : "—");
    text($("bkTarget"), num(basket.tp_target) ? money(basket.tp_target) : "—");
    var pl = $("bkProfit");
    text(pl, basket.open ? signed(basket.profit) : "—");
    tone(pl, basket.open ? floating : 0);
    var bar = $("bkBar");
    if (bar) {
      var target = num(basket.tp_target);
      // Progress TOWARD the take-profit. This was Math.abs(floating) / target,
      // so a $50 loss against a $100 target filled the bar to 50% — the same
      // picture as being halfway to a win, while pointing the other way. A
      // basket under water is 0% of the way to its target; the track turns red
      // so the direction is still legible at a glance.
      var frac = (basket.open && target > 0 && floating > 0)
        ? clamp(floating / target * 100, 0, 100) : 0;
      bar.style.setProperty("--w", frac + "%");
      cls(bar.parentNode, "is-loss", !!basket.open && floating < 0);
    }
    renderLegs(basket, digits);

    text($("stClosed"), String(num(stats.baskets_closed)) + " · "
      + num(stats.wins) + "W/" + num(stats.losses) + "L");
    text($("stWin"), num(stats.baskets_closed) ? pct(stats.win_rate, 1) : "—");
    var net = $("stNet");
    text(net, signed(stats.net_pl));
    tone(net, num(stats.net_pl));
    text($("stPeak"), money(stats.peak_equity));

    // -- engine state ----------------------------------------------------- //
    if (eng.strategy && eng.strategy !== S.activeKey) {
      S.activeKey = eng.strategy;
      renderRoster();
    }
    var stateEl = $("deployState");
    if (stateEl) {
      var line;
      if (!snap.connected) {
        line = "MetaTrader 5 is not connected — " + (snap.notice || "waiting for the terminal.");
      } else if (eng.running) {
        line = (eng.strategy_title || "Automation") + " is live.";
      } else {
        line = "Idle. Press DEPLOY to hand " + (eng.strategy_title || "the strategy")
          + " the account.";
      }
      text(stateEl, line.trim());
    }
    //: The engine's reason carries live numbers ("6 layers, P/L 2.60 of 20.94
    //: target, 0.71 to next layer") and so changes on every poll. Appended to the
    //: sentence above it rewrote a polite live region once a second, which made a
    //: screen reader recite the state forever and reflowed the paragraph under
    //: the cursor. It belongs in its own aria-hidden node: still on screen, no
    //: longer announced, and no longer able to move the sentence above it.
    text($("deployWhy"), (eng.running && snap.connected) ? String(eng.last_reason || "") : "");
    syncDeployLabel();
    cls($("killBtn"), "is-on", !!risk.kill);

    // -- notice ----------------------------------------------------------- //
    var notice = $("notice");
    if (notice) {
      var msg = String(snap.notice || "");
      if (!msg && !live && snap.mt5_error) { msg = String(snap.mt5_error); }
      notice.hidden = !msg;
      // role="alert" re-announces on every mutation, and this ran once a second.
      // A screen reader was reading the same sentence over and over, so only
      // touch the node when the sentence actually changes. text() already
      // compares before writing; the guard here is about not clearing it either.
      if (msg) { text(notice, msg); }
    }

    // -- ticker + activity -> assistant log ------------------------------- //
    var feed = snap.activity || [];
    if (feed.length) {
      text($("tickerText"), clock(feed[0].ts) + "  " + String(feed[0].text || ""));
      var fresh = feed.filter(function (a) { return num(a.ts) > S.lastActivityTs; });
      // This replay used to be gated on S.lastActivityTs > 0, which threw away
      // everything that happened before the page loaded — including the reason
      // the engine had stopped. Show the last few with their own timestamps, so
      // the log opens on what actually happened rather than on silence.
      fresh.slice(0, S.lastActivityTs > 0 ? 4 : 3).reverse()
        .forEach(function (a) { say(String(a.text || ""), "ai", a.ts); });
      S.lastActivityTs = Math.max(S.lastActivityTs, num(feed[0].ts));
    } else if (!live) {
      text($("tickerText"), snap.notice || "Waiting for MetaTrader 5…");
    }
  }

  //: A leg's net is profit + swap. Only `profit` was shown, so the legs did not
  //: add up to the basket total, which engine.py sums as profit + swap.
  function legNet(p) { return num(p && p.profit) + num(p && p.swap); }

  function renderLegs(basket, digits) {
    var host = $("legs");
    if (!host) { return; }
    var positions = (basket && basket.positions) || [];
    // The structure — how many legs, which way, what size, what entry — only
    // changes when a position opens or closes. The P/L changes every second.
    // Rebuilding all of it every second restarted every row's entry animation
    // and dropped any text the user was mid-way through selecting, so build on
    // structural change and rewrite the P/L cell in place after that.
    var sig = positions.map(function (p) {
      return [num(p.ticket), String(p.type || ""), num(p.volume), num(p.price_open)].join("/");
    }).join("|");
    if (host.getAttribute("data-sig") !== sig) {
      host.setAttribute("data-sig", sig);
      host.textContent = "";
      if (!positions.length) {
        var empty = document.createElement("li");
        empty.className = "legs__empty";
        empty.textContent = "No open positions.";
        host.appendChild(empty);
      } else {
        positions.forEach(function (p, i) {
          var li = document.createElement("li");
          li.className = "leg";
          var n = document.createElement("span");
          n.className = "leg__n";
          n.textContent = "#" + (i + 1);
          var sideName = String(p.type || "").toLowerCase();
          var side = document.createElement("span");
          side.className = "leg__side " + (sideName === "sell" ? "sell" : "buy");
          side.textContent = sideName.toUpperCase() || "—";
          var px = document.createElement("span");
          px.className = "leg__px";
          px.textContent = lots(p.volume) + " @ " + price(p.price_open, digits);
          var pl = document.createElement("span");
          pl.className = "leg__pl";
          li.appendChild(n); li.appendChild(side); li.appendChild(px); li.appendChild(pl);
          host.appendChild(li);
        });
      }
    }
    var cells = host.querySelectorAll(".leg__pl");
    for (var i = 0; i < cells.length && i < positions.length; i++) {
      var net = legNet(positions[i]);
      text(cells[i], signed(net));
      tone(cells[i], net);
    }
  }

  // ── equity sparkline ─────────────────────────────────────────────────── //
  function drawCurve(points) {
    var line = $("curveLine");
    var area = $("curveArea");
    if (!line) { return; }
    var pts = (points || []).filter(function (p) {
      return p && p.length >= 2 && isFinite(num(p[1]));
    });
    if (!pts.length) {
      line.setAttribute("d", "");
      if (area) { area.setAttribute("d", ""); }
      text($("curveLabel"), "equity");
      return;
    }
    var W = 300, H = 64, pad = 4;
    var ys = pts.map(function (p) { return num(p[1]); });
    var lo = Math.min.apply(null, ys), hi = Math.max.apply(null, ys);
    //: A degenerate range is the normal state twice: on the first launch, when
    //: exactly one equity point exists, and any time the account has not moved.
    //: `hi = lo + 1` put every y at (v-lo)/(hi-lo) = 0, which is the FLOOR of the
    //: box, not the centre the comment claimed — so a brand-new account showed
    //: its equity flatlining along the bottom of the panel, in rising-green with
    //: the gain fill lit. Widening symmetrically puts it at 0.5, mid-box.
    var flat = (hi - lo) < 1e-9;
    if (flat) { lo -= 0.5; hi += 0.5; }
    var step = pts.length > 1 ? (W - pad * 2) / (pts.length - 1) : 0;
    var d = "";
    var lastY = 0;
    for (var i = 0; i < pts.length; i++) {
      var x = pad + step * i;
      var y = H - pad - (ys[i] - lo) / (hi - lo) * (H - pad * 2);
      lastY = y;
      d += (i ? " L" : "M") + x.toFixed(1) + " " + y.toFixed(1);
    }
    //: one point is a dot, and a dot draws nothing — carry it across as a level.
    if (pts.length === 1) { d += " L" + (W - pad) + " " + lastY.toFixed(1); }
    line.setAttribute("d", d);
    var rising = ys[ys.length - 1] >= ys[0];
    //: flat is neither up nor down. Colouring it green and filling the area under
    //: it reads as a gain to anyone glancing at the panel, which is the one thing
    //: an untouched account has not done.
    line.setAttribute("stroke", flat ? "rgba(244,245,247,.66)"
      : (rising ? "#28c76f" : "#e8362a"));
    if (area) {
      area.setAttribute("d", d + " L" + (W - pad).toFixed(1)
        + " " + (H - pad) + " L" + pad + " " + (H - pad) + " Z");
      area.setAttribute("fill", (rising && !flat) ? "url(#curveFill)" : "none");
      area.setAttribute("opacity", (rising && !flat) ? ".3" : "0");
    }
    text($("curveLabel"), "equity · " + pts.length + "pt");
  }

  // ══ 4. the live stream ════════════════════════════════════════════════ //
  function openStream() {
    if (typeof EventSource === "undefined") { return startPolling(); }
    try {
      var es = new EventSource("/api/stream");
      S.stream = es;
      es.addEventListener("status", function (ev) {
        S.streamTries = 0;
        stopPolling();
        try { apply(JSON.parse(ev.data)); } catch (e) { /* skip a bad frame */ }
      });
      es.onerror = function () {
        es.close();
        S.stream = null;
        S.streamTries += 1;
        startPolling();                                   // keep the UI alive
        var wait = Math.min(15000, 800 * Math.pow(1.7, Math.min(S.streamTries, 6)));
        setTimeout(openStream, wait);
      };
    } catch (e) {
      startPolling();
    }
  }
  function startPolling() {
    if (S.pollTimer) { return; }
    var tick = function () {
      getJSON("/api/status").then(apply).catch(function () { /* server down */ });
    };
    tick();
    S.pollTimer = setInterval(tick, 2000);
  }
  function stopPolling() {
    if (S.pollTimer) { clearInterval(S.pollTimer); S.pollTimer = null; }
  }

  // ══ 5. assistant ══════════════════════════════════════════════════════ //
  function say(body, who, ts) {
    var log = $("aiLog");
    if (!log) { return; }
    var msg = document.createElement("div");
    msg.className = "msg msg--" + (who === "me" ? "me" : "ai");
    msg.textContent = String(body);
    var time = document.createElement("span");
    time.className = "msg__time";
    time.textContent = clock(ts || Date.now() / 1000);
    msg.appendChild(time);
    log.appendChild(msg);
    while (log.children.length > 60) { log.removeChild(log.firstChild); }
    log.scrollTop = log.scrollHeight;
  }

  function renderChips(suggestions) {
    var host = $("aiChips");
    if (!host) { return; }
    host.textContent = "";
    (suggestions || []).forEach(function (s) {
      var b = document.createElement("button");
      b.type = "button";
      b.className = "sug";
      b.textContent = String(s.label || s.message || "");
      b.addEventListener("click", function () { ask(String(s.message || s.label || "")); });
      host.appendChild(b);
    });
  }

  function loadBriefing() {
    return getJSON("/api/briefing").then(function (b) {
      var log = $("aiLog");
      if (!log || !b) { return; }
      var box = document.createElement("div");
      box.className = "msg msg--ai msg--brief";
      var hi = document.createElement("b");
      hi.className = "msg__hi";
      hi.textContent = String(b.greeting || "Welcome back to INFINITY.100 AI.");
      box.appendChild(hi);
      var ul = document.createElement("ul");
      (b.lines || []).forEach(function (line) {
        var li = document.createElement("li");
        li.textContent = String(line);
        ul.appendChild(li);
      });
      box.appendChild(ul);
      var time = document.createElement("span");
      time.className = "msg__time";
      time.textContent = clock(Date.now() / 1000);
      box.appendChild(time);
      log.appendChild(box);
      log.scrollTop = log.scrollHeight;
      renderChips(b.suggestions);
      S.briefed = true;
    }).catch(function (err) {
      say("I am online but the briefing failed to load (" + err.message + ").", "ai");
    });
  }

  function ask(message) {
    var msg = String(message || "").trim();
    if (!msg) { return; }
    say(msg, "me");
    var input = $("aiInput");
    if (input) { input.value = ""; }
    postJSON("/api/assistant", { message: msg }).then(function (reply) {
      if (!reply) { return; }
      if (reply.error) { say(String(reply.error), "ai"); return; }
      say(String(reply.text || "…"), "ai", reply.ts);
      renderChips(reply.suggestions);
      runAction(reply.action);
    }).catch(function (err) {
      say("I could not reach the engine: " + err.message, "ai");
    });
  }

  function runAction(action) {
    var route = ({
      "engine.start": "/api/engine/start",
      "engine.stop": "/api/engine/stop",
      "engine.close_all": "/api/engine/close_all"
    })[String(action || "")];
    if (!route) { return; }
    postJSON(route, {}).then(function (r) {
      if (r && r.message) { say(String(r.message), "ai"); }
      refreshRosterSoon();
    }).catch(function (err) {
      // this is the assistant firing engine.start/stop on the user's behalf. An
      // unhandled rejection here meant the request died silently and the log
      // still read as though the instruction had been carried out.
      say("I could not run that: " + err.message, "ai");
    });
  }

  // ══ 6. controls ═══════════════════════════════════════════════════════ //
  var rosterTimer = null;
  function refreshRosterSoon() {
    if (rosterTimer) { clearTimeout(rosterTimer); }
    rosterTimer = setTimeout(function () { rosterTimer = null; loadRoster(); }, 350);
  }

  function busy(on) {
    S.busy = !!on;
    syncDeployLabel();
    var conn = $("connBtn");
    if (conn) { conn.classList.toggle("is-busy", !!on); }
  }

  function onDeploy() {
    var running = !!(S.snap && S.snap.engine && S.snap.engine.running);
    busy(true);
    if (running) {
      postJSON("/api/engine/stop", {}).then(function (r) {
        say(String((r && r.message) || "Automation stopped."), "ai");
      }).catch(noop).then(function () { busy(false); });
      return;
    }
    var needsSwitch = S.selected && S.activeKey && S.selected !== S.activeKey;

    // Say it at the moment of commitment, in the log, where it stays. Not a
    // confirm() — the account is the user's to risk, and a modal they dismiss
    // once teaches them to dismiss the next one. But they should not be able to
    // say afterwards that nothing told them.
    var exp = ((cardFor(S.selected) || {}).expectancy) || {};
    if (exp.verdict === "losing" || exp.verdict === "cost-heavy") {
      say("Before I start: " + String(exp.note || "") +
          " Run it on a demo account until its own numbers say otherwise.", "ai");
    }

    var step = needsSwitch
      ? postJSON("/api/engine/strategy", { key: S.selected })
      : Promise.resolve({ ok: true });
    step.then(function (r) {
      if (r && r.ok === false) {
        say(String(r.message || "That strategy cannot be switched in right now."), "ai");
        return null;
      }
      if (needsSwitch) { S.activeKey = S.selected; refreshRosterSoon(); }
      return postJSON("/api/engine/start", {});
    }).then(function (r) {
      if (r) { say(String(r.message || "Deployed."), "ai"); }
    }).catch(function (err) {
      say("Deploy failed: " + err.message, "ai");
    }).then(function () { busy(false); });
  }

  function onFlatten() {
    busy(true);
    postJSON("/api/engine/close_all", {}).then(function (r) {
      say(String((r && r.message) || "Flattened."), "ai");
    }).catch(noop).then(function () { busy(false); });
  }

  function onKill() {
    var on = !(S.snap && S.snap.risk && S.snap.risk.kill);
    busy(true);
    postJSON("/api/risk/kill", { on: on }).then(function (r) {
      say(String((r && r.message) || (on ? "Kill switch ON." : "Kill switch OFF.")), "ai");
      cls($("killBtn"), "is-on", on);
    }).catch(noop).then(function () { busy(false); });
  }

  /* The assistant is a full-height right rail only when the layout has room for
     one. Narrower than that it becomes a bottom-right overlay, so it starts
     collapsed — otherwise it would cover the stats column before the user has
     asked it anything. */
  function setDock(collapsed) {
    var dock = document.querySelector(".dock");
    var toggle = $("dockToggle");
    if (!dock) { return; }
    dock.classList.toggle("is-collapsed", !!collapsed);
    if (toggle) {
      toggle.textContent = collapsed ? "Show" : "Hide";
      toggle.setAttribute("aria-expanded", collapsed ? "false" : "true");
    }
  }

  /* The assistant has three layouts, matching the CSS breakpoints:
       rail    (>=1500px) full-height right column — open, it costs nothing
       overlay (980-1499) floats over the bottom-right — closed until asked for
       static  (<980px)   flows inline at the end of the page — open
     Only `overlay` starts collapsed, because only `overlay` covers content. */
  function dockMode() {
    if (!window.matchMedia) { return "rail"; }
    if (window.matchMedia("(min-width:1500px)").matches) { return "rail"; }
    if (window.matchMedia("(max-width:980px)").matches) { return "static"; }
    return "overlay";
  }

  /* Driven off `resize` and the status tick rather than a matchMedia listener,
     because the change event is not reliably delivered under a forced viewport. */
  function applyDockMode() {
    var mode = dockMode();
    if (mode === S.dockMode) { return; }
    S.dockMode = mode;
    setDock(mode === "overlay");
  }

  function onAddStrategy() {
    var note = $("addNote");
    if (!note) { return; }
    note.hidden = !note.hidden;
    if (note.hidden) { return; }
    note.textContent = "";
    var lead = document.createTextNode(
      "Drop a new module in backend/strategies/ with the @register decorator and it "
      + "appears here on restart — the platform does not need redesigning. Tune the "
      + "live ones under ");
    var code = document.createElement("code");
    code.textContent = "config.json → strategies";
    note.appendChild(lead);
    note.appendChild(code);
    note.appendChild(document.createTextNode("."));
  }

  function noop() {}

  // ══ 7. link broker ════════════════════════════════════════════════════ //
  /* The panel someone opens *because* nothing is working, so it leans on
     nothing working: /api/broker answers while disconnected and every field is
     filled from that one reply. The password box is write-only — the server
     never sends a password back — so an empty box means "keep the one you
     have", which is why the payload omits `password` instead of sending "". */
  var CUSTOM_PATH = "__custom__";
  var link = { info: null, connected: null, focus: false, busy: false };

  function linkDialog() { return $("linkDlg"); }

  function linkOpen() {
    var d = linkDialog();
    if (!d) { return; }
    if (!d.open) {
      if (typeof d.showModal === "function") {
        try { d.showModal(); } catch (e) { d.setAttribute("open", ""); }
      } else {
        d.setAttribute("open", "");        // no <dialog> support: still usable
      }
    }
    linkSay("", "");
    linkWarn([]);
    link.focus = true;
    linkLoad();
  }

  function linkClose() {
    var d = linkDialog();
    if (!d) { return; }
    if (d.open && typeof d.close === "function") {
      try { d.close(); } catch (e) { d.removeAttribute("open"); }
    } else {
      d.removeAttribute("open");
    }
    linkForget();
  }

  /* Nothing secret is left sitting in the DOM once the panel is shut. */
  function linkForget() {
    var pw = $("lkPassword");
    if (pw) { pw.value = ""; }
    linkReveal(false);
  }

  function linkReveal(show) {
    var pw = $("lkPassword");
    var btn = $("lkReveal");
    if (pw) { pw.type = show ? "text" : "password"; }
    if (btn) {
      btn.textContent = show ? "Hide" : "Show";
      btn.setAttribute("aria-pressed", show ? "true" : "false");
      btn.setAttribute("aria-label", show ? "Hide password" : "Show password");
    }
  }

  function linkSay(words, kind) {
    var el = $("linkMsg");
    if (!el) { return; }
    el.textContent = String(words || "");
    el.classList.remove("is-bad", "is-good");
    if (kind) { el.classList.add("is-" + kind); }
  }

  function linkWarn(list) {
    var el = $("linkWarns");
    if (!el) { return; }
    el.textContent = "";
    (list || []).forEach(function (line) {
      var li = document.createElement("li");
      li.textContent = String(line);
      el.appendChild(li);
    });
  }

  function linkState(words, kind) {
    var el = $("linkState");
    if (!el) { return; }
    text(el, String(words || ""));
    el.classList.remove("is-live", "is-bad", "is-busy");
    if (kind) { el.classList.add("is-" + kind); }
  }

  function linkBusy(on) {
    link.busy = !!on;
    ["lkSave", "lkSaveOnly", "lkDisconnect"].forEach(function (id) {
      var b = $(id);
      if (b) { b.disabled = !!on; }
    });
  }

  /* One sentence saying where the link actually stands, in the words someone
     who has not read the code would use. `src` is normalised first because the
     same line is drawn from two places: the /api/broker reply and the
     once-a-second status snapshot. */
  function linkWords(src) {
    var info = link.info || {};
    if (info.mt5_available === false) {
      return ["bad", "The MetaTrader5 Python package will not import"
        + (info.mt5_import_error ? " (" + info.mt5_import_error + ")" : "")
        + ", so nothing can connect until that is fixed."];
    }
    if (src.connected) {
      var who = [];
      if (src.login) { who.push("account " + src.login); }
      if (src.server) { who.push("on " + src.server); }
      if (src.symbol) { who.push("trading " + src.symbol); }
      return ["live", "Connected" + (who.length ? " — " + who.join(", ") : "") + "."];
    }
    if (src.connecting) {
      return ["busy", "Connecting" + (src.stage ? " — " + src.stage : "") + "…"];
    }
    if (src.error) {
      return ["bad", "Not connected: " + src.error
        + (src.stage ? " (stage: " + src.stage + ")" : "")];
    }
    if (src.blocked && src.block_reason) { return ["bad", String(src.block_reason)]; }
    if (src.offline) { return ["", "Offline. Press SAVE & CONNECT when ready."]; }
    return ["", "Not linked yet. Fill this in and press SAVE & CONNECT."];
  }

  function srcFromInfo(info) {
    var a = info.account || {};
    var live = info.live_account || {};
    return {
      connected: !!info.connected, connecting: !!info.connecting,
      offline: !!info.offline, blocked: !!info.blocked,
      block_reason: info.block_reason || "",
      stage: info.stage || "", error: info.error || "",
      symbol: info.symbol || "",
      login: num(live.login) || num(a.login),
      server: String(live.server || a.server || "")
    };
  }

  function srcFromSnap(snap) {
    var a = snap.account || {};
    var was = link.info || {};
    return {
      connected: !!snap.connected, connecting: !!snap.connecting,
      offline: !snap.connected && !!was.offline, blocked: !!snap.blocked,
      block_reason: String(snap.notice || was.block_reason || ""),
      stage: String(snap.mt5_stage || ""), error: String(snap.mt5_error || ""),
      symbol: String((snap.symbol || {}).name || ""),
      login: num(a.login), server: String(a.server || "")
    };
  }

  function linkLoad() {
    return getJSON("/api/broker").then(linkFill).catch(function (err) {
      linkState("Could not read the link state: " + err.message, "bad");
    });
  }

  /* Also runs on the reply to a save, so it must not overwrite a box the user
     is still typing in. `keepFields` is for a rejected save: the boxes still
     hold what was typed, and replacing them with the saved values would throw
     away the very thing the message is asking the user to correct. */
  function linkFill(info, keepFields) {
    if (!info || typeof info !== "object" || !info.account) { return; }
    link.info = info;
    link.connected = !!info.connected;
    var a = info.account;

    if (!keepFields) {
      linkTerminals(info, a);
      var login = $("lkLogin");
      if (login && document.activeElement !== login) {
        login.value = num(a.login) ? String(num(a.login)) : "";
      }
      var server = $("lkServer");
      if (server && document.activeElement !== server) {
        server.value = String(a.server || "");
      }
      var real = $("lkReal");
      if (real) { real.checked = !!a.allow_live_real; }
    }
    linkPassword(a);

    var off = $("lkDisconnect");
    if (off) { off.hidden = !info.connected; }
    var save = $("lkSave");
    if (save) {
      save.textContent = info.connected ? "SAVE & RECONNECT" : "SAVE & CONNECT";
    }

    var pair = linkWords(srcFromInfo(info));
    linkState(pair[1], pair[0]);
    linkFocus(a);
  }

  /* showModal() lands focus on the close button, which is not where anyone
     wants to start. Aim at the first box that still needs something. */
  function linkFocus(a) {
    if (!link.focus) { return; }
    link.focus = false;
    var target = !num(a.login) ? $("lkLogin")
      : (!a.password_set ? $("lkPassword") : $("lkSave"));
    if (target && typeof target.focus === "function") {
      try { target.focus(); } catch (e) { /* nothing worth doing about it */ }
    }
  }

  function linkPassword(a) {
    var pw = $("lkPassword");
    var help = $("lkPasswordHelp");
    if (pw && !pw.value) {
      pw.placeholder = a.password_set
        ? "Saved — leave blank to keep it" : "Your MT5 password";
    }
    if (!help) { return; }
    var env = String(a.password_env_name || "INFINITY_MT5_PASSWORD");
    if (a.password_from_env) {
      text(help, "Coming from the " + env + " environment variable, and that one "
        + "wins. Anything typed here is saved but not used until you clear it.");
    } else if (a.password_set) {
      text(help, "A password is already saved. Leave this blank to keep it, or "
        + "type a new one to replace it.");
    } else {
      text(help, "Stored in config.json on this computer. Set " + env + " in the "
        + "environment instead if you would rather it never touch a file.");
    }
  }

  /* MetaTrader 5 records its own install directory and every trade server it
     has connected to; the backend reads those, so this list is discovered, not
     guessed. A saved path discovery missed is still offered. */
  function linkTerminals(info, a) {
    var sel = $("lkTerminal");
    if (!sel) { return; }
    var saved = String(a.terminal_path || "");
    var seen = Object.create(null);
    var opts = [];
    // Terminal discovery walks the MetaQuotes data dirs, which also contain
    // WhatsApp transfer folders — they carry a servers list but no terminal64.exe,
    // so they report exists=false. They stay in the list (a missing terminal is
    // information) but sink below the ones that can actually be driven, so the
    // first entry someone sees is a real installation.
    (info.terminals || []).slice().sort(function (x, y) {
      return (y && y.exists ? 1 : 0) - (x && x.exists ? 1 : 0);
    }).forEach(function (t) {
      if (!t || !t.path || seen[t.path]) { return; }
      seen[t.path] = true;
      opts.push({ value: String(t.path), label: linkTermLabel(t) });
    });
    if (saved && !seen[saved]) {
      seen[saved] = true;
      opts.push({ value: saved, label: saved + "  —  from config.json" });
    }
    opts.push({ value: "", label: "Let MetaTrader 5 pick — use a running terminal" });
    opts.push({ value: CUSTOM_PATH, label: "Other… type the path myself" });

    // a path and a label cannot contain "|" on Windows, so this is a
    // faithful signature of the option set and nothing more
    var sig = opts.map(function (o) { return o.value + "|" + o.label; }).join("||");
    if (sel.getAttribute("data-sig") !== sig) {
      var pathBox = $("lkPath");
      var want = (sel.value === CUSTOM_PATH && pathBox && pathBox.value)
        ? CUSTOM_PATH : saved;
      sel.textContent = "";
      opts.forEach(function (o) {
        var el = document.createElement("option");
        el.value = o.value;
        el.textContent = o.label;
        sel.appendChild(el);
      });
      sel.setAttribute("data-sig", sig);
      sel.value = want;
      if (sel.value !== want) { sel.value = ""; }      // the choice went away
    }
    linkPathRow();
  }

  function linkTermLabel(t) {
    var bits = [];
    if (t.servers && t.servers.length) { bits.push(t.servers.join(", ")); }
    if (!t.exists) { bits.push("not found on disk"); }
    return String(t.dir || t.path) + (bits.length ? "  —  " + bits.join(" · ") : "");
  }

  function linkTermByPath(path) {
    var list = (link.info && link.info.terminals) || [];
    for (var i = 0; i < list.length; i += 1) {
      if (list[i] && String(list[i].path) === String(path)) { return list[i]; }
    }
    return null;
  }

  function linkPathRow() {
    var sel = $("lkTerminal");
    if (!sel) { return; }
    var custom = sel.value === CUSTOM_PATH;
    var row = $("lkPathRow");
    if (row) { row.hidden = !custom; }
    var term = custom ? null : linkTermByPath(sel.value);
    linkServers(term);
    linkTermHelp(term, custom, sel.value);
    /* Advanced starts collapsed, so anything in there that needs attention has
       to open it. A typed path or an install MetaTrader recorded and then lost
       is otherwise invisible behind a closed panel, and its help line is being
       written where nobody can read it. */
    var adv = $("lkAdvanced");
    if (adv && (custom || (sel.value && term && !term.exists))) { adv.open = true; }
  }

  /* Server names come from bases\<SERVER NAME>\ inside the terminal's own data
     folder — a name is offered because that terminal really connected to it. */
  function linkServers(term) {
    var all = (link.info && link.info.servers) || [];
    var names = term && term.servers && term.servers.length ? term.servers : all;
    var list = $("lkServers");
    if (list) {
      list.textContent = "";
      names.forEach(function (name) {
        var opt = document.createElement("option");
        opt.value = String(name);
        list.appendChild(opt);
      });
    }
    var box = $("lkServer");
    if (box && !box.value && names.length === 1) { box.value = String(names[0]); }
  }

  function linkTermHelp(term, custom, value) {
    var help = $("lkTerminalHelp");
    if (!help) { return; }
    var servers = (term && term.servers) || [];
    var seen = servers.length
      ? " It has connected to " + (servers.length === 1 ? "one server: "
        : servers.length + " servers: ") + servers.join(", ") + "."
      : "";
    if (custom) {
      text(help, "Full path to terminal64.exe — the MetaTrader 5 program itself, "
        + "not a shortcut to it.");
    } else if (!value) {
      text(help, "INFINITY.100 will attach to a MetaTrader 5 that is already "
        + "running. Open the terminal and log in first.");
    } else if (term && !term.exists) {
      // the install is only a leftover record; say so before anything cheerful
      text(help, "MetaTrader 5 recorded this install but the program is not there "
        + "any more — pick another, or type the real path." + seen);
    } else if (servers.length) {
      text(help, "This install has connected to "
        + (servers.length === 1 ? "one server: " : servers.length + " servers: ")
        + servers.join(", ") + ".");
    } else {
      text(help, "Leave on the default unless you have more than one MetaTrader 5 "
        + "installed.");
    }
    linkServerNote(term);
  }

  /* A terminal knows which servers it has reached before. That list is history,
     not a whitelist — there is no set of approved brokers anywhere in this app,
     and a name outside the list is not rejected. It is still the likeliest
     reason a login is refused when several brokers are installed, so the note
     stays; it just has to read as a spelling check and not as a restriction. */
  function linkServerNote(term) {
    var help = $("lkServerHelp");
    if (!help) { return; }
    var box = $("lkServer");
    var typed = String((box && box.value) || "").trim();
    var servers = (term && term.servers) || [];
    var known = servers.some(function (s) {
      return String(s).toLowerCase() === typed.toLowerCase();
    });
    if (typed && servers.length && !known) {
      text(help, "New server for this terminal — it has not connected to “" + typed
        + "” before. Any broker is accepted, so this is only worth a second look "
        + "at the spelling.");
      cls(help, "is-warn", true);
      return;
    }
    text(help, "Exactly as your broker spells it. Any broker is accepted.");
    cls(help, "is-warn", false);
  }

  function linkPayload(connect) {
    var sel = $("lkTerminal");
    var pw = $("lkPassword");
    var body = {
      login: String((($("lkLogin") || {}).value) || "").trim(),
      server: String((($("lkServer") || {}).value) || "").trim(),
      allow_live_real: !!(($("lkReal") || {}).checked),
      connect: !!connect
    };
    if (sel) {
      body.terminal_path = sel.value === CUSTOM_PATH
        ? String((($("lkPath") || {}).value) || "").trim()
        : String(sel.value || "");
    }
    // omitted rather than "": an empty box means keep whatever is saved
    if (pw && pw.value) { body.password = pw.value; }
    return body;
  }

  function linkSubmit(connect) {
    if (link.busy) { return; }
    linkBusy(true);
    linkSay(connect ? "Saving and connecting…" : "Saving…", "");
    postJSON("/api/broker/link", linkPayload(connect)).then(function (r) {
      r = r || {};
      linkWarn(r.warnings);
      if (!r.ok) {
        linkSay(String(r.message || r.error || "That did not go through."), "bad");
        linkFill(r, true);      // keep what was typed; it is what needs fixing
        linkBlame(String(r.message || r.error || ""));
        return;
      }
      linkForget();          // the secret is on the server now, drop the copy
      linkSay(String(r.message || "Saved."), "good");
      say(String(r.message || "Broker details saved."), "ai");
      linkFill(r);
      refreshRosterSoon();
    }).catch(function (err) {
      linkSay("Could not save: " + err.message, "bad");
    }).then(function () { linkBusy(false); });
  }

  /* The server names the field it would not accept, so put the cursor there
     instead of making the user work out which box the sentence is about. */
  function linkBlame(message) {
    var box = null;
    if (/account number|^login/i.test(message)) {
      box = $("lkLogin");
    } else if (/password/i.test(message)) {
      box = $("lkPassword");
    } else if (/^server|server name/i.test(message)) {
      box = $("lkServer");
    } else if (/terminal/i.test(message)) {
      var sel = $("lkTerminal");
      box = (sel && sel.value === CUSTOM_PATH) ? $("lkPath") : sel;
    }
    if (!box || typeof box.focus !== "function") { return; }
    try {
      box.focus();
      if (typeof box.select === "function" && box.value) { box.select(); }
    } catch (e) { /* a browser that will not focus is not worth failing over */ }
  }

  function linkDisconnect() {
    if (link.busy) { return; }
    linkBusy(true);
    linkSay("Disconnecting…", "");
    postJSON("/api/disconnect", {}).then(function (r) {
      var words = String((r && r.message) || "Disconnected.");
      linkSay(words, "good");
      say(words, "ai");
      return linkLoad();
    }).catch(function (err) {
      linkSay("Disconnect failed: " + err.message, "bad");
    }).then(function () { linkBusy(false); });
  }

  /* Called every snapshot: keeps the prompt above DEPLOY honest, and the open
     panel's status line moving during a connect attempt without polling. */
  function linkSync(snap) {
    var connected = !!snap.connected;
    var bar = $("linkBar");
    if (bar) {
      bar.hidden = connected;
      if (!connected) { text($("linkBarText"), linkBarWords(snap)); }
    }

    /* The bar button, which is never hidden. Its label is the state, so the one
       control the user can always see also answers "am I connected?" without
       them having to decode a wifi glyph. */
    var brk = $("brokerBtn");
    if (brk) {
      brk.classList.toggle("needs-link", !connected);
      brk.classList.toggle("is-linked", connected);
      text($("brokerBtnText"),
           snap.connecting ? "LINKING…" : (connected ? "BROKER" : "LINK BROKER"));
      brk.title = connected
        ? brokerTitle(snap) + " — open the broker link panel to change or disconnect"
        : "MetaTrader 5 is not linked. Click to enter your broker login.";
    }

    var d = linkDialog();
    if (d && d.open) {
      var pair = linkWords(srcFromSnap(snap));
      linkState(pair[1], pair[0]);
      if (link.connected !== null && link.connected !== connected) { linkLoad(); }
    }
    link.connected = connected;
  }

  /* Who we are actually linked to. The account panel carries these numbers too,
     but it is below the fold on a narrow window and the tooltip is not. */
  function brokerTitle(snap) {
    var acct = snap.account || {};
    var login = num(acct.login);
    var server = String(acct.server || "").trim();
    var bits = [];
    if (login > 0) { bits.push("Account " + login); }
    if (server) { bits.push(server); }
    if (acct.trade_mode) { bits.push(String(acct.trade_mode)); }
    return bits.length ? bits.join(" · ") : "Linked to MetaTrader 5";
  }

  function linkBarWords(snap) {
    if (snap.connecting) {
      return "Connecting to MetaTrader 5" + (snap.mt5_stage ? " — " + snap.mt5_stage : "")
        + "…";
    }
    if (snap.mt5_error) {
      return "MetaTrader 5 did not connect: " + String(snap.mt5_error);
    }
    if (snap.blocked && snap.notice) { return String(snap.notice); }
    return "MetaTrader 5 is not linked. Nothing will trade until it is.";
  }

  // ══ 8. wiring ═════════════════════════════════════════════════════════ //
  function wire() {
    var deploy = $("deployBtn");
    if (deploy) { deploy.addEventListener("click", onDeploy); }
    var flat = $("flattenBtn");
    if (flat) { flat.addEventListener("click", onFlatten); }
    var kill = $("killBtn");
    if (kill) { kill.addEventListener("click", onKill); }
    var conn = $("connBtn");
    if (conn) { conn.addEventListener("click", linkOpen); }
    var add = $("addStrategy");
    if (add) { add.addEventListener("click", onAddStrategy); }

    // -- link broker panel ------------------------------------------------ //
    var brokerBtn = $("brokerBtn");
    if (brokerBtn) { brokerBtn.addEventListener("click", linkOpen); }
    var linkBtn = $("linkBtn");
    if (linkBtn) { linkBtn.addEventListener("click", linkOpen); }
    var linkX = $("linkClose");
    if (linkX) { linkX.addEventListener("click", linkClose); }
    var linkForm = $("linkForm");
    if (linkForm) {
      linkForm.addEventListener("submit", function (ev) {
        ev.preventDefault();          // method="dialog" would close it instead
        linkSubmit(true);
      });
    }
    var saveOnly = $("lkSaveOnly");
    if (saveOnly) {
      saveOnly.addEventListener("click", function () { linkSubmit(false); });
    }
    var lkOff = $("lkDisconnect");
    if (lkOff) { lkOff.addEventListener("click", linkDisconnect); }
    var reveal = $("lkReveal");
    if (reveal) {
      reveal.addEventListener("click", function () {
        var pw = $("lkPassword");
        linkReveal(!!pw && pw.type === "password");
      });
    }
    var term = $("lkTerminal");
    if (term) { term.addEventListener("change", linkPathRow); }
    var lkServer = $("lkServer");
    if (lkServer) {
      lkServer.addEventListener("input", function () {
        var sel = $("lkTerminal");
        linkServerNote(sel && sel.value !== CUSTOM_PATH
          ? linkTermByPath(sel.value) : null);
      });
    }
    var dlg = $("linkDlg");
    if (dlg) {
      /* Three ways out of this panel, and all three must drop the typed
         password. The X and the backdrop go through linkClose(), which forgets
         it directly. Esc closes natively, and `close` is the semantically right
         hook — but it is queued on the user interaction task source, so a
         browser that has throttled the page can defer it. keydown is a real
         input event and is not deferred, so Esc clears on the keystroke and the
         `close` listener stays as the backstop for every other route. */
      dlg.addEventListener("keydown", function (ev) {
        if (ev.key === "Escape" || ev.keyCode === 27) { linkForget(); }
      });
      dlg.addEventListener("close", linkForget);
      dlg.addEventListener("click", function (ev) {
        if (ev.target === dlg) { linkClose(); }        // the backdrop
      });
    }

    var form = $("aiForm");
    if (form) {
      form.addEventListener("submit", function (ev) {
        ev.preventDefault();
        var input = $("aiInput");
        ask(input ? input.value : "");
      });
    }

    var toggle = $("dockToggle");
    if (toggle) {
      toggle.addEventListener("click", function () {
        setDock(!document.querySelector(".dock").classList.contains("is-collapsed"));
      });
    }

    document.addEventListener("keydown", function (ev) {
      if (ev.key === "/" && document.activeElement !== $("aiInput")) {
        var input = $("aiInput");
        if (!input) { return; }
        ev.preventDefault();
        // The dock collapses to a header, and .dock.is-collapsed .dock__body is
        // display:none — focus() on a hidden input is a no-op, so "/" swallowed
        // the keystroke and nothing happened. Open the dock first.
        var dock = document.querySelector(".dock");
        if (dock && dock.classList.contains("is-collapsed")) { setDock(false); }
        input.focus();
      }
    });
  }

  function boot() {
    wire();
    applyDockMode();
    window.addEventListener("resize", applyDockMode);
    getJSON("/api/status").then(apply).catch(noop)
      .then(loadRoster)
      .then(loadBriefing)
      .then(openStream);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot);
  } else {
    boot();
  }
}());
