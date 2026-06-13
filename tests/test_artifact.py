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


# ── the tools (create / update / rewrite / list / delete + versioning) ──────────


def _arts(art):
    return art._read_store()["artifacts"]


def test_show_artifact_rejects_unknown_kind(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "gif", "code": "x"})
    assert "Unknown artifact kind" in out
    assert _arts(art) == []  # nothing persisted on rejection


@pytest.mark.parametrize("kind", ["html", "svg", "mermaid", "react"])
def test_show_artifact_creates_a_v1_artifact(monkeypatch, tmp_path, kind):
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": kind, "code": "<x/>", "title": "T"})
    assert "Created" in out
    a = _arts(art)[0]
    assert a["kind"] == kind and a["title"] == "T"
    assert len(a["versions"]) == 1 and a["versions"][0]["code"] == "<x/>"
    assert art._read_store()["current"] == a["id"]


def test_kind_is_normalized(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "  HTML ", "code": "x"})
    assert _arts(art)[0]["kind"] == "html"


def test_update_artifact_appends_a_version_via_string_replace(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<h1>Hello</h1>"})
    out = art.update_artifact.invoke({"old_string": "Hello", "new_string": "World"})
    assert "version 2" in out
    a = _arts(art)[0]
    assert len(a["versions"]) == 2
    assert a["versions"][-1]["code"] == "<h1>World</h1>"
    assert a["versions"][0]["code"] == "<h1>Hello</h1>"  # v1 preserved (no clobber)


def test_update_requires_exactly_one_match(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>x</p><p>x</p>"})
    out = art.update_artifact.invoke({"old_string": "x", "new_string": "y"})
    assert "matches 2 times" in out
    assert len(_arts(art)[0]["versions"]) == 1  # not applied
    miss = art.update_artifact.invoke({"old_string": "zzz", "new_string": "y"})
    assert "not found" in miss
    assert len(_arts(art)[0]["versions"]) == 1


def test_update_with_no_artifact_is_a_clean_message(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert "No artifact" in art.update_artifact.invoke(
        {"old_string": "a", "new_string": "b"}
    )


def test_rewrite_replaces_whole_source_keeps_kind(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "svg", "code": "<svg>1</svg>", "title": "old"})
    out = art.rewrite_artifact.invoke({"code": "<svg>2</svg>", "title": "new"})
    assert "version 2" in out
    a = _arts(art)[0]
    assert a["kind"] == "svg" and a["title"] == "new"
    assert a["versions"][-1]["code"] == "<svg>2</svg>"


def test_update_targets_by_id_and_touches_to_front(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "first"})
    first_id = _arts(art)[0]["id"]
    art.show_artifact.invoke({"kind": "html", "code": "second"})  # now front
    art.update_artifact.invoke(
        {"old_string": "first", "new_string": "FIRST", "artifact_id": first_id}
    )
    arts = _arts(art)
    assert arts[0]["id"] == first_id  # edited artifact moved to front
    assert arts[0]["versions"][-1]["code"] == "FIRST"


def test_list_artifacts_summarizes(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    assert "No artifacts yet" in art.list_artifacts.invoke({})
    art.show_artifact.invoke({"kind": "mermaid", "code": "graph", "title": "Flow"})
    out = art.list_artifacts.invoke({})
    assert "Flow" in out and "[mermaid]" in out and "current" in out


def test_delete_artifact_removes_and_repoints_current(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "a"})
    keep = _arts(art)[0]["id"]
    art.show_artifact.invoke({"kind": "html", "code": "b"})
    drop = _arts(art)[0]["id"]
    out = art.delete_artifact.invoke({"artifact_id": drop})
    assert "Deleted" in out
    store = art._read_store()
    assert [a["id"] for a in store["artifacts"]] == [keep]
    assert store["current"] == keep  # current re-pointed off the deleted one
    assert "No artifact" in art.delete_artifact.invoke({"artifact_id": "nope"})


def test_versions_rotate_to_max(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_VERSIONS", "3")
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "v0"})
    for i in range(1, 5):
        art.rewrite_artifact.invoke({"code": f"v{i}"})
    versions = _arts(art)[0]["versions"]
    assert (
        len(versions) == 3 and versions[-1]["code"] == "v4"
    )  # oldest trimmed, newest kept


