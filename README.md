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
  "gaps": [...], "coverage": {...}, "delta": {...},
  "interpretability": {...} }        // required — headline, metrics, steps,
                                     // evidence, assumptions, uncertainty,
                                     // limitations, counterfactuals, lineage
```

That `why` is the point of the system. Two labs published opposing in vivo
results on vitamin C and melanoma metastasis; the graph did not average them or
pick a winner — it identified that one camp used **Gulo-knockout mice**, which
cannot synthesise vitamin C, and that this is the boundary the disagreement sits
on.

> ### Integrating against this agent? Read [`SCHEMA.md`](./SCHEMA.md).
>
> **[`SCHEMA.md`](./SCHEMA.md) is at the root of this repository** — the same
> directory as this README, not inside `docs/`. It is the authoritative contract:
> the request you send, every field of the graph that comes back, the closed
> vocabularies, and the guarantees. This README summarises it; **where the two
> disagree, `SCHEMA.md` is correct and the README is a bug.**
>
> ```
> research-evidence-mapper/
> ├── SCHEMA.md      <-- the contract. start here to integrate.
> ├── README.md      <-- this file. orientation and how to run it.
> ├── CLAUDE.md          the deployed system prompt
> └── docs/              design record only — not the contract
> ```

**Status: deployed, and its own acceptance test passes.** `agent_015feTqKz3Bmtec2RaWaE2sW` **v12** runs
on the Claude Developer Platform with three skills, a memory store and the
Paperclip MCP attached. All four ask types have executed against it, and all six
of [`docs/BUILD.md`](./docs/BUILD.md)'s blocking facts are verified — including
the one it calls out as *"zero is a failure, not a clean result"*.

## What it actually does

Given *"can a small-molecule IRAK4 inhibitor suppress synovial fibroblast-driven
inflammation in rheumatoid arthritis, or is its effect confined to the myeloid
compartment?"* it returned a graph in which:

- round 1 (`new_question`, `standard`) found no direct evidence and emitted the
  missing relationship as **gap `g3`**;
- round 2 (`test_gap` on `g3`, `deep`) **found the paper round 1 missed** and
  promoted the gap into link `L3`, state `disagreed`, with the boundary
  condition spelled out: the drug works on TLR-driven fibroblast inflammation,
  fails on IL-1β-driven cytokines, but does block IL-1β-driven MMP.

That round-1 miss is the point, not an embarrassment. A shallow sweep produced a
confident absence; a deeper targeted round overturned it. The graph records
both, and `changed_in_round` marks what moved.

The distinguishing behaviours:

- **Every claim is a verbatim quote**, string-matched against the fetched text by
  code. A quote that does not match is dropped and counted in
  `coverage.no_quote_discarded`. It is never repaired.
- **Nothing is filtered by score.** Hedged and single-source findings stay in.
  The caller sets its own threshold.
- **Absence is reported as structure.** A pair nobody linked becomes a `gap`; a
  gap that has been *searched for* carries `searched_in_round`, which is a much
  stronger statement than one nobody looked at.
- **Disagreement gets explained, not averaged.** When two camps conflict,
  `explain_disagreement` compares their experimental conditions and reports
  `conditions differ: {A} vs {B}` — because different conditions is the common
  case, not real contradiction.
- **Coverage is honest.** `found` / `read` / `used` / `truncated` /
  `stop_reason` describe what actually happened. Only `complete` means the
  literature was exhausted.

---

## Input / output schema

Full contract in [`SCHEMA.md`](./SCHEMA.md), at the repository root. Summary:

### Input — the task string is one JSON object

```jsonc
{
  "graph_id": "g_7f2a",    // omit for new_question
  "ask": "resolve_link",   // new_question | expand_node | resolve_link | test_gap
  "target": "L2",          // an id from that graph; the QUESTION for new_question
  "depth": "deep",         // quick | standard | deep | exhaustive
  "years": 5,              // only papers from the last 5 years. omit = no limit
  "reason": "..."          // logged, never acted on
}
```

**Only `target` is required.** `{"target": "does X affect Y"}` is a valid
request and returns a real graph. Everything else defaults rather than refusing:

| omitted or unusable | default |
|---|---|
| `ask` | `new_question` |
| `depth`, or not one of the four tiers | `standard` |
| `years`, or not a positive number | unbounded |
| `reason` | `null` |
| `graph_id` | treated as `new_question` |
| not JSON at all | the whole text becomes the question |

Every substitution appears in `coverage.defaults_applied`, so a caller who
wanted `deep` can see they got `standard`. **The one hard failure is the
question** — a missing `target`, or one naming an id that does not exist,
returns `status: "failed"` rather than a guess.

| ask | target | does |
|---|---|---|
| `new_question` | free text | mints a new `graph_id` at round 1 |
| `expand_node` | a `things` id | what else connects to this node |
| `resolve_link` | a `links` id | more evidence on one relationship, biased to the thin side |
| `test_gap` | a `gaps` id | has anyone actually stated this? sets `searched_in_round` either way |

| depth | papers | queries | extraction |
|---|---|---|---|
| `quick` | 10 | 2 | abstracts only |
| `standard` | 25 | 4 | abstracts, full text on the top few |
| `deep` | 50 | 6 | full text on every paper that yields a finding |
| `exhaustive` | 300 | 12 | full text throughout — minutes, not a demo tier |

`quick` may never report "no evidence" — ten papers is page one, and page one lies.

**`years` is a publication-year floor.** It is applied as `--year-min`, and
`coverage.years` records the window. Two honest limits: a windowed absence is a
much weaker claim than an unbounded one — *"nothing in 5 years"* is not
*"nothing"* — and not every source accepts the flag, so `coverage` states which
sources the window actually reached.

### Output — always the full graph, never a delta

```jsonc
{
  "schema_version": "1.1", "graph_id": "g_7f2a", "question": "...",
  "round": 2, "status": "ok", "generated_at": "...", "error": null,
  "rounds":   [ { "n": 1, "ask": "...", "outcome": "new_evidence" } ],
  "coverage": { "depth": "deep", "found": 412, "read": 43, "used": 40,
                "truncated": true, "no_quote_discarded": 6,
                "quotes_unverified": 0, "queries": [ { "q": "..." } ],
                "stop_reason": "max_papers" },
  "things":   [ { "id": "t1", "name": "...", "kind": "protein", "aliases": [] } ],
  "papers":   [ { "id": "p2", "doi": "...", "first_author": "...",
                  "study_type": "test_tube", "is_preprint": false } ],
  "findings": [ { "id": "f2", "from": "t2", "how": "binds", "to": "t3",
                  "says": "yes", "quote": "<verbatim>", "paper": "p2",
                  "quote_verified": true,   // true | null; null NEVER means yes
                  "where": "<conditions>", "is_own_result": true } ],
  "links":    [ { "id": "L2", "from": "t2", "how": "binds", "to": "t3",
                  "state": "disagreed", "why": "conditions differ: ...",
                  "confidence": { "overall": 0.42, "agreement": 0.5,
                                  "evidence_quality": 0.4, "independence": 0.0 } } ],
  "gaps":     [ { "id": "g1", "missing": ["t1","t3"], "implied_by": ["L1","L2"],
                  "confidence": 0.34, "searched_in_round": null } ],
  "interpretability": { "schema_version": "1.0.0", "headline": {...},
                        "metrics": [...], "steps": [...], "evidence": [...],
                        "assumptions": [...], "uncertainty": {...},
                        "limitations": [...], "counterfactuals": [...],
                        "lineage": [...], "extensions": {...} }
}
```

**`delta` — what this round changed.** Derived from the graph, never stored
separately:

```jsonc
"delta": { "round": 2, "things_added": ["t34"], "papers_added": ["p9"],
           "findings_added": ["f40","f41"], "links_added": ["L36"],
           "links_changed": ["L12"], "gaps_added": ["g7"], "gaps_resolved": ["g3"] }
