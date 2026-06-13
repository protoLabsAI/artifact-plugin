"""Tests for the artifact plugin — the tool, the history store, the route split,
and the plugin-view contract (the regression guard for the /api-vs-/plugins mount
bug). Run with: pytest (needs fastapi + langchain_core, the host's deps)."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load(monkeypatch, tmp_path):
    """Fresh module bound to a temp ARTIFACT_DIR so history is isolated per test."""
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    monkeypatch.delenv("PROTOAGENT_INSTANCE", raising=False)
    spec = importlib.util.spec_from_file_location(
        "artifact_under_test", ROOT / "__init__.py"
    )
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


# ── the tool ──────────────────────────────────────────────────────────────────


def test_show_artifact_rejects_unknown_kind(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "gif", "code": "x"})
    assert "Unknown artifact kind" in out
    assert art._read_history() == []  # nothing persisted on rejection


@pytest.mark.parametrize("kind", ["html", "svg", "mermaid", "react"])
def test_show_artifact_accepts_each_kind_and_persists(monkeypatch, tmp_path, kind):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": kind, "code": "<x/>", "title": "T"})
    assert "Rendered" in out
    items = art._read_history()
    assert len(items) == 1
    assert (
        items[0]["kind"] == kind
        and items[0]["title"] == "T"
        and items[0]["code"] == "<x/>"
    )


def test_kind_is_normalized(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "  HTML ", "code": "x"})
    assert art._read_history()[0]["kind"] == "html"


def test_history_prepends_newest_and_rotates_to_max(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "3")
    art = _load(monkeypatch, tmp_path)
    for i in range(5):
        art.show_artifact.invoke({"kind": "svg", "code": f"<n>{i}</n>"})
    items = art._read_history()
    assert len(items) == 3  # capped
    assert items[0]["code"] == "<n>4</n>"  # newest first
    assert items[-1]["code"] == "<n>2</n>"  # oldest two evicted


def test_history_survives_a_reload_same_dir(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>kept</p>"})
    art2 = _load(monkeypatch, tmp_path)  # fresh module, same ARTIFACT_DIR
    assert art2._read_history()[0]["code"] == "<p>kept</p>"


def test_oversize_artifact_is_rejected_not_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_CODE_KB", "1")
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "html", "code": "x" * 2048})
    assert "too large" in out.lower()
    assert art._read_history() == []


def test_bad_history_env_does_not_crash_load(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "not-a-number")
    art = _load(monkeypatch, tmp_path)  # must not raise at import
    assert art._MAX_HISTORY == 20  # fell back to the default


def test_corrupt_history_file_reads_as_empty(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._history_path().write_text("{not json", encoding="utf-8")
    assert art._read_history() == []  # tolerated, not a 500


def test_instance_scoping_isolates_history(monkeypatch, tmp_path):
    # _history_path() reads PROTOAGENT_INSTANCE live, so a scoped instance routes
    # to its own subdir — no module reload needed.
    art = _load(monkeypatch, tmp_path)  # host (no instance)
    art.show_artifact.invoke({"kind": "svg", "code": "host"})
    assert art._read_history()[0]["code"] == "host"
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    assert "roxy" in str(art._history_path())
    assert art._read_history() == []  # the roxy instance has its own (empty) history


# ── the routes (the split + gating contract) ───────────────────────────────────


def _app(art):
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(art._build_view_router(), prefix="/plugins/artifact")
    app.include_router(art._build_data_router(), prefix="/api/plugins/artifact")
    return app


def test_view_page_served_on_the_PUBLIC_prefix(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    # The PAGE is public /plugins/artifact/view (iframe-loadable, base-derivation safe)…
    assert c.get("/plugins/artifact/view").status_code == 200
    # …and is NOT under /api (where the base would resolve to "/api" and break the kit).
    assert c.get("/api/plugins/artifact/view").status_code == 404


def test_data_routes_on_the_gated_prefix(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    assert c.get("/api/plugins/artifact/current").json() == {
        "kind": "",
        "code": "",
        "title": "",
        "ts": 0,
    }
    assert c.get("/api/plugins/artifact/history").json() == {"items": []}
    art.show_artifact.invoke({"kind": "svg", "code": "<x/>", "title": "T"})
    assert c.get("/api/plugins/artifact/current").json()["code"] == "<x/>"
    assert len(c.get("/api/plugins/artifact/history").json()["items"]) == 1


def test_manifest_view_path_matches_the_served_public_route(monkeypatch, tmp_path):
    import yaml

    m = yaml.safe_load((ROOT / "protoagent.plugin.yaml").read_text())
    path = m["views"][0]["path"]
    assert path == "/plugins/artifact/view"  # public, NOT /api/plugins/…
    # And the base a view derives from this path is empty (host) — the bug guard.
    assert path.split("/plugins/")[0] == ""


# ── the shell page: four-rules / kit contract ──────────────────────────────────


def test_shell_page_is_four_rules_compliant(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    html = art._SHELL_HTML
    # rule 4 — the same-origin DS kit, base-prefixed by hand (loads before the kit).
    assert "/_ds/plugin-kit.css" in html
    assert "/_ds/plugin-kit.js" in html
    assert 'location.pathname.split("/plugins/")[0]' in html
    # ESM — dynamic import, never a classic <script src> (protoContent#224).
    assert 'import(window.__base + "/_ds/plugin-kit.js")' in html
    assert 'type="module"' in html
    # rules 2+3 — gated data via the kit's slug-aware authed fetch.
    assert 'apiFetch("/api/plugins/artifact/history")' in html
    # nested artifact frame stays sandboxed with NO same-origin (the isolation model).
    assert 'sandbox="allow-scripts"' in html
    assert "allow-same-origin" not in html
    # The kit owns theming: no hand-rolled :root theme map, no bespoke handshake
    # listener (hex survives ONLY as `var(--pl-color-…, #fallback)` defaults).
    assert ":root{" not in html and ":root {" not in html
    assert 'addEventListener("message"' not in html


def test_libs_are_vendored_same_origin_not_cdn(monkeypatch, tmp_path):
    """react/mermaid load from the same-origin vendor route — NO cdnjs (so artifacts
    work offline), every lib still SRI-pinned (sha512 of the vendored bytes)."""
    html = _load(monkeypatch, tmp_path)._SHELL_HTML
    assert "cdnjs.cloudflare.com" not in html  # no external CDN dependency
    assert "/plugins/artifact/vendor/" in html  # served same-origin
    # all four libs present, each with an integrity hash.
    for lib in (
        "mermaid.min.js",
        "react.production.min.js",
        "react-dom.production.min.js",
        "babel.min.js",
    ):
        assert lib in html
    assert html.count("sha512-") == 4 and 'integrity="' in html
    # crossorigin is REQUIRED even same-origin: the sandbox is an opaque origin, so
    # the lib load is cross-origin and SRI needs the CORS fetch to validate.
    assert 'crossorigin="anonymous"' in html


def test_vendored_files_exist_and_match_the_allowlist(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    vendor = ROOT / "vendor"
    for name in art._VENDOR_FILES:
        assert (vendor / name).exists(), f"vendor/{name} missing"
    # no stray files served that aren't on disk, no disk files unlisted.
    on_disk = {p.name for p in vendor.glob("*.js")}
    assert on_disk == art._VENDOR_FILES


def test_vendor_route_serves_js_and_blocks_traversal(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    r = c.get("/plugins/artifact/vendor/react.production.min.js")
    assert r.status_code == 200
    assert "javascript" in r.headers["content-type"]
    assert "immutable" in r.headers.get("cache-control", "")
    assert (
        r.headers.get("access-control-allow-origin") == "*"
    )  # SRI from the opaque sandbox
    # allowlist: an unlisted name / traversal attempt is a clean 404, not a file read.
    assert c.get("/plugins/artifact/vendor/secrets.env").status_code == 404
    assert c.get("/plugins/artifact/vendor/..%2f__init__.py").status_code == 404