def test_artifacts_rotate_to_max(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "3")
    art = _load(monkeypatch, tmp_path)
    for i in range(5):
        art.show_artifact.invoke({"kind": "svg", "code": f"<n>{i}</n>"})
    arts = _arts(art)
    assert len(arts) == 3 and arts[0]["versions"][0]["code"] == "<n>4</n>"


def test_oversize_artifact_is_rejected_not_persisted(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_MAX_CODE_KB", "1")
    art = _load(monkeypatch, tmp_path)
    out = art.show_artifact.invoke({"kind": "html", "code": "x" * 2048})
    assert "too large" in out.lower()
    assert _arts(art) == []


def test_state_survives_a_reload_same_dir(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art.show_artifact.invoke({"kind": "html", "code": "<p>kept</p>"})
    art.update_artifact.invoke({"old_string": "kept", "new_string": "edited"})
    art2 = _load(monkeypatch, tmp_path)  # fresh module, same ARTIFACT_DIR
    a = art2._read_store()["artifacts"][0]
    assert len(a["versions"]) == 2 and a["versions"][-1]["code"] == "<p>edited</p>"


def test_legacy_flat_history_migrates_to_versioned(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    # a pre-0.6 file: {"items": [flat artifacts]}
    art._store_path().write_text(
        '{"items": [{"id": "old1", "kind": "svg", "code": "<x/>", "title": "Legacy", "ts": 5}]}',
        encoding="utf-8",
    )
    store = art._read_store()
    a = store["artifacts"][0]
    assert a["id"] == "old1" and a["title"] == "Legacy"
    assert len(a["versions"]) == 1 and a["versions"][0]["code"] == "<x/>"
    assert store["current"] == "old1"


def test_bad_history_env_does_not_crash_load(monkeypatch, tmp_path):
    monkeypatch.setenv("ARTIFACT_HISTORY", "not-a-number")
    art = _load(monkeypatch, tmp_path)  # must not raise at import
    assert art._MAX_HISTORY == 20  # fell back to the default


def test_corrupt_store_file_reads_as_empty(monkeypatch, tmp_path):
    art = _load(monkeypatch, tmp_path)
    art._store_path().write_text("{not json", encoding="utf-8")
    assert art._read_store() == {
        "artifacts": [],
        "current": None,
    }  # tolerated, not a 500


def test_instance_scoping_isolates_state(monkeypatch, tmp_path):
    # _store_path() reads PROTOAGENT_INSTANCE live, so a scoped instance routes
    # to its own subdir — no module reload needed.
    art = _load(monkeypatch, tmp_path)  # host (no instance)
    art.show_artifact.invoke({"kind": "svg", "code": "host"})
    assert _arts(art)[0]["versions"][0]["code"] == "host"
    monkeypatch.setenv("PROTOAGENT_INSTANCE", "roxy")
    assert "roxy" in str(art._store_path())
    assert _arts(art) == []  # the roxy instance has its own (empty) state


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
    assert c.get("/api/plugins/artifact/history").json() == {
        "artifacts": [],
        "current": None,
    }
    assert c.get("/api/plugins/artifact/current").json()["version"] == 0
    art.show_artifact.invoke({"kind": "svg", "code": "<x/>", "title": "T"})
    art.update_artifact.invoke({"old_string": "<x/>", "new_string": "<y/>"})
    cur = c.get("/api/plugins/artifact/current").json()
    assert (
        cur["code"] == "<y/>" and cur["version"] == 2
    )  # latest version of the focused artifact
    hist = c.get("/api/plugins/artifact/history").json()
    assert len(hist["artifacts"]) == 1 and len(hist["artifacts"][0]["versions"]) == 2
    assert hist["current"] == hist["artifacts"][0]["id"]


def test_delete_route_removes_the_artifact(monkeypatch, tmp_path):
    from fastapi.testclient import TestClient

    art = _load(monkeypatch, tmp_path)
    c = TestClient(_app(art))
    art.show_artifact.invoke({"kind": "html", "code": "x"})
    aid = art._read_store()["artifacts"][0]["id"]
    r = c.delete(f"/api/plugins/artifact/artifact/{aid}")
    assert r.status_code == 200 and r.json()["deleted"] == aid
    assert art._read_store()["artifacts"] == []
    assert c.delete("/api/plugins/artifact/artifact/nope").status_code == 404


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