```

The reply is still the **full graph**, deliberately: one parser, no reassembly,
and a consumer that misses a round is not left holding a graph it cannot
complete. `delta` just saves every consumer recomputing the same diff. It is not
written to a separate file — a delta file is a second source of truth that can
disagree with the graph.

`links_changed` is scores **moving**, which is correct behaviour rather than
drift: a link at 0.81 dropping to 0.44 when a contradicting paper arrives is the
system working.

`confidence.overall = 0.4·agreement + 0.4·evidence_quality + 0.2·independence`,
computed in code and recomputable from `findings` + `papers`. **Scores are valid
for one `(graph_id, round)` pair only** — a link at 0.81 can correctly drop to
0.44 when a contradicting paper arrives. Never cache one across rounds.

**Failure is still a graph.** There is no error blob, ever — one parser handles
every reply. `status` is `ok` | `empty` | `partial` | `failed`; on anything but
`ok` the lists are empty or short, `coverage` still reports real numbers, and
`error` carries a one-line cause. A domain abstention — an extending ask against
a graph that is not in memory — carries a full `interpretability` block too, with
`headline.status: "FAILED"` and a `GRAPH_NOT_FOUND` limitation. Only an
infrastructure failure that produces no reply at all falls outside the contract,
and that is the orchestrator's to report.

**`interpretability` — the shared LABrador block.** Required on every reply, and
enforced by [`schema/graph.schema.json`](./schema/graph.schema.json): a graph
without it does not validate, and `{"graph_id": "g"}` alone no longer satisfies
the output contract. It lets a UI answer six questions without reading the graph
— what was concluded, why, what backs each result, how each number was computed,
what uncertainty remains, and what would falsify it.

```jsonc
"interpretability": {
  "headline":    { "result": "EVIDENCE_MAPPED_DISPUTED", "status": "QUALIFIED",
                   "basis": ["OBSERVED","INFERRED","MODELED"] },
  "metrics":     [ { "id": "metric.papers_used", "value": 2, "unit": "papers",
                     "evidence_ids": [], "assumption_ids": ["assumption.depth"] } ],
  "steps":       [ { "id": "step.confidence.overall",
                     "formula": "0.4*agreement + 0.4*evidence_quality + 0.2*independence" } ],
  "evidence":    [ { "id": "evidence.f6", "source_id": "doi:10.1126/…",
                     "locator": "results", "quote": "<verbatim>", "grade": "MODERATE" } ],
  "uncertainty": { "intervals": [ { "interval_type": "observed_range",
                                    "confidence_level": null } ] },
  "limitations": [ { "code": "UNVERIFIED_QUOTE", "severity": "ERROR",
                     "field_path": "output.findings[].quote_verified" } ],
  "extensions":  { "quote_verified": true, "search": { "queries": [...] },
                   "relationships": [ { "supporting": [], "contradicting": [],
                                        "no_effect": [] } ] }
}
```

It is **derived, never stored** — the same rule `delta` follows, and for the same
reason. [`SCHEMA.md`](./SCHEMA.md) has the full field list, the
`headline.result` vocabulary and every limitation code.

Two contract rules worth knowing before you integrate:

- **A heuristic score is never a probability.** `confidence.overall` is a ranking
  aid over what was read. `HEURISTIC_CONFIDENCE` says so on every single reply,
  and `uncertainty.intervals[].confidence_level` is `null` because there is no
  confidence interval to report.
- **Unknown is `null` plus a limitation, never `0`.** A count that was not
  recorded does not become zero, and an absent year window does not become "all
  years". `queries: []` is legal and raises `QUERIES_NOT_RECORDED`.

---

## What it needs

### MCP servers — exactly one

| server | transport | auth |
|---|---|---|
| **Paperclip** — `https://paperclip.gxl.ai/mcp` | remote streamable HTTP | `Authorization: Bearer <token>` |

