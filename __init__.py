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
# renders, not just the latest.
_MAX_HISTORY = max(1, int(os.environ.get("ARTIFACT_HISTORY", "20") or "20"))


def _history_path() -> Path:
    base = Path(os.environ.get("ARTIFACT_DIR") or (Path.home() / ".protoagent" / "artifact"))
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
        return f"Unknown artifact kind {kind!r}. Use one of: {', '.join(sorted(_KINDS))}."
    ts = int(time.time() * 1000)
    item = {"id": str(ts), "kind": k, "code": code or "", "title": title or "", "ts": ts}
    items = [item] + _read_history()
    _write_history(items[:_MAX_HISTORY])
    # Broadcast so the console lights the Artifact rail icon even when the panel is closed.
    _emit("created", {"id": item["id"], "kind": k, "title": title or ""})
    return f"Rendered a {k} artifact ({len(code or '')} chars) to the Artifact panel."


def _build_router():
    from fastapi import APIRouter
    from fastapi.responses import HTMLResponse

    router = APIRouter()

    @router.get("/current")
    async def _current_artifact() -> dict:
        items = _read_history()
        return items[0] if items else {"kind": "", "code": "", "title": "", "ts": 0}

    @router.get("/history")
    async def _history() -> dict:
        return {"items": _read_history()}

    @router.get("/view")
    async def _view():
        return HTMLResponse(_SHELL_HTML)

    return router


def register(registry) -> None:
    global _REGISTRY
    _REGISTRY = registry
    registry.register_tool(show_artifact)
    registry.register_skill_dir("skills")  # teaches: render with show_artifact, don't write files
    registry.register_router(_build_router(), prefix="/api/plugins/artifact")


# The shell page (ADR 0026 iframe). It takes the operator bearer via the console's postMessage
# handshake, polls /current, and renders each new artifact into a NESTED sandboxed iframe. The
# nested frame is sandbox="allow-scripts" with NO allow-same-origin — generated code is isolated.
_SHELL_HTML = r"""<!doctype html><html><head><meta charset="utf-8"><style>
  :root{ --bg:#0a0a0c; --fg:#ededed; --fg-muted:#9aa0aa; --border:#2a2a30; }
  html,body{margin:0;height:100%;background:var(--bg);color:var(--fg-muted);
    font-family:ui-sans-serif,system-ui,-apple-system,sans-serif}
  #wrap{display:flex;flex-direction:column;height:100%}
  /* Toolbar: history picker + download. Hidden until there's at least one artifact. */
  #bar{display:none;align-items:center;gap:8px;padding:6px 10px;border-bottom:1px solid var(--border);font-size:12px}
  #bar select{flex:1;min-width:0;background:transparent;color:var(--fg);border:1px solid var(--border);border-radius:6px;padding:3px 6px;font-size:12px}
  #bar button{background:transparent;color:var(--fg-muted);border:1px solid var(--border);border-radius:6px;padding:3px 10px;cursor:pointer;font-size:12px}
  #stage{flex:1;min-height:0;position:relative}
  #empty{display:flex;align-items:center;justify-content:center;height:100%;text-align:center;padding:24px;font-size:14px}
  /* No white flash — the artifact frame defaults to the console's dark ground (ADR 0038). */
  #frame{border:0;width:100%;height:100%;display:none;background:var(--bg)}
</style></head><body>
<div id="wrap">
  <div id="bar"><select id="hist"></select><button id="dl" type="button" title="Download this artifact">Download</button></div>
  <div id="stage">
    <div id="empty">No artifact yet. Ask the agent to render one — a chart, diagram, or widget.</div>
    <iframe id="frame" sandbox="allow-scripts" referrerpolicy="no-referrer"></iframe>
  </div>
</div>
<script>
  var token = null, items = [], selId = null, followNewest = true, lastRenderedId = null;
  // Theme follows the console (ADR 0026 bridge). Dark fallbacks so we never flash white.
  var theme = { bg: "#0a0a0c", fg: "#ededed", fgMuted: "#9aa0aa" };
  var EXT = { html: "html", svg: "svg", mermaid: "mmd", react: "jsx" };
  window.addEventListener("message", function (e) {
    var m = e.data || {}; if (m.type !== "protoagent:init") return;
    token = m.token || null;
    if (m.theme) {
      theme = { bg: m.theme.bg || theme.bg, fg: m.theme.fg || theme.fg, fgMuted: m.theme.fgMuted || theme.fgMuted };
      var r = document.documentElement.style;
      r.setProperty("--bg", theme.bg); r.setProperty("--fg", theme.fg); r.setProperty("--fg-muted", theme.fgMuted);
      if (m.theme.border) r.setProperty("--border", m.theme.border);
    }
  });
  function esc(s){ return String(s).replace(/&/g,"&amp;").replace(/</g,"&lt;"); }
  function base(){ return '<style>html,body{margin:0;background:' + theme.bg + ';color:' + theme.fg + '}</style>'; }
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
      var r = await fetch("/api/plugins/artifact/history", { headers: token ? { Authorization: "Bearer " + token } : {} });
      var d = await r.json(); items = (d && d.items) || [];
      if (!items.length) return;
      if (followNewest || !selId) selId = items[0].id;
      rebuildSelect(); render();
    } catch (e) { /* transient */ }
  }
  setInterval(poll, 1500); poll();
  document.addEventListener("visibilitychange", function(){ if(!document.hidden) poll(); }); // refresh on return
</script></body></html>"""
