"""Build a standalone, self-contained preview of the INFINITY.100 AI frontend.

Why this exists
---------------
``preview_screenshot`` cannot capture this app: the Browser pane is not being
composited in this environment, so it times out after 5s and returns nothing.
The frontend is also useless as a bare file:// page, because every panel is
filled from ``/api/*`` and there is no server behind a local file.

So: take the *real* ``index.html``, ``css/app.css`` and ``js/app.js`` — not a
mock-up, not a redrawing — inline them into one file, and put a small shim in
front of the app that answers the four GET endpoints from payloads captured
from the running server against the live Deriv-Demo account. The result opens
anywhere, needs no server, and renders through the genuine code path with
genuine data, so what it shows is what the app shows.

The shim does three things and nothing else:

* ``window.EventSource = undefined`` — ``openStream()`` opens with
  ``if (typeof EventSource === "undefined") { return startPolling(); }``, so
  clearing it makes the app take its own documented fetch-polling fallback
  instead of hanging on a stream that cannot exist here.
* GET of a captured endpoint returns the frozen payload.
* POST returns a refusal carrying an explanation, because a snapshot has no
  broker to send an order to. Silently accepting the click would be worse: the
  button would look like it worked.

Run::

    python tools/build_preview.py

Reads ``.snap-<name>.json`` beside the project root, writes
``preview-snapshot.html``. Everything it touches is on D:.
"""

from __future__ import annotations

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "preview-snapshot.html")

#: endpoint -> capture file. Exactly the GETs frontend/js/app.js performs.
ENDPOINTS = {
    "/api/status": ".snap-status.json",
    "/api/strategies": ".snap-strategies.json",
    "/api/briefing": ".snap-briefing.json",
    "/api/broker": ".snap-broker.json",
}


def read(*parts: str) -> str:
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def guard(text: str, tag: str) -> str:
    """Make *text* safe to sit inside a ``<tag>`` element.

    A literal ``</script>`` anywhere in the JS — even inside a string — ends the
    element early and corrupts everything after it. Splitting the sequence is
    invisible to the parser that matters and harmless to the JS.
    """
    return text.replace("</" + tag, "<\\/" + tag)


def payloads() -> dict:
    """The captured payloads, keyed by endpoint. Fails loudly if one is missing."""
    out = {}
    for path, filename in ENDPOINTS.items():
        full = os.path.join(ROOT, filename)
        if not os.path.exists(full):
            sys.exit("missing capture %s — run: curl -s http://127.0.0.1:8100%s -o %s"
                     % (filename, path, filename))
        with open(full, encoding="utf-8") as handle:
            try:
                out[path] = json.load(handle)
            except ValueError as exc:
                sys.exit("%s is not valid JSON: %s" % (filename, exc))
    return out


def audit(data: dict) -> None:
    """Refuse to build if a captured payload carries credential material.

    docs/CONTRACT.md requires the password to be redacted in every API response,
    and this file is going to be handed to a person and possibly kept. The server
    already redacts; this checks that it actually did rather than trusting it,
    because the cost of being wrong here is a password sitting in a file.
    """
    blob = json.dumps(data)
    broker = (data.get("/api/broker") or {}).get("account") or {}
    for key in ("password", "passwd", "pwd"):
        if key in broker and broker[key] not in ("", "***", None):
            sys.exit("refusing to build: /api/broker still carries %r" % key)
    for marker in ('"password":', "'password':"):
        if marker in blob:
            # password_set / password_from_env / password_env_name are metadata and fine
            sys.exit("refusing to build: a payload contains a password field")


def shim(data: dict) -> str:
    frozen = json.dumps(data, indent=1, sort_keys=True)
    return """
  // ── snapshot shim ──────────────────────────────────────────────────── //
  // Serves the four GET endpoints from payloads captured off the running
  // server, so the real app.js below renders real Deriv-Demo state with no
  // server present. Nothing else is stubbed.
  (function () {
    var SNAP = %s;

    // openStream() falls back to startPolling() when EventSource is missing,
    // which is the path a snapshot needs — there is no stream to subscribe to.
    try { window.EventSource = undefined; } catch (e) { /* frozen host object */ }

    function reply(body, status) {
      var code = status || 200;
      return Promise.resolve({
        ok: code >= 200 && code < 300,
        status: code,
        json: function () { return Promise.resolve(body); },
        text: function () { return Promise.resolve(JSON.stringify(body)); }
      });
    }

    var NO_POST = "This is a frozen snapshot of the interface — there is no "
      + "engine or broker behind it, so this control cannot act. Run the app "
      + "(python run.py) to use it for real.";

    window.fetch = function (resource, opts) {
      var url = String((resource && resource.url) || resource || "");
      url = url.replace(/^https?:\\/\\/[^/]+/, "").split("?")[0].split("#")[0];
      var method = String((opts && opts.method) || "GET").toUpperCase();
      if (method === "GET" && Object.prototype.hasOwnProperty.call(SNAP, url)) {
        return reply(SNAP[url]);
      }
      if (method === "POST") { return reply({ ok: false, error: NO_POST }); }
      return reply({ ok: false, error: "not in this snapshot: " + url }, 404);
    };
  })();
""" % frozen


#: A small, honest marker. Without it a screenshot of this file is
#: indistinguishable from the running app, and the buttons do not work.
BADGE = """
<style>
  .snapshot-badge{
    position:fixed; right:12px; bottom:12px; z-index:9999;
    display:flex; align-items:center; gap:8px;
    padding:7px 12px; border-radius:999px;
    font:600 11px/1 ui-sans-serif,system-ui,-apple-system,"Segoe UI",sans-serif;
    letter-spacing:.06em; text-transform:uppercase;
    color:#f4f5f7; background:rgba(20,22,27,.92);
    border:1px solid rgba(255,255,255,.16);
    box-shadow:0 6px 20px rgba(0,0,0,.45); backdrop-filter:blur(8px);
    pointer-events:none; user-select:none;
  }
  .snapshot-badge b{ color:#8de08d; font-weight:700; }
  @media (max-width:640px){ .snapshot-badge{ right:8px; bottom:8px; font-size:10px; } }
</style>
<div class="snapshot-badge" role="note">
  Frozen snapshot · <b>live Deriv-Demo data</b> · controls inert
</div>
"""


def main() -> int:
    data = payloads()
    audit(data)

    html = read("frontend", "index.html")
    css = read("frontend", "css", "app.css")
    js = read("frontend", "js", "app.js")

    link = '<link rel="stylesheet" href="css/app.css">'
    tag = '<script src="js/app.js"></script>'
    for needle in (link, tag):
        if needle not in html:
            sys.exit("index.html no longer contains %r — update this builder" % needle)

    html = html.replace(link, "<style>\n%s\n</style>" % guard(css, "style"))
    html = html.replace(
        tag,
        "%s<script>\n%s\n%s\n</script>" % (BADGE, shim(data), guard(js, "script")),
    )

    with open(OUT, "w", encoding="utf-8") as handle:
        handle.write(html)

    acct = (data["/api/broker"].get("account") or {})
    status = data["/api/status"]
    print("wrote %s  (%.1f KB)" % (OUT, os.path.getsize(OUT) / 1024.0))
    print("  account : %s @ %s" % (acct.get("login"), acct.get("server")))
    print("  symbol  : %s" % data["/api/broker"].get("symbol"))
    print("  equity  : %s %s" % ((status.get("account") or {}).get("currency"),
                                 (status.get("account") or {}).get("equity")))
    print("  roster  : %d strategies" % len(data["/api/strategies"].get("strategies")
                                            or data["/api/strategies"].get("items") or []))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