Paperclip is a virtual filesystem of full-text biomedical papers, regulatory
documents and clinical trials. The MCP exposes **one passthrough tool**,
`paperclip({ command: string })`, which runs the Paperclip CLI server-side — so
the tool surface is the CLI surface, and calling it means writing a command
string. It is **stateless** (nothing carries between calls) and the sandbox it
runs in **blocks shell loops and `xargs`**, so there is no batching: one command
per call.

**Paperclip is the only source of papers.** No `web_fetch`, no second corpus. If
Paperclip cannot supply something, that is a `coverage` fact to report — not a
licence to go around it, because a graph built partly from elsewhere breaks the
guarantee that every quote is verifiable against a Paperclip document id.

Auth rides in a platform credential vault as a `static_bearer`; the credential
id goes in `manifest.vault_ids`. Creating the vault is still manual, but keeping
it alive is not:

```bash
bun run rotate      # refresh the local Paperclip login, push the token to the vault
```

**Run it immediately before any deployed call.** The stored bearer is a copy of
Paperclip's OAuth `id_token`, which lives about an hour, and when the copy goes
stale the platform exposes **no paperclip tool to the agent at all** — which the
agent then reads as an empty corpus rather than an expired credential. The order
inside the script is the trap it exists to close: refresh *first*, then rotate.
Rotating without refreshing copies an already-expiring token into the vault and
looks like it worked.

