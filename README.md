# research-evidence-mapper

A deployed **Claude Managed Agent** that turns one question about the scientific
literature into a machine-readable knowledge graph, and grows that graph across
rounds.

It reads real papers through Paperclip, extracts every claim as a **verbatim
quote** from text it actually fetched, and returns the whole graph as JSON —
entities, evidence, scored relationships, and the places where the literature
has a hole in it.

```jsonc
// in
{ "target": "do antioxidant supplements accelerate cancer metastasis?",
  "depth": "standard", "years": 5 }

// out — the full graph, always
{ "things": [...], "papers": [...], "findings": [...],
  "links": [ { "state": "disagreed",
               "why": "conditions differ: {braf v600e-driven melanoma, mouse}
                                      vs {b16f0 melanoma, gulo ko mice}" } ],
  "gaps": [...], "coverage": {...}, "delta": {...} }
```

That `why` is the point of the system. Two labs published opposing in vivo
results on vitamin C and melanoma metastasis; the graph did not average them or
pick a winner — it identified that one camp used **Gulo-knockout mice**, which
cannot synthesise vitamin C, and that this is the boundary the disagreement sits
on.

## Start here

**[`managed/research-evidence-mapper/README.md`](./managed/research-evidence-mapper/README.md)**
is the agent's own documentation: what it guarantees and how each guarantee is
enforced, the full input/output schema, what it needs, how to deploy and call
it, and its known limits.

| file | what it is |
|---|---|
| [`CLAUDE.md`](./managed/research-evidence-mapper/CLAUDE.md) | the deployed system prompt, uploaded verbatim |
| [`SCHEMA.md`](./managed/research-evidence-mapper/SCHEMA.md) | the authoritative data contract |
| [`CONTRACT.md`](./managed/research-evidence-mapper/CONTRACT.md) | deliverables, MCP details, `assemble.py` spec |
| [`BUILD.md`](./managed/research-evidence-mapper/BUILD.md) | build plan and its six blocking acceptance facts |
| [`.claude/skills/`](./managed/research-evidence-mapper/.claude/skills) | three skills, uploaded to the Skills API unchanged |
| [`fixtures/`](./managed/research-evidence-mapper/fixtures) | three corpus-validated questions, each grading something specific |
| [`runs/g_e087`](./managed/research-evidence-mapper/runs) | a real two-round graph from the deployed agent |

## What makes it trustworthy

Every one of these is a mechanism, not an intention.

- **A finding without a verbatim quote does not exist.** Quotes are string-matched
  against the fetched text; a mismatch is dropped and counted, never repaired.
- **Assembly is deterministic.** `assemble.py` is stdlib-only and byte-identical
  across runs and hash seeds. Nothing is scored by hand.
- **A dead corpus never reads as "no evidence".** A canary runs before any real
  search; an unreachable Paperclip returns `status: "failed"` with an `error`
  prefixed `PAPERCLIP UNAVAILABLE`.
- **Nothing is filtered by score.** Low-confidence findings survive on purpose —
  they are the speculative leads worth exploring.
- **Absence is reported as structure.** A pair nobody linked becomes a `gap`; a
  gap that has been *searched for* carries `searched_in_round`, which is a much
  stronger statement than one nobody looked at.

## Quickstart

Requires [Bun](https://bun.sh) and an `ANTHROPIC_API_KEY`.

```bash
bun install
cp .env.example .env          # add ANTHROPIC_API_KEY

bun run typecheck && bun run check
bun run deploy research-evidence-mapper
bun run console research-evidence-mapper -- --once "$(cat managed/research-evidence-mapper/fixtures/q-disputed.txt)"
```

Check the deterministic assembler on its own, no API key needed:

```bash
python3 managed/research-evidence-mapper/.claude/skills/graph-assembly/assemble.py --selftest
```

## Provenance

Extracted from the LABrador workspace, where this node sits in a larger pipeline
(hypothesis → evidence → tractability → recruitment → economics). Only this node
and the shared runtime it needs are included here; the other nodes belong to
their own authors.

MIT — see [LICENSE](./LICENSE).
