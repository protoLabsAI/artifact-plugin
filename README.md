# artifact-plugin

A **protoAgent plugin** that gives the agent generative UI on demand. The agent calls
`show_artifact(kind, code)` to render **HTML / SVG / Mermaid / React** into the console's Artifact
panel — rendered in a **sandboxed iframe** (`sandbox="allow-scripts"`, no same-origin), the same
isolation model as Claude Artifacts / Open WebUI. Generated code runs, but can't touch the console.

It's also the **reference external plugin**: pure Python + a self-served iframe page + a bundled
skill — no host build, no federation. Installable from this git URL.

## Install

In the protoAgent console: **Plugins → Download → install from a git URL**, or in config:

```yaml
plugins:
  enabled: [artifact]
```

then install `https://github.com/protoLabsAI/artifact-plugin` (ADR 0027). Restart to mount its
console view.

## What it adds

- **Tools** — an artifact is a **version chain** (the Claude "update vs rewrite" model), so editing
  iterates the same artifact instead of flooding the panel with near-duplicates:
  - `show_artifact(kind, code, title)` — **create** (`kind` ∈ `html` · `svg` · `mermaid` · `react`).
  - `update_artifact(old_string, new_string, artifact_id?)` — **targeted edit** (string-replace,
    must match once) → new version. The fast path for small changes.
  - `rewrite_artifact(code, title?, artifact_id?)` — **full replace** → new version.
  - `list_artifacts()` / `delete_artifact(artifact_id)` — manage them.
- **View** "Artifact" (right rail) — a sandboxed renderer with an **artifact picker**, **version
  navigation** (step back/forward through edits), an **in-panel code editor** (edit the source and
  *Run & save* → a new `user` version, never overwriting the agent's), **download** (this version),
  and **delete**.
- **Events** `artifact.created` / `artifact.updated` / `artifact.deleted` (ADR 0039) — broadcast on
  the bus so the console lights the Artifact rail icon even when the panel is closed.
- **Skill** `rendering-artifacts` — teaches render-don't-write-files and the edit-don't-recreate
  workflow.

## Configuration

The operator-facing knobs are **Settings ▸ Plugins ▸ Artifact** fields (no restart) — and an
environment variable of the same knob overrides the UI for headless / ACP setups. Precedence:
**env > Settings ▸ Plugins > default**.

| Setting (Settings ▸ Plugins) | Env override | Default | What |
|---|---|---|---|
| **Interactive artifacts** | `ARTIFACT_ASK_ENABLED` | _off_ | Let artifacts call back to the agent via `window.protoArtifact.ask()` (below). |
| **Ask system instruction** | `ARTIFACT_ASK_SYSTEM` | _(none)_ | Optional system prompt wrapping every `ask()`. |

Numeric tuning caps are config-or-env only (no UI field — the settings schema has no `int` type):

| Knob | Env | Default | What |
|---|---|---|---|
| history | `ARTIFACT_HISTORY` | `20` | How many artifacts to keep (oldest evicted). |
| max_versions | `ARTIFACT_MAX_VERSIONS` | `50` | Max versions kept per artifact (oldest edits trimmed). |
| max_code_kb | `ARTIFACT_MAX_CODE_KB` | `512` | Max source size per version (a larger render is rejected). |
| ask_max_chars | `ARTIFACT_ASK_MAX_CHARS` | `4000` | Max prompt length for an `ask()`. |
| — | `ARTIFACT_DIR` | `~/.protoagent/artifact` | Where state is stored (instance-scoped by `PROTOAGENT_INSTANCE`). |

## Interactive artifacts (calling back to the agent)

Every artifact gets a **`window.protoArtifact.ask(prompt)`** helper — the
[`window.claude.complete`](https://claude.com/blog/claude-powered-artifacts) analog. It returns a
Promise that resolves to the agent's answer, so an artifact can be a mini-app — an AI game NPC, a
tutor, a content generator:

```js
const reply = await window.protoArtifact.ask("Give the NPC a gruff one-line greeting.");
```

It's **opt-in** — flip **Interactive artifacts** on in **Settings ▸ Plugins ▸ Artifact** (or set
`ARTIFACT_ASK_ENABLED=1`); letting sandboxed artifact code trigger LLM calls is a cost surface.
Under the hood the sandboxed artifact `postMessage`s the shell, which calls the
**bearer-gated** `POST /api/plugins/artifact/ask` → a *bare* completion via the host SDK
(`graph.sdk.complete`, protoAgent ≥ the build that ships it). When disabled or unsupported, `ask()`
rejects with a clear message. The artifact sandbox stays opaque-origin throughout — the bridge is
the only channel out.

## Routes

The shell **page** is public at `/plugins/artifact/view` (an iframe page-load can't carry a
bearer, and the page derives its slug base from `/plugins/…`); its **data/action** routes
(`/current`, `/history`, `PUT`/`DELETE` `/artifact/{id}`, `POST /ask`) are gated under
`/api/plugins/artifact`. Page chrome is the protoLabs design-system kit
(`/_ds/plugin-kit.{css,js}`), so the panel follows the operator's live theme.

## Security

Generated artifacts are untrusted (prompt injection) and run **sandboxed** — a nested
`<iframe sandbox="allow-scripts">` with **no** `allow-same-origin`, so the code runs but can't
reach the console, its cookies, or its APIs (the Claude Artifacts / Open WebUI model). See
protoAgent's
[security & trust model](https://github.com/protoLabsAI/protoAgent/blob/main/docs/explanation/security-and-trust.md).

> **Offline / no network.** The `react` and `mermaid` libraries (React, ReactDOM, Babel, Mermaid)
> are **vendored** under `vendor/` and served same-origin from `/plugins/artifact/vendor/…`, so every
> artifact kind renders **fully offline** — no `cdnjs`, no outbound network at all
> (`capabilities.network: []` is literally true). The scripts are still pinned with **Subresource
> Integrity** (`integrity` + `crossorigin="anonymous"` — required because the sandbox is an opaque
> origin, so the load is cross-origin); a tampered served file won't execute. To bump a lib, replace
> the file in `vendor/`, recompute its `sha512` SRI, and update the `LIB` map in the shell page.

## Development

```bash
pip install -r requirements-dev.txt
pytest            # the suite
ruff check . && ruff format --check .
```

CI (`.github/workflows/ci.yml`) runs the same on every PR.

---
Built for [protoAgent](https://github.com/protoLabsAI/protoAgent).