Which header the MCP accepts for a **long-lived API key** is still untested;
`Bearer` is proven with a session token. A long-lived key would retire this
script entirely.

### Memory

One memory store, mounted at `/mnt/memory/research-evidence-mapper/`, provisioned by
`bun run deploy`. State survives across sessions and rounds.

```
/mnt/memory/research-evidence-mapper/
  index.json          graph_id -> question, round, updated_at
  g_7f2a/
    meta.json  things.json  papers.json  links.json  gaps.json
    findings/r1.json  findings/r2.json     # appended per round, chunked at 80KB
```

A single memory file caps at 100KB. Ids are stable forever — `t1` stays `t1`,
and gap ids key on the missing pair so `test_gap` targets survive re-ranking.

### Skills — three, uploaded unchanged

| skill | owns |
|---|---|
| `literature-search` | query construction per ask, tier budgets, pagination, the Paperclip seam, normalization, coverage accounting |
| `claim-extraction` | the two extraction modes and quote fidelity |
| `graph-assembly` | runs the bundled `assemble.py` — dedup, scoring, link states, disagreement explanation, gaps |

`assemble.py` is stdlib-only, class-free and deterministic: two runs on the same
input are byte-identical, including across `PYTHONHASHSEED` values. All
arithmetic lives there. Nothing is ever scored by hand, because prose arithmetic
does not reproduce.

### Runtime

Python 3.11 in the sandbox, no extra packages. No `tools.ts` — the agent needs
no host-side handler, because bundled skill scripts execute in the sandbox
(verified) and Paperclip is reached over MCP rather than a local CLI.

---

## Install

Three different things people mean by this.

### A. Deploy the agent (what you do once)

```bash
bun install                  # once per machine
cp .env.example .env         # add ANTHROPIC_API_KEY
bun run deploy               # uploads skills, creates or versions the agent
```

`deploy` uploads each changed `skills/<dir>/` bundle to the Skills API, creates
or versions the Managed Agent with `CLAUDE.md` as its system prompt, provisions
the memory store on first run, and writes the resulting ids back into
`manifest.json`.

It is **idempotent and content-addressed** — a skill whose bundle hash is
unchanged is skipped, and a re-run with nothing changed reports `unchanged`
across the board rather than churning versions.

The agent name is optional. `bun run deploy` reads `manifest.json`; passing a
name checks it against `manifest.name` rather than selecting a directory, so a
wrong name errors instead of quietly deploying something else.

Requirements:

- `ANTHROPIC_API_KEY` in `.env` at the repo root
- a Paperclip credential in a platform vault, its id in `manifest.vault_ids`

### B. Call the deployed agent (what everyone else does)

Nothing to install. It is a server-side agent with an HTTP endpoint.

```bash
bun run console              # opens it in the Claude Console
bun run console -- --once "$(cat fixtures/q-disputed.txt)"
```

