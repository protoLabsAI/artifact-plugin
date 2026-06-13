"""Artifact plugin (ADR 0038) — generative UI on demand.

The agent calls ``show_artifact(kind, code)`` to render HTML / SVG / Mermaid / React into the
console's Artifact panel. The panel is a plugin-served shell page (iframed by the console, ADR
0026) that renders the agent's generated code in a **nested sandboxed iframe**
(``sandbox="allow-scripts"``, no same-origin) — the same isolation model as Claude Artifacts and
Open WebUI: generated code runs, but can't touch the console, its cookies, or its APIs.

The current artifact is persisted to a **file** (instance-scoped), not module memory — under the
ACP runtime the tool executes in the operator-MCP process while the route is served by the main
process, so the two only share state through disk.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

from langchain_core.tools import tool

log = logging.getLogger("protoagent.plugins.artifact")

_KINDS = {"html", "svg", "mermaid", "react"}


# Keep the last N artifacts (ARTIFACT_HISTORY overrides) so the user can revisit + download past
# renders, not just the latest. A bad env value must not crash plugin load — fall back to 20.
def _env_int(name: str, default: int, minimum: int = 1) -> int:
    try:
        return max(minimum, int(os.environ.get(name, "") or default))
    except (TypeError, ValueError):
        log.warning("[artifact] %s is not an integer — using %d", name, default)
        return default


_MAX_HISTORY = _env_int("ARTIFACT_HISTORY", 20)

# Cap a single artifact's source so one runaway render can't bloat history.json (the
# whole history is read+rewritten on every render). ~512 KB is generous for hand- or
# model-written HTML/SVG/React; override with ARTIFACT_MAX_CODE_KB.
_MAX_CODE_BYTES = _env_int("ARTIFACT_MAX_CODE_KB", 512) * 1024


def _history_path() -> Path:
    base = Path(
        os.environ.get("ARTIFACT_DIR") or (Path.home() / ".protoagent" / "artifact")
    )
    inst = os.environ.get("PROTOAGENT_INSTANCE", "").strip()
    if inst:
        base = base / inst
    base.mkdir(parents=True, exist_ok=True)
    return base / "history.json"


def _read_history() -> list[dict]:
    try:
        data = json.loads(_history_path().read_text(encoding="utf-8"))
        items = data.get("items") if isinstance(data, dict) else data
        return items if isinstance(items, list) else []
    except (FileNotFoundError, ValueError):
        return []


def _write_history(items: list[dict]) -> None:
    path = _history_path()
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"items": items}, fh)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


# Set at register() so the tool can broadcast on the bus (ADR 0039). Under the default runtime the
# tool runs in the server process where the bus is wired; the dot lights from artifact.created.
_REGISTRY = None


def _emit(event: str, data: dict) -> None:
    try:
        if _REGISTRY is not None:
            _REGISTRY.emit(event, data)  # → "artifact.<event>" (namespace-guarded)
    except Exception:  # noqa: BLE001 — emitting must never break the tool
        log.debug("[artifact] emit(%s) failed", event, exc_info=True)


@tool
def show_artifact(kind: str, code: str, title: str = "") -> str:
    """Render a generative-UI artifact into the console's Artifact panel.

    ``kind`` is one of: "html" (a full or partial HTML document), "svg" (inline SVG markup),
    "mermaid" (a Mermaid diagram definition), or "react" (a self-contained React component script
    that renders into ``#root``; React, ReactDOM and Babel are provided). ``code`` is the source;
    ``title`` is an optional label. The artifact runs sandboxed — it cannot access the console.
    Use this to SHOW the user a chart, diagram, mock-up, or interactive widget you generate —
    prefer it over writing files when the user just wants to see something rendered.
    """
    k = (kind or "").strip().lower()
    if k not in _KINDS:
        return (
            f"Unknown artifact kind {kind!r}. Use one of: {', '.join(sorted(_KINDS))}."
        )
    code = code or ""
    if len(code.encode("utf-8")) > _MAX_CODE_BYTES:
        return (
            f"Artifact too large ({len(code.encode('utf-8')) // 1024} KB > "
            f"{_MAX_CODE_BYTES // 1024} KB). Trim the source or split it; raise "
            f"ARTIFACT_MAX_CODE_KB if you really need more."
        )
    ts = int(time.time() * 1000)
    item = {"id": str(ts), "kind": k, "code": code, "title": title or "", "ts": ts}
    items = [item] + _read_history()
    _write_history(items[:_MAX_HISTORY])
    # Broadcast so the console lights the Artifact rail icon even when the panel is closed.
    _emit("created", {"id": item["id"], "kind": k, "title": title or ""})
    return f"Rendered a {k} artifact ({len(code)} chars) to the Artifact panel."


def _build_view_router():
    """The shell PAGE — served under the PUBLIC ``/plugins/artifact`` prefix
    (plugin-view rule 2): a browser iframe page-load can't carry an Authorization
    bearer, so a gated page 401-blanks under the token gate. The page is also where
    the slug-aware base is derived (``location.pathname.split("/plugins/")[0]``), so
    it MUST live under ``/plugins/`` — a ``/api/plugins/`` page poisons the base to
    ``/api`` and the kit's ``/_ds/`` assets 404 (the bug this split fixes). The page
    fetches its DATA from the gated data router with the handshake token."""
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/view")
    async def _view():
        return HTMLResponse(_SHELL_HTML)

    return router


def _build_data_router():
    """The DATA routes — mounted under ``/api/plugins/artifact`` so they inherit the
    operator bearer gate (plugin-view rule 2). Read-only history of rendered
    artifacts, fetched from the shell page with the handshake token."""
    from fastapi import APIRouter

    router = APIRouter()

    @router.get("/current")
    async def _current_artifact() -> dict:
        items = _read_history()
        return items[0] if items else {"kind": "", "code": "", "title": "", "ts": 0}

    @router.get("/history")
    async def _history() -> dict:
        return {"items": _read_history()}

    return router


def register(registry) -> None:
    global _REGISTRY
    _REGISTRY = registry
    registry.register_tool(show_artifact)
    registry.register_skill_dir(
        "skills"
    )  # teaches: render with show_artifact, don't write files
    # TWO routers at DISTINCT prefixes (a same-prefix second router is silently
    # de-duped by the host): the PAGE on public /plugins/artifact (iframe-loadable,
    # base-derivation-safe) and the DATA routes on gated /api/plugins/artifact.
    registry.register_router(_build_view_router(), prefix="/plugins/artifact")
    registry.register_router(_build_data_router(), prefix="/api/plugins/artifact")


# The shell page (ADR 0026 iframe). It takes the operator bearer via the console's postMessage
# handshake, polls /current, and renders each new artifact into a NESTED sandboxed iframe. The
# nested frame is sandbox="allow-scripts" with NO allow-same-origin — generated code is isolated.
_SHELL_HTML = r"""<!doctype html><html><head><meta charset="utf-8">
<script>
  // Slug-aware base (protoAgent ADR 0042, plugin-view rule 3) computed FIRST — the
  // kit's own <link> loads before the kit exists, so it's base-prefixed by hand.
  window.__base = location.pathname.split("/plugins/")[0];
  document.write('<link rel="stylesheet" href="' + window.__base + '/_ds/plugin-kit.css">');
</script>
<style>
  /* Layout only — colors/typography come from plugin-kit.css's --pl-* tokens, which
     plugin-kit.js re-skins to the operator's live theme (dark fallbacks, no flash). */
  html,body{margin:0;height:100%;background:var(--pl-color-bg,#0a0a0c);color:var(--pl-color-fg-muted,#9aa0aa);
    font-family:var(--pl-font-sans,ui-sans-serif,system-ui,sans-serif)}
  #wrap{display:flex;flex-direction:column;height:100%}
  /* Toolbar: history picker + download. Hidden until there's at least one artifact. */
  #bar{display:none;align-items:center;gap:8px;padding:6px 10px;
    border-bottom:var(--pl-border-width,1px) solid var(--pl-color-border,#2a2a30);font-size:12px}
  #bar select{flex:1;min-width:0}
  #stage{flex:1;min-height:0;position:relative}
  #empty{display:flex;align-items:center;justify-content:center;height:100%;text-align:center;padding:24px;font-size:14px}
  /* No white flash — the artifact frame defaults to the console's ground (ADR 0038). */
  #frame{border:0;width:100%;height:100%;display:none;background:var(--pl-color-bg,#0a0a0c)}
</style></head><body>
<div id="wrap">
  <div id="bar"><select id="hist" class="pl-input"></select><button id="dl" class="pl-btn pl-btn--sm" type="button" title="Download this artifact">Download</button></div>
  <div id="stage">
    <div id="empty">No artifact yet. Ask the agent to render one — a chart, diagram, or widget.</div>
    <iframe id="frame" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
  </div>
</div>
<script type="module">
  // The DS plugin-kit owns the protoagent:init handshake (bearer + theme, incl. live
  // re-themes onto the --pl-* tokens) and slug-aware authed fetches — replacing the
  // hand-rolled listener/theme map this page carried. plugin-kit.js is an ES MODULE,
  // so it loads via dynamic import (a classic <script src> throws on its exports;
  // see protoAgent docs/how-to/build-a-plugin-view.md). Older host without /_ds:
  // fall back to a tokenless same-origin shim.
  let kit;
  try { kit = await import(window.__base + "/_ds/plugin-kit.js"); }
  catch (e) { kit = { initPluginView(){}, apiFetch: (p, i) => fetch(window.__base + p, i) }; }
  var items = [], selId = null, followNewest = true, lastRenderedId = null;
  var EXT = { html: "html", svg: "svg", mermaid: "mmd", react: "jsx" };
  function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  // The NESTED artifact iframe (sandboxed, no stylesheet access) gets the live theme
  // injected as literal colors — read the kit-managed tokens at render time.
  function base(){
    var cs = getComputedStyle(document.documentElement);
    var bg = (cs.getPropertyValue("--pl-color-bg") || "#0a0a0c").trim();
    var fg = (cs.getPropertyValue("--pl-color-fg") || "#ededed").trim();
    return '<style>html,body{margin:0;background:' + bg + ';color:' + fg + '}</style>';
  }
  function srcdoc(kind, code) {
    if (kind === "html") return base() + code;
    if (kind === "svg") return '<!doctype html>' + base() + '<body style="display:grid;place-items:center;min-height:100vh">' + code + '</body>';
    if (kind === "mermaid") return '<!doctype html>' + base() + '<body><pre class="mermaid">' + esc(code) + '</pre>' +
      '<script src="https://cdnjs.cloudflare.com/ajax/libs/mermaid/10.9.1/mermaid.min.js"><\/script>' +
      '<script>mermaid.initialize({startOnLoad:false,theme:"dark"});mermaid.run();<\/script></body>';
    if (kind === "react") return '<!doctype html>' + base() + '<body><div id="root"></div>' +
      '<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react/18.3.1/umd/react.production.min.js"><\/script>' +
      '<script crossorigin src="https://cdnjs.cloudflare.com/ajax/libs/react-dom/18.3.1/umd/react-dom.production.min.js"><\/script>' +
      '<script src="https://cdnjs.cloudflare.com/ajax/libs/babel-standalone/7.24.7/babel.min.js"><\/script>' +
      '<script type="text/babel" data-presets="react">' + code + '<\/script></body>';
    return '<!doctype html>' + base() + '<body style="font-family:sans-serif;padding:16px">unsupported artifact kind</body>';
  }
  function current(){ for (var i=0;i<items.length;i++) if (items[i].id===selId) return items[i]; return items[0]||null; }
  function label(it, i){ var t = it.title || (it.kind + " artifact"); return (i===0?"● ":"") + t + "  ·  " + it.kind; }
  function rebuildSelect(){
    var sel = document.getElementById("hist"); sel.innerHTML = "";
    items.forEach(function(it,i){ var o=document.createElement("option"); o.value=it.id; o.textContent=label(it,i); sel.appendChild(o); });
    if (selId) sel.value = selId;
  }
  function render(){
    var it = current();
    document.getElementById("bar").style.display = items.length ? "flex" : "none";
    if (!it || !it.code){ return; }
    document.getElementById("empty").style.display = "none";
    if (it.id !== lastRenderedId){
      lastRenderedId = it.id;
      var f = document.getElementById("frame");
      f.srcdoc = srcdoc(it.kind, it.code); f.style.display = "block";
    }
  }
  document.getElementById("hist").addEventListener("change", function(e){
    selId = e.target.value; followNewest = items.length && selId === items[0].id; render();
  });
  document.getElementById("dl").addEventListener("click", function(){
    var it = current(); if (!it) return;
    var blob = new Blob([it.code], { type: "text/plain" });
    var a = document.createElement("a"); a.href = URL.createObjectURL(blob);
    a.download = "artifact-" + it.id + "." + (EXT[it.kind] || "txt");
    document.body.appendChild(a); a.click(); a.remove(); setTimeout(function(){ URL.revokeObjectURL(a.href); }, 1000);
  });
  async function poll() {
    if (document.hidden) return;  // don't poll while the window is hidden/minimized (desktop perf)
    try {
      var r = await kit.apiFetch("/api/plugins/artifact/history");
      var d = await r.json(); items = (d && d.items) || [];
      if (!items.length) return;
      if (followNewest || !selId) selId = items[0].id;
      rebuildSelect(); render();
    } catch (e) { /* transient */ }
  }
  // Boot ONCE, on whichever fires first: the handshake (the bearer arrives with
  // protoagent:init, so the gated history poll authenticates) or a short timer
  // for the no-handshake case (standalone page / older host).
  var booted = false;
  function boot(){ if (booted) return; booted = true; poll(); setInterval(poll, 1500); }
  kit.initPluginView(boot);
  setTimeout(boot, 800);
  document.addEventListener("visibilitychange", function(){ if(!document.hidden && booted) poll(); }); // refresh on return
</script></body></html>"""
