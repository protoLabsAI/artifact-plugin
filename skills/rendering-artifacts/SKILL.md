---
name: rendering-artifacts
description: When the user wants to SEE, render, visualize, preview, or "show me" a chart, diagram, mock-up, table, or interactive widget — render it with the show_artifact tool instead of writing files to disk.
---

# Rendering artifacts (generative UI)

The console has an **Artifact panel** powered by the `show_artifact` tool. Use it whenever the
user wants to **look at something rendered** rather than get source files.

## When to use `show_artifact` (NOT the filesystem)

- "show me…", "render…", "visualize…", "draw…", "make a chart/diagram/flowchart of…",
  "build a little widget/demo to see…", "preview…"
- → call `show_artifact(kind, code)`; it renders sandboxed and the user sees it immediately.

Reach for `show_artifact` **before** writing files. Writing `.jsx`/`.html` to the workspace gives
the user files to wire up themselves — not what they asked for when they want to *see* it.

## Kinds

- `mermaid` — flowcharts, sequence/ER/gantt diagrams. `code` is the Mermaid definition.
- `html` — a full or partial HTML document (with inline `<style>`/`<script>` as needed).
- `svg` — inline SVG markup (icons, simple charts).
- `react` — a self-contained component script that renders into `#root`; React, ReactDOM, and
  Babel are provided. Write the component **and** the `ReactDOM.createRoot(...).render(...)` call.

## Editing an artifact (don't re-create it)

When the user asks to change something you already rendered, **iterate the same artifact** — don't
call `show_artifact` again (that makes a near-duplicate and clutters the panel). Use:

- **`update_artifact(old_string, new_string)`** — a targeted edit. `old_string` must appear in the
  current source **exactly once** (copy it verbatim, whitespace included; add surrounding context
  to make it unique). This is the fast path — prefer it for small changes. Creates a new version.
- **`rewrite_artifact(code, title?)`** — replace the whole source. Use for large changes where a
  targeted edit would be awkward. Creates a new version; the kind is kept.

Each edit is a **version** the user can step back through in the panel, so iterate freely — you're
never destroying the previous version. Both default to the most-recent artifact; pass
`artifact_id` to target another.

## Managing artifacts

- **`list_artifacts()`** — see the ids/kinds/titles/version counts (to target an edit or delete).
- **`delete_artifact(artifact_id)`** — remove one for cleanup. (The user can also delete from the
  panel's trash button.)

## Interactive artifacts (calling back to you)

`html` and `react` artifacts can call **`window.protoArtifact.ask(prompt)`** — it returns a
Promise resolving to *your* answer — so an artifact can be a live mini-app (a game NPC, a tutor,
a generator). Use it when the user asks for something that needs intelligence *inside* the widget:

```js
const line = await window.protoArtifact.ask("Greet the player as a grumpy dwarf, one line.");
```

It only works if the operator set `ARTIFACT_ASK_ENABLED` — if it's off, `ask()` rejects with a
message telling them how to enable it, so write artifacts that degrade gracefully.

## When to still write files

Only when the user explicitly wants a **project / files** ("scaffold a repo", "write the component
to a file", "create a Vite app"). For "show me a counter widget" → `show_artifact("react", …)`.