Any backend can drive it with three HTTP calls — create a session, send the
task, read the SSE stream. See `lib/claude-managed-agent.ts`.

### C. Prototype it locally (what you do to change it)

Only needed if you want to iterate on the agent itself.

```bash
git clone <repo> && cd <repo>
bun install
cp .env.example .env        # add ANTHROPIC_API_KEY
```

For running the pipeline by hand against the corpus you also need the Paperclip
CLI. Note the vendor installer is currently broken on macOS — its launcher
resolves to the system Python 3.9 while its vendored `urllib3` needs 3.10+, and
it never installs `click` or `rookiepy` at all. A working install:

```bash
python3.11 -m venv ~/.paperclip/venv                       # 3.11 or 3.12; NOT 3.13+
curl -sL -o /tmp/gxl_paperclip-0.7.36-py3-none-any.whl \
     https://paperclip.gxl.ai/paperclip.whl                # served under a name pip rejects
~/.paperclip/venv/bin/pip install /tmp/gxl_paperclip-0.7.36-py3-none-any.whl
ln -sf ~/.paperclip/venv/bin/paperclip ~/.local/bin/paperclip
paperclip login
```

Three traps, all verified: `pip install https://paperclip.gxl.ai/paperclip.whl`
fails because the served filename violates PEP 427 and pip rejects it before
downloading; `pip install gxl-paperclip` fails because the PyPI index returns
200 with no distributions; and `rookiepy` publishes wheels only for cp37–cp312,
so on Python 3.13+ it tries a Rust source build and fails.

The agent's own skills are **not loadable** while prototyping — the session's
cwd is the repo root, so `skills/` is outside
what the Skill tool discovers. Read the SKILL.md files and follow them by hand;
the deploy smoke test is what proves real skill loading.

### Verifying a change

There is no test framework; these four are the verification story, and all four
must be green before a change ships.

```bash
python3 skills/graph-assembly/assemble.py --selftest   # assembler properties
bun run validate                                       # examples vs JSON Schema
bun run typecheck
bun run check                                          # ultracite/biome
```

`bun run test` runs the first two together. `bun run validate <graph.json>`
checks any graph you have to hand.

The assembler CLI, beyond `--input`:

```bash
# print or list stored graphs, read-only — never assembles, never writes
python3 skills/graph-assembly/assemble.py --memory-dir runs --show g_e087
python3 skills/graph-assembly/assemble.py --memory-dir runs --list

# upgrade a graph assembled before the interpretability contract existed:
# re-derives coverage defaults and rebuilds the block. Pure — no scores change.
python3 skills/graph-assembly/assemble.py --rebuild runs/g_e087.json --out -

# ...and, given re-fetched source text as <source-dir>/<paper_id>.txt, re-check
# every quote. A paper with no file keeps quote_verified null, never true.
python3 skills/graph-assembly/assemble.py --rebuild runs/g_e087.json \
    --source-dir /tmp/src --memory-dir runs --save --out runs/g_e087.json
```

`--save` on a rebuild matters: without it the stored state keeps findings with
no `quote_verified`, the next round fails them closed, and the verification is
thrown away.

---

## What it guarantees, and how each one is enforced

Every one of these is a mechanism, not an intention. Where a rule is checked by
code, that is said; where it is reported rather than enforced, that is said too.

