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

- **Tool** `show_artifact(kind, code, title)` — `kind` ∈ `html` · `svg` · `mermaid` · `react`.
- **View** "Artifact" (right rail) — a sandboxed renderer with a **history picker** (revisit the last
  `ARTIFACT_HISTORY` renders, default 20) and a **download** button (saves the artifact's source).
- **Event** `artifact.created` (ADR 0039) — broadcast on the bus when the agent renders, so the
  console lights the Artifact rail icon even when the panel is closed.
- **Skill** `rendering-artifacts` — nudges the agent to render (vs writing files) for "show me…".

## Security

Generated artifacts are untrusted (prompt injection) and run sandboxed — see protoAgent's
[security & trust model](https://github.com/protoLabsAI/protoAgent/blob/main/docs/explanation/security-and-trust.md).

---
Built for [protoAgent](https://github.com/protoLabsAI/protoAgent).
