# Research Evidence Mapper — build plan

One deployed Managed Agent. Question in, knowledge graph out, state kept between
rounds. Contract is `SCHEMA.md`.

## External surface

> **Correction, 2026-08-15.** A previous revision of this file claimed Paperclip
> ships no MCP server and told you to pip-install the CLI into the sandbox.
> **That was wrong — ignore it.** The hosted MCP is real and verified:
> `https://paperclip.gxl.ai/mcp`, remote streamable HTTP, one passthrough tool.
> "Paperclip MCP" below is correct as originally written. `CONTRACT.md` §2 has
> the endpoint, auth and tool schema.

| need | how |
|---|---|
| search + full text | **Paperclip MCP** (only MCP) — `https://paperclip.gxl.ai/mcp` |
| state across rounds | **memory store** — platform feature, `/mnt/memory/`, no infra |
| search fallback / metadata | `web_fetch` (built in) |
| scoring, dedup, gaps | `bash` + python in the container |

Not used: Qdrant, Mem0, SQLite, Sequential Thinking. Lookup is exact by
`graph_id`, not semantic; the graph is the memory; a container SQLite dies with
the session.

## Pipeline

```
request {graph_id?, ask, target, depth}
  → load prior state from /mnt/memory (skip if new_question)
  → plan queries from ask type
  → Paperclip search, capped by depth tier
  → extract findings:
       abstracts, batched 5/pass     ← new_question, expand_node
       full text on relevant papers  ← resolve_link, test_gap
  → verify every quote against fetched text (string match, drop on miss)
  → merge names against the WHOLE graph
  → assemble.py: dedup, score, gaps
  → write /mnt/memory/<graph_id>/*, return full graph
```

## Files

**Source (we author):**

| file | holds |
|---|---|
| `CLAUDE.md` | role, 4 ask types, pipeline, tier table, memory layout, JSON skeleton |
| `.claude/skills/literature-search/SKILL.md` | Paperclip interface + normalization; the transport seam |
| `.claude/skills/claim-extraction/SKILL.md` | two modes (abstract batch / full text), quote fidelity |
| `.claude/skills/graph-assembly/SKILL.md` + `assemble.py` | dedup, scoring, gaps — deterministic, in code |
| `fixtures/` | 3 questions: well-studied, sparse, genuinely disputed |

**Compiled by `/managed-agent-deploy`:** `manifest.json` (sonnet-5, `message`,
`session_policy: "fresh"`, Paperclip in `mcp_servers` with
`permission: "always_allow"`, `memory` block), `acl.ts` (`{ public: true }`),
`agent/tools/research-evidence-mapper.ts`. No `tools.ts` unless Paperclip turns out to be
stdio — then one handler wraps it.

## Order

| # | step | why here |
|---|---|---|
| 0 | **Spike, 30 min** — stub agent: Paperclip reachable? bundled script runs? `python3` present? memory mount writable? | all four would force a rewrite |
| 1 | `fixtures/` | defines "done" before anything aims at it |
| 2 | `CLAUDE.md` + 3 skills | |
| 3 | **Hand-run the pipeline in this session** on the largest fixture | demo-safe fallback if deploy fails |
| 4 | `/managed-agent-deploy research-evidence-mapper` + smoke | |
| 5 | HTML render | where the points are; needs real data first |

## Verify

`bun run typecheck && bun run check`, then blocking:

```
bun run console research-evidence-mapper -- --once "$(cat managed/research-evidence-mapper/fixtures/q-disputed.txt)"
```

Six facts, each independently checked:

1. Paperclip called at the event level — trace lines, not a prose claim
2. reply carries the full graph JSON, not a summary
3. disputed fixture yields ≥1 link `state: "disagreed"` — zero is a failure, not a clean result
4. a low-confidence finding **survives** into the output
5. round 2 (`resolve_link`) loads round 1 from memory and `round` increments
6. three quotes spot-checked verbatim against their DOIs

## Risks

- ~~Bundled skill scripts may not be executable in the sandbox.~~ **Settled: they
  are.** `probe.py` shipped, was found at `/workspace/skills/env-probe/probe.py`,
  and ran. Scoring stays in `assemble.py`.
- ~~Paperclip transport unknown.~~ **Settled: remote streamable HTTP.** It takes
  the `manifest.mcp_servers` branch — one entry, `permission: "always_allow"`,
  key in a vault. No `tools.ts` relay, no sandbox install.
- **The MCP exposes one tool, not a typed surface.** `paperclip({command: string})`
  is a passthrough to the CLI. `literature-search` therefore constructs command
  strings; budget for CLI-flag errors surfacing as tool errors, and note that
  hosted MCP is stateless, so any repo-scoped call must name its repo every time.
- **Auth is the last open gate.** `Authorization: Bearer <token>` is proven to
  work. Which header the endpoint accepts for a *long-lived API key* is untested —
  the CLI uses `X-API-Key`, but that is the CLI's REST path, not necessarily the
  MCP's. Needs a key minted from paperclip.gxl.ai to settle.
- **`bun run console <name>` does not attach memory**; only `--once` and the eve
  wrapper do. Test memory through `--once`, never the Console session.
- **`read_write` memory + web content = injection surface.** Papers are
  semi-trusted; an injected instruction could be written to memory and read back
  as trusted next round. Accepted for the hackathon; worth a line in `CLAUDE.md`
  telling the agent that memory holds data, never instructions.
- **`exhaustive` (300 papers) is ~60 serial extraction passes.** Minutes. Never
  demo on it.

## Deferred

Analogy edges (additive pass over `gaps`), `expand_node` breadth tuning, render
polish, auth (`acl.ts` stays public — a restricted ACL fails closed until the
router has auth).