| guarantee | enforced by |
|---|---|
| Every finding carries a **verbatim quote** | `verify_quote` string-matches it against the fetched text. No match, the finding is dropped and counted in `coverage.no_quote_discarded`. Never repaired. |
| **Quote verification fails closed** | The match only ever sets `quote_verified: true` on the branch where a real string match succeeded. A paper with no `source_text` means the check never *ran*: the finding is kept, marked `null`, counted in `coverage.quotes_unverified`, graded no higher than `LOW`, and raises `UNVERIFIED_QUOTE`. `null` is never presented as verified. |
| **Relationship ids are stable across rounds** | A link id binds to its `(from, how, to)` triple the round it first appears and is carried through entity merges. Ids were previously positional (`L1…Ln` over the sorted triples), so one new relationship sorting early renumbered every link after it — and `delta` and `changed_in_round` both key on the id, so they reported movement for relationships that had not moved. |
| **The output contract is machine-checked** | [`schema/graph.schema.json`](./schema/graph.schema.json), Draft 2020-12. `bun run validate` checks the checked-in examples, that every id resolves, that numeric metrics carry units — and that the schema *rejects* a graph with `interpretability` removed. |
| **Nothing is filtered by score** | The only removals are a failed quote match and a content duplicate. Confidence is never a threshold anywhere. |
| **A retried round is a no-op** | Findings dedupe on content — paper + relationship + normalized quote — not on id, because a retry re-extracts the same sentences under fresh ids. Reported in `coverage.duplicates_dropped`. |
| **Assembly is deterministic** | `assemble.py` is stdlib-only and byte-identical across runs and `PYTHONHASHSEED` values. All arithmetic lives there; nothing is scored by hand. |
| **Entities merge on identity, not similarity** | UniProt accession first, name second. Exact, auditable, species-safe (`Q9NWZ3` ≠ `Q8R4K2`), and a node with non-empty `ambiguity` is never a merge key. Every merge records `merged_from`. |
| **A dead corpus never reads as "no evidence"** | A canary query runs before any real search; a missing or failing Paperclip tool returns `status: "failed"`, `stop_reason: "search_unavailable"` and an `error` prefixed `PAPERCLIP UNAVAILABLE:`. |
| **The reply is machine-readable** | The first character is `{`, the last is `}`. No preamble, no fence — a downstream `JSON.parse` is the consumer. |
| **Facts append, derivations recompute** | `findings/r<N>.json` holds only that round's findings. `links` and `gaps` are recomputed every round, because a link's confidence legitimately moves when new evidence arrives — appending a derived score would store one already known to be wrong. |
| **Round ids are local, and never reach stored rows** | A round numbers its own papers `p1, p2…`; the assembler translates them. Stored findings take only the entity-coalesce remap, never the incoming round's map. |
| **Ids are never reused** | A link or gap id is bound to what it names the round it first appears, and the high-water counter rides in `coverage.id_seq` so a row that later disappears does not free its number for something else. An id a consumer stored either still resolves or is gone — it never comes back meaning a different thing. |
| **A caller cannot vouch for its own quote** | `quote_verified` on an incoming finding is discarded before assembly. Only the string match, or a value a prior round earned, may set it `true`. |
| **Gaps are ranked, not just counted** | Basis of both supporting links, paper-independence, a hub penalty, and a bonus when several intermediates imply the same missing pair. |

Reported rather than enforced, so a shortfall is visible instead of silent:
`coverage.interventions_without_target`, `proteins_without_accession`,
`has_disease_node`, and `confidence_profile` (including `hedged_but_confident`,
findings whose own two fields disagree about the same sentence).

## Modelling rules that shape the graph

These are the decisions that determine whether the graph is usable downstream.

**Nodes are concepts; reagents are conditions.** `IRAK4 inhibition` is one node.
`PF-06650833`, `KIC-0101` and `IRAK4 kinase-deficient mice` are ways of doing it
and belong in `where`. Splitting one relationship across four reagents produces
four `single_source` links where the literature supports one `agreed` link, and
understates every confidence nearby.

**But compounds stay distinct, and each states its target.** For *"what inhibits
IRAK4?"*, eight compounds is the answer — merging them destroys it. What makes
their evidence poolable is the **edge**: every intervention emits a quoted
finding naming what it acts on, so pooling runs along the mechanism path.
Unlinked ones are reported, never invented.

**Identity is an accession, resolved from the quote.** Every `protein`/`gene`
node carries `uniprot_accession`. Resolve from the evidence, not the label — the
string "IL-6" gives P05231, the ligand, while receptor-blockade evidence is
P08887, a different molecule. Where it cannot be resolved, leave it off and say
why in `ambiguity`.

**The graph is self-describing.** The disease or indication is its own node
whenever the question concerns one. A consumer that has to parse the question
string is reading input the graph should have encoded.

**`how` is a closed set** — `inhibits` · `activates` · `binds` · `suppresses` ·
`increases` · `decreases` · `causes` · `drives` · `treats` · `associated_with`.
Links key on `(from, how, to)`, so a free-form verb forks one relationship into
several. Activity is not abundance: a kinase inhibitor `inhibits` its target;
`suppresses` is for a process or phenotype; `decreases` is for a measured
quantity.

**`findings.confidence` is calibrated to be used at the bottom.** It never
filters anything — its job is to let Stage 2 tell a hard result from a
speculation, and the low-confidence findings are the ones worth exploring for
novel directions. 0.9–1.0 quantified primary result · 0.5–0.65 hedged ·
0.3–0.45 discussion-section speculation. `hedged: true` means ≤ 0.65.

## When Paperclip is down

A dead search tool and an empty literature look identical in the output, and
confusing them is the worst failure this agent can produce. "No evidence found"
is a scientific claim; emitting it because the corpus was unreachable is a
fabricated one, and the caller cannot tell.

So the agent probes before it concludes:

```
search -s pmc "rheumatoid arthritis" -n 1     # a healthy corpus cannot return nothing
```

If that canary fails, it stops — it does not run the planned queries, does not
fall back to another source, and returns:

```jsonc
{ "status": "failed", "error": "<the tool's own message, verbatim>",
  "coverage": { "found": 0, "read": 0, "used": 0,
                "stop_reason": "search_unavailable" },
  "things": [], "papers": [], "findings": [], "links": [], "gaps": [] }
```

For an extending ask the prior graph is left **unchanged** in memory — a failed
round never overwrites good state. If Paperclip dies mid-round, whatever was
already quote-verified is kept and the reply is `status: "partial"` with the
same `stop_reason`. And a `test_gap` that could not query never sets
`searched_in_round`: nothing was searched.

---

## Status and gaps

`BUILD.md` defines six blocking facts. All six are verified.

| # | fact | result |
|---|---|---|
| 1 | Paperclip called at the event level, not claimed in prose | ✅ MCP calls in every trace |
| 2 | reply carries the full graph JSON, not a summary | ✅ 43KB raw, asserted mechanically |
| 3 | disputed fixture yields ≥1 link `state: "disagreed"` | ✅ see below |
| 4 | a low-confidence finding survives into the output | ✅ nothing filters on score — verified in code |
| 5 | round 2 loads round 1 from memory and `round` increments | ✅ one graph reached round 7 |
| 6 | three quotes spot-checked verbatim against their DOIs | ✅ 3/3, re-resolved **by DOI** rather than by the id the agent used |

Fact 3, in production, on `fixtures/q-disputed.txt`:

```
L26  Vitamin C --increases--> melanoma metastasis   state: disagreed
why: "conditions differ: {braf v600e-driven melanoma, mouse}
                     vs  {b16f0 melanoma, gulo ko mice}"
```

It did not merely flag the conflict. It identified that one camp used
**Gulo-knockout mice** — the model that cannot synthesise vitamin C — which is
the actual reason those two labs disagree.

| capability | state |
|---|---|
| `new_question`, `expand_node`, `resolve_link`, `test_gap` | ✅ all four run in production |
| figure reading (`ask-image`) | ✅ produces `figure_caption` findings; `resolve_link`/`test_gap` only |
| MCP outage reporting | ✅ deliberately tested — credential invalidated, agent reported, credential restored |
| entity merge by accession | ✅ shipped; coverage of accessions in older graphs is still thin |
| `targets[]` block for the tractability node | ❌ **not built** — a contract change against a live consumer, needs its owner's sign-off |

### Known limits

- **Search is cheap; extraction is the bottleneck.** Runs routinely find 40–100
  papers and use 1–11. The tier's `max_papers` is not what binds.
- **Older graphs are not migrated.** Accession merging and the `how` enum govern
  new extraction. Changing a verb on an existing pair *forks* a parallel link,
  because links key on `(from, how, to)` — migrating an old graph needs a
  deliberate remap, not a re-extraction.
- **`quick` may never report absence.** Ten papers is page one, and page one
  lies. A negative finding needs the deepest tier that was actually run — a
  `standard` sweep once reported "nobody has tested this" and `deep` found the
  paper.
- **The reference graph's quotes are only partly re-verified.** `runs/g_e087.json`
  was assembled before `quote_verified` existed, and the artifact does not record
  that assembly-time verification happened — so the contract refuses to infer it.
  Re-fetching source text through Paperclip re-checked 35 of 44 findings; 3
  papers could not be re-resolved in the corpus by exact title, leaving 9
  findings at `null`, and 1 quote genuinely failed (it joins two sentences that
  are not adjacent in the paper). Both are reported, not repaired.
- **Quote matching ignores whitespace entirely.** Corpora render the same
  sentence with different spacing around punctuation — Paperclip's
  `content.lines` emits `( Figure 5F )` where the PDF reads `(Figure 5F)` — and
  two real findings failed on that alone. The comparison now drops whitespace on
  both sides. It cannot turn a paraphrase into a match: every non-space
  character must still appear, in order, contiguously.
- **The MCP watchdog and the outage report race each other.** With no server
  there is no MCP call to observe, so the client-side watchdog can fire before
  the agent reports the outage it exists to report. `--mcp-silence 0` stands it
  down.

## Repo map

| path | what it is |
|---|---|
| [`CLAUDE.md`](./CLAUDE.md) | the deployed system prompt, uploaded verbatim |
| [`manifest.json`](./manifest.json) | compiled config — model, MCP, memory, vault, deployment ids. Generated by `scripts/deploy.ts`, so it is excluded from the formatter |
| [`acl.ts`](./acl.ts) | who may call this agent through the router |
| [`skills/`](./skills) | three skills, uploaded to the Skills API unchanged. `graph-assembly` bundles `assemble.py` and `example-round.json` |
| [`fixtures/`](./fixtures) | three corpus-validated questions, each grading something specific |
| [`runs/`](./runs) | `g_e087.json`, a real two-round graph from the deployed agent, plus `g_minimal.json`, regenerated offline from `fixtures/round-minimal.json` and asserted byte-identical on every `bun run validate`. Excluded from the formatter for the same reason `manifest.json` is: it is generated |
| [`SCHEMA.md`](./SCHEMA.md) | **the authoritative data contract** — input first, then output. Root-level on purpose: it is the file consumers integrate against |
| [`scripts/rotate-paperclip-credential.ts`](./scripts/rotate-paperclip-credential.ts) | `bun run rotate` — refresh the Paperclip login and push the token into the vault. Required before any deployed call |
| [`schema/`](./schema) | the same contract as a validator. `graph.schema.json` is the module output; `interpretability.schema.json` is the **shared LABrador block**, `$ref`d from it and reusable by any other LABrador module |
| [`docs/CONTRACT.md`](./docs/CONTRACT.md) | deliverables, MCP details, `assemble.py` spec |
| [`docs/BUILD.md`](./docs/BUILD.md) | build plan and its six blocking acceptance facts |
| [`agent/tools/research-evidence-mapper.ts`](./agent/tools/research-evidence-mapper.ts) | the eve router wrapper. **Its filename is the router-facing tool name**, and `agent/instructions.md` dispatches to that name — rename the file and the dispatch entry silently points at a tool that does not exist |
| `lib/`, `scripts/`, `agent/` | session runtime, deploy/console CLIs, eve router wrapper |

This README is the entry point; [`SCHEMA.md`](./SCHEMA.md) — **at the repository
root, beside this file** — is the reference. Where the two disagree, `SCHEMA.md`
wins and the disagreement is a bug. It is kept at the root deliberately, because
it is the one file another team writes code against; please do not move it into
`docs/`.

## Provenance

Extracted from the LABrador workspace, where this node sits in a larger pipeline
(hypothesis → evidence → tractability → recruitment → economics). Only this node
and the shared runtime it needs are included here; the other nodes belong to
their own authors.

MIT — see [LICENSE](./LICENSE).

