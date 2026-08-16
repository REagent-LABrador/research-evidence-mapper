# Knowledge Graph — Stage 1 ⇄ Stage 2 Contract

**This is the authoritative contract for the agent, and it lives at the root**
**of the repository.** Everything a consumer needs is here: the request you
send, every field of the graph that comes back, the closed vocabularies, and
the guarantees. [`README.md`](./README.md) summarises this file for
orientation; where the two disagree, this file is correct.

**Machine-checkable form:** [`schema/graph.schema.json`](./schema/graph.schema.json)
(JSON Schema Draft 2020-12) is this contract as a validator, and it `$ref`s the
shared LABrador interpretability block from
[`schema/interpretability.schema.json`](./schema/interpretability.schema.json).
Run `bun run validate <graph.json>` to check any graph against it. This prose
file explains; the schema decides.


One JSON file per question, **grown across rounds**. Five lists referencing each
other by `id`. Nothing nested.

| list       | one row is                                    | drawn?            |
|------------|-----------------------------------------------|-------------------|
| `things`   | a molecule, gene, protein, disease, method    | yes — the dots    |
| `papers`   | one source                                    | no                |
| `findings` | one claim from one paper + its exact sentence | no — click a line |
| `links`    | one relationship, summarizing its findings    | yes — the lines   |
| `gaps`     | a relationship implied but never stated       | yes — dashed      |

`findings` is the raw evidence, `links` is the summary of it. Same data, two levels.

## Input — the request (also called: the task string)

One ask per request. Point at a row **by id** — never describe it in prose.

**The canonical first call — seeding a graph from a question:**

```jsonc
{
  // no graph_id — new_question is the only ask that doesn't extend
  "ask": "new_question",
  "target": "can a small-molecule IRAK4 inhibitor suppress synovial fibroblast-driven inflammation in rheumatoid arthritis, or is its effect confined to the myeloid compartment?",
  "depth": "standard",   // 25 papers; enough to seed things + links, not to claim absence
  "years": 5,            // optional: only papers from the last 5 years
  "reason": "seeding the hypothesis-generation graph for the RA / IRAK4 track"
}
```

**Every field, and what it does:**

```jsonc
{
  "graph_id": "g_7f2a",     // omit for new_question
  "ask": "resolve_link",    // new_question | expand_node | resolve_link | test_gap
  "target": "L2",           // an id from that graph; the QUESTION for new_question
  "depth": "deep",          // quick | standard | deep | exhaustive
  "years": 5,               // only papers from the last 5 years. omit = no limit
  "reason": "blocking a downstream decision"   // logged, not acted on
}
```

**Only `target` is required.** Every other field defaults, and a missing field
never refuses a round — `{"target": "does X affect Y"}` is a valid request.

| omitted or unusable | default |
|---|---|
| `ask` | `new_question` |
| `depth`, or not one of the four tiers | `standard` |
| `years`, or not a positive number | unbounded — all years |
| `reason` | `null` |
| `graph_id` | treated as `new_question` |
| the string is not JSON at all | the whole text becomes the question |

Every substitution is listed in `coverage.defaults_applied`, so two different
requests never produce indistinguishable output and a caller who wanted `deep`
can see they got `standard`.

**The one hard failure is the question.** A missing or empty `target` on
`new_question`, or a `target` naming an id that does not exist on an extending
ask, returns `status: "failed"` with `error` set and every list empty. Nothing
is guessed.

**`years` is a minimum publication year, applied as `--year-min`.** Note for
implementers: `--since` is accepted by the search and then does not filter
(a `--since 2025-06-01` query returned 2024 and 2018 papers), and some sources
reject the flag outright. `coverage.years` records the window and must never
claim a bound the search did not apply. A windowed absence is also a weaker
claim than an unbounded one — "nothing in 5 years" is not "nothing".

- **`expand_node`** — `target` is a `things` id (`"t1"`). *What else connects to
  this?* Searches that thing's `name` + `aliases`. Returns new links touching it,
  plus any new things those links introduce. Use when a node is central
  (high `mentions`) but has few links.
- **`resolve_link`** — `target` is a `links` id (`"L2"`). *Get more evidence on
  this exact relationship.* Searches the `from`/`how`/`to` triple, biased toward
  the under-represented side (`yes` vs `no`) and toward conditions not yet seen in
  `where`. Use when `state` is `disagreed` or `single_source`.
- **`test_gap`** — `target` is a `gaps` id (`"g1"`). *Has anyone actually stated
  this?* Searches the missing pair directly. Two outcomes: the gap is promoted to
  a real `link`, or it survives with `searched_in_round` set. A pair that has been
  **looked for** and not found is a much stronger claim than one nobody searched.
- **`new_question`** — `target` is free text. The only ask that does **not**
  extend: returns a **new** `graph_id` at `round: 1`. Omit `graph_id`.

Rules:

- One ask per request. Round accounting is per-request.
- Unknown `graph_id` or `target` → error. No partial graph is returned.
- The reply is always the **full graph**, never a delta — diff on `round`.
- `expand_node`, `resolve_link`, `test_gap` all extend in place: same `graph_id`,
  `round` increments, existing ids stay stable.

## Output

```jsonc
{
  "schema_version": "1.1",
  "graph_id": "g_7f2a",          // stable across rounds; the thing you extend
  "question": "can a small molecule bind an antibody to create a new one?",
  "round": 2,                    // how many search passes have run
  "status": "ok",                // ok | empty | partial | failed — see note 6
  "generated_at": "2026-08-15T14:20:00Z",
  "error": null,                 // one-line plain-text cause when status != ok.
                                 // Prefixed "PAPERCLIP UNAVAILABLE: " when the
                                 // corpus could not be reached at all.


  "delta": {
    "round": 2, "things_added": ["t34"], "papers_added": ["p9"],
    "findings_added": ["f40", "f41"], "links_added": ["L36"],
    "links_changed": ["L12"], "gaps_added": ["g7"], "gaps_resolved": ["g3"]
  },
  // derived, never stored. The reply is still the FULL graph -- one parser, no
  // reassembly, no ordering dependency. `links_changed` is scores MOVING, which
  // is correct behaviour, not drift.

  "rounds": [                    // what each round asked, and what it cost
    { "n": 1, "ask": "new_question", "target": null, "depth": "standard",
      "papers_added": 25, "outcome": "new_evidence" },
    { "n": 2, "ask": "resolve_link", "target": "L2", "depth": "deep",
      "papers_added": 18, "outcome": "contradicted" }   // new_evidence|nothing_new|promoted|contradicted
  ],

  "coverage": {                  // what was NOT read. Always present.
    "depth": "deep",             // tier of the LATEST round — see note 2
    "found": 412,                // results the search reported existing
    "read": 43, "used": 40,      // abstracts pulled / papers yielding a finding
    "truncated": true,           // true = a sample, not the literature
    "no_quote_discarded": 6,
    "duplicates_dropped": 0,
    "years": 5,                  // the request's time window; null = unbounded
    "defaults_applied": ["depth: standard (not supplied)"],   // every substitution
    "figures_read": 1,           // ask-image calls; 0 on new_question/expand_node

    // Reported, never enforced — a shortfall is visible instead of silent.
    "interventions_without_target": ["t6"],      // no edge to a protein/gene
    "interventions_without_target_count": 1,
    "proteins_without_accession": ["t9"],        // identity a consumer cannot key on
    "proteins_without_accession_count": 1,
    "has_disease_node": true,    // false = a consumer must parse the question
    "confidence_profile": {      // a scale nobody uses carries nothing
      "n": 38, "min": 0.35, "max": 0.9,
      "below_0_65": 4,
      "hedged_but_confident": ["f12"]   // hedged:true AND confidence > 0.65 —
                                        // the finding's own two fields disagree
    },

    "limits": { "max_papers": 50, "max_queries": 6 },
    "stop_reason": "max_papers", // max_papers|queries_exhausted|no_new_results|time_limit|complete|search_unavailable

    "quotes_unverified": 0,      // retained findings whose quote could NOT be
                                 // checked. A graph-level count, recomputed
                                 // every round, never accumulated
    "id_seq": { "link": 37, "gap": 50 },
                                 // highest link/gap number ever issued for this
                                 // graph. Carried so a row that disappears does
                                 // not free its id for something else next
                                 // round. Bookkeeping — never renders, never
                                 // scores. Absent on a graph read back with
                                 // --show that predates it.
    "queries": [                 // what was actually asked. Required by the
                                 // output contract; an empty list is legal but
                                 // raises QUERIES_NOT_RECORDED, because a search
                                 // nobody recorded cannot be reproduced
      { "q": "IRAK4 inhibitor synovial fibroblast",
        "tool": "paperclip.search",
        "search_id": "s_6247a4f6",   // Paperclip's own id for the search
        "n_results": 20 }
    ]
  },
  // papers carry, in addition to their bibliographic fields:
  //   "source_sha256": "9f2c…"   SHA-256 of the exact source text this paper's
  //                              quotes were checked against, or null when none
  //                              was supplied. Recorded at assembly, because
  //                              source_text itself is stripped before the reply.

  "things": [{
    "id": "t1", "name": "IRAK4",
    "kind": "gene",              // protein|small_molecule|gene|disease|process|method
    "aliases": ["IRAK-4"],       // surface forms merged here
    "mentions": 14,              // papers mentioning it — node size
    "round": 1,                  // round this node entered the graph

    // Identity. Required on every protein/gene node; omit on diseases,
    // processes, methods and small molecules. Resolve with
    // `search -s proteins "<name> human"` — and resolve from the QUOTE, not
    // the label: "IL-6" gives P05231 (the ligand) while receptor-blockade
    // evidence is P08887, a different molecule. Species is part of identity;
    // IRAK4 also matches Q1RMT8 (bovine) and Q8R4K2 (mouse).
    "uniprot_accession": "Q9NWZ3",
    "gene_symbol": "IRAK4",
    "resolved_by": "f7 quote names the kinase directly",
    "ambiguity": [],             // rejected candidates + why. NON-EMPTY MEANS
                                 // this node is never used as a merge key.
    "merged_from": [             // provenance; a merge is otherwise irreversible
      { "name": "interleukin-1 receptor-associated kinase 4", "via": "accession" }
    ]
  }],

  "papers": [{
    "id": "p2", "title": "...", "year": 2021, "journal": "...", "doi": "10.1038/...",
    "pmid": "34423919",          // carried for dedup: doi > pmid > title+year
    "first_author": "Rader",     // drives the independence score
    "study_type": "test_tube",   // meta_analysis|clinical_trial|human_cohort|animal|test_tube|computational|review
    "is_preprint": false, "retracted": false,
    "round": 1                   // which round pulled it
  }],
  "findings": [{
    "id": "f2", "from": "t2", "how": "binds", "to": "t3",
    // how: inhibits | activates | binds | suppresses | increases | decreases |
    //      causes | drives | treats | associated_with  -- closed set. Activity is not abundance: a kinase
    //      inhibitor `inhibits`, it does not `decrease`. Links key on
    //      (from, how, to), so a free-form verb forks one relationship.
    "says": "yes",               // yes | no | no_effect
    "quote": "the linker-38C2 conjugate binds integrin",   // EXACT source words
    "quote_verified": true,      // true  = matched verbatim against the paper's
                                 //         source_text at assembly
                                 // null  = NO source_text was supplied, so the
                                 //         check never ran. Never read null as
                                 //         "verified" — it is the fail-closed
                                 //         value, and it raises UNVERIFIED_QUOTE
                                 // false = checked and NOT found. Assembly
                                 //         discards those, so false only appears
                                 //         after `--rebuild --source-dir`
                                 //         re-checked a stored graph; it raises
                                 //         QUOTE_MISMATCH
    "paper": "p2", "where": "test tube",                   // conditions, if stated
    "section": "results",        // abstract|results|methods|discussion|figure_caption
    "is_own_result": true,       // false = citing someone else's work
    "hedged": false,             // "may", "suggests", "could"
    "confidence": 0.9,           // model's own read-accuracy score
    "flags": [],                 // labels, never filters
    "round": 1
  }],
  "links": [{
    "id": "L2", "from": "t2", "how": "binds", "to": "t3",
    "yes": ["f2"], "no": ["f3"], "no_effect": [],
    "state": "disagreed",        // agreed|disagreed|single_source|no_effect
    "why": "different conditions",        // only when disagreed
    "basis": "primary",          // primary|hedged_only|background_only|mixed
    "confidence": {
      "overall": 0.42, "label": "medium",
      "evidence_quality": 0.4,   // study strength, from study_type
      "agreement": 0.5,          // yes vs no counts
      "independence": 0.0        // distinct research groups
    },
    "changed_in_round": 2        // null if untouched since creation — see note 4
  }],
  "gaps": [{
    "id": "g1", "missing": ["t1", "t3"],   // pair nobody connected
    "implied_by": ["L1", "L2"],            // the links suggesting it
    "note": "nobody states 38C2 binds integrin directly",
    "confidence": 0.34,                    // capped at 0.6 — a proposal, not a finding
    "searched_in_round": null              // set once test_gap has looked for it
  }]
}
```

## `interpretability` — the shared LABrador block

Every reply carries a top-level `interpretability` object. It is **required**:
a graph without it fails `schema/graph.schema.json`, and `{"graph_id": "g"}`
alone no longer satisfies the output contract. It is present on successful
rounds, on scientifically empty ones, and on domain abstentions (an extending
ask against a graph that is not in memory). Only an infrastructure failure that
produces no reply at all is outside it — that is the orchestrator's to report.

It exists so a UI can answer six questions without reading the graph: what was
concluded, why, what backs each result, how each number was computed, what
uncertainty remains, and what would falsify it.

The block is **derived, never stored** — the same rule `delta` follows. It is a
pure function of the finished graph, so a stored graph can be re-emitted with a
current block (`--rebuild`) and there is never a second copy to disagree with
the first.

```jsonc
{
  "interpretability": {
    "schema_version": "1.0.0",     // the shared contract's version, not the graph's

    "headline": {
      "title": "Evidence graph: 37 relationships from 20 papers",
      "result": "EVIDENCE_MAPPED_DISPUTED",   // stable machine vocabulary, below
      "plain_language": "Read 2 papers and mapped 37 relationships from 44 …",
      "status": "QUALIFIED",       // SUPPORTED|QUALIFIED|INCONCLUSIVE|FAILED|NOT_APPLICABLE
      "basis": ["OBSERVED", "INFERRED", "MODELED"]
    },

    "metrics": [ /* papers found/read/used, findings verified/unchecked/mismatched,
                    relationships total/agreed/disputed/single-source/null,
                    gaps open, mean heuristic confidence, entities.
                    Every numeric one carries a unit. */ ],

    "steps":  [ /* ordered: search → screen → quote_verification → entity_resolution
                   → link_assembly → the three confidence terms → confidence.overall
                   → gap_ranking → stop. Each carries its real formula. */ ],

    "evidence":    [ /* one row per finding: claim, doi:/pmid: id, resolvable URL,
                        section locator, the verbatim quote, a grade, synthetic:false */ ],
    "assumptions": [ /* depth, both search limits, the year window, the scoring
                        weights, the study-quality table, the preprint penalty,
                        own-results-only, the gap-ranking constants */ ],

    "uncertainty":    { /* observed range of the heuristic score; seed and draws
                           are null because assembly is deterministic */ },
    "limitations":    [ /* structured codes, ERROR first — see the table below */ ],
    "counterfactuals":[ /* what would move the strongest link, recomputed through
                           the real scorer and labelled hypothetical */ ],
    "lineage":        [ /* output field ← input fields ← transformation */ ],
    "extensions":     { /* module-specific; the shared UI never requires it */ }
  }
}
```

### `headline.result` — the stable vocabulary

| value | when |
|---|---|
| `EVIDENCE_MAPPED` | relationships were assembled and none is disputed |
| `EVIDENCE_MAPPED_DISPUTED` | at least one relationship has primary papers pointing opposite ways |
| `EVIDENCE_MAPPED_SINGLE_SOURCE` | every relationship rests on exactly one paper |
| `NO_EVIDENCE_FOUND` | no finding survived quote verification |
| `GRAPH_UNAVAILABLE` | the round could not be served (`status: "failed"`) |

`status` is `SUPPORTED` only when the evidence is corroborated, every quote was
verified, and `stop_reason` is `complete`. Anything less is `QUALIFIED` — which,
for a literature search, is the normal case.

### `limitations` — the codes

Sorted ERROR, then WARNING, then INFO. Codes are stable; message text is not.

| code | severity | means |
|---|---|---|
| `QUOTE_MISMATCH` | ERROR | a quote was checked against source text and was **not** in it |
| `UNVERIFIED_QUOTE` | ERROR | quotes retained that were never checked — no source text was supplied |
| `EMPTY_EVIDENCE` | ERROR | nothing survived verification |
| `EMPTY_METRICS` | ERROR | no metric could be derived at all |
| `ROUND_NOT_SERVED` | ERROR | the round was refused for a reason other than a missing graph |
| `RETRACTED_SOURCE` | ERROR | a contributing paper is flagged retracted (graded `UNSUPPORTED`, not removed) |
| `GRAPH_NOT_FOUND` | ERROR | the ask targeted a graph that is not in memory |
| `DANGLING_REFERENCE` | ERROR | ids in findings/links/gaps that resolve to no row. Reported, never deleted |
| `ROUND_ERROR` | ERROR | the round's `error` line, stripped of markup |
| `ROUND_PARTIAL` | WARNING | `status: "partial"` — it finished, but not everything it meant to read was read |
| `ROUND_EMPTY` | WARNING | `status: "empty"` — the search ran and returned nothing usable |
| `COVERAGE_NOT_RECORDED` | WARNING | `found`/`read`/`used` are null, so coverage cannot be stated. Null, not zero |
| `INDEPENDENCE_OVERSTATED` | WARNING | papers with no `first_author`: `independence` falls back to the paper id, so it reads higher than the evidence supports |
| `PREPRINT_STATUS_UNRECORDED` | WARNING | papers with no `is_preprint`: the 0.8 penalty applies only on an explicit `true`, so they score as if peer-reviewed |
| `LIMITS_NOT_RECORDED` | INFO | no paper budget recorded, so reads cannot be attributed to a limit |
| `UNKNOWN_VALUE` | INFO | the inventory: every metric, step result and assumption in the block whose value is null |
| `TRUNCATED_SEARCH` | WARNING | the round hit a budget, not the end of the literature |
| `COVERAGE_INCOMPLETE` | WARNING | `stop_reason` is not `complete` |
| `QUERIES_NOT_RECORDED` | WARNING | the round did not record what it searched for |
| `SINGLE_SOURCE_LINKS` | WARNING | how many relationships rest on one paper |
| `PROTEINS_WITHOUT_ACCESSION` | WARNING | identity rests on name matching for some nodes |
| `INTERVENTIONS_WITHOUT_TARGET` | WARNING | intervention nodes with no molecular target |
| `NO_DISEASE_NODE` | WARNING | nothing in the graph reaches a clinical endpoint |
| `DEFAULTS_APPLIED` | WARNING | the caller omitted fields and defaults were substituted |
| `SOURCE_ID_MISSING` | WARNING | a paper has neither DOI nor PMID |
| `UNTAGGED_VALUE` | WARNING | metrics that are structural counts and cite nothing |
| `DISPUTED_LINKS` | INFO | how many relationships disagree |
| `QUOTES_DISCARDED` | INFO | findings dropped at assembly for failing verification |
| `HEURISTIC_CONFIDENCE` | INFO | always present: the score is not a probability |
| `YEARS_NOT_SPECIFIED` | INFO | no publication window was requested |
| `DEPTH_NOT_SPECIFIED` | INFO | no depth tier was recorded |
| `EMPTY_COUNTERFACTUALS` | INFO | no relationship exists, so there is nothing a further paper could move |
| `INTERPRETABILITY_PARTIAL` | INFO | fields the corpus cannot supply — see below |

Any `EMPTY_<COLLECTION>` code follows the same rule: an array in the block may
be empty only when it is genuinely not applicable, and then the matching code
says why. `bun run validate` fails if an array is empty without one.

### `extensions` — module-specific

| key | holds |
|---|---|
| `quote_verified` | **boolean.** `true` only when every retained quote was checked and matched |
| `quote_verification` | `{method, verified, unchecked, mismatched, discarded, fails_closed, by_evidence}` |
| `search` | queries, depth, limits, found/read/used, figures_read, truncated, stop_reason, years |
| `confidence_decomposition` | `{heuristic: true, is_probability: false, weights, terms, labels}` |
| `relationships` | per-link ledger keeping `supporting` / `contradicting` / `no_effect` **separate** |
| `gaps` | gap id → the resolved entity names for its missing pair |
| `graph` | graph_id, round, the round history |
| `id_conventions` | how each ledger id joins back to the graph |

`evidence[].source_type` is `publication` (peer-reviewed, `is_preprint: false`),
`preprint` (`is_preprint: true`), or `publication_status_unrecorded` — the paper
did not say. That third value is not cosmetic: `evidence_quality` applies its
0.8 preprint penalty only on an explicit `true`, so an unrecorded paper is
scored as if peer-reviewed, and `PREPRINT_STATUS_UNRECORDED` says so.

`relationships` and `gaps` are **annotations, not copies**. A link's `state`,
`basis`, `confidence`, `why` and `changed_in_round` live on `links[]` under the
same id and are not repeated here; a gap's `missing`, `implied_by`, `note`,
`confidence` and `searched_in_round` live on `gaps[]`. Evidence ids are
`evidence.<finding id>`. The block annotates the graph beside it — it does not
restate it, and a consumer should never have two places to read the same number
from.

Size, so it is not a surprise: on the reference graph the block is about half
the reply, and `evidence` is most of that, because one row per finding carries
that finding's quote. It scales linearly with `findings`, like the graph does.

The three evidence axes are never collapsed into one number. "Nobody looked" and
"somebody looked and found nothing" are different facts, and `no_effect` is the
second one.

### What this module cannot supply

`INTERPRETABILITY_PARTIAL` names them, per run:

- **no Paperclip document id** — the search interface used here does not return a
  stable document handle, so `evidence.source_id` falls back to `doi:` / `pmid:`.
- **no page or figure locator** — `evidence.locator` carries the finding's
  section label (`results`, `discussion`, `figure_caption`) and nothing finer.
- **`source_sha256` only where source text was supplied** — a paper read in an
  earlier round, or one whose text the round never fetched, has `null`.

None of these is filled with a guess.

## Worked example — a real request and a real reply

Everything below is copied out of [`runs/g_e087.json`](./runs/g_e087.json), the
graph the deployed agent actually produced. Values are unedited; the lists are
**excerpted**, and the counts under each show what the full artifact holds.

### The request

Two rounds were sent. Round 1 seeded the graph
([`fixtures/q-disputed.txt`](./fixtures/q-disputed.txt)):

```json
{
  "ask": "new_question",
  "target": "Do antioxidant supplements (N-acetylcysteine, vitamin E, vitamin C) accelerate or suppress cancer progression and metastasis?",
  "depth": "standard",
  "reason": "fixture: two labs publish directly opposing in vivo results — the graph must yield at least one disagreed link"
}
```

Round 2 asked for more evidence on the one link that came back `disagreed`:

```json
{
  "graph_id": "g_e087",
  "ask": "resolve_link",
  "target": "L21",
  "depth": "standard",
  "reason": "round-2 artifact: exercise links_changed on the disagreed link"
}
```

### The reply

Full graph both times. After round 2 it held **31 things,
20 papers, 44 findings,
37 links, 50 gaps** — about 51 KB.

**The link the question was really about.** Two labs disagree, and the graph
neither averaged them nor picked a winner:

```json
{
  "basis": "primary",
  "changed_in_round": 2,
  "confidence": {
    "agreement": 0.6667,
    "evidence_quality": 0.575,
    "independence": 1.0,
    "label": "medium",
    "overall": 0.6967
  },
  "from": "t3",
  "how": "increases",
  "id": "L21",
  "no": [
    "f20"
  ],
  "no_effect": [],
  "state": "disagreed",
  "to": "t11",
  "why": "conditions differ: {braf v600e-driven melanoma, mouse} vs {b16f0 melanoma, gulo ko mice}",
  "yes": [
    "f1_1",
    "f6"
  ]
}
```

`why` is the payload. One camp used BRAF-V600E melanoma in ordinary mice, the
other B16F0 melanoma in **Gulo-knockout** mice — animals that cannot synthesise
vitamin C, which is precisely why the two results differ. `agreement` is 0.6667
rather than 1.0 because two findings say yes and one says no, and
`independence` is 1.0 because the yes and no come from different first authors.

**The evidence behind it.** Every finding carries the sentence it came from:

```json
[
  {
    "confidence": 0.9,
    "flags": [],
    "from": "t3",
    "hedged": false,
    "how": "increases",
    "id": "f6",
    "is_own_result": true,
    "paper": "p3",
    "quote": "Four diet-relevant compounds from this list—VitC, β-carotene, retinyl palmitate, and canthaxanthin—were selected and found to accelerate metastasis in mice with BRAF V600E -driven malignant melanoma.",
    "quote_verified": true,
    "round": 1,
    "says": "yes",
    "section": "abstract",
    "to": "t11",
    "where": "braf v600e-driven melanoma, mouse"
  }
]
```

and the finding that disagrees with it:

```json
[
  {
    "confidence": 0.9,
    "flags": [],
    "from": "t3",
    "hedged": false,
    "how": "increases",
    "id": "f20",
    "is_own_result": true,
    "paper": "p6",
    "quote": "Ascorbate-supplemented gulo KO mice injected with B16FO melanoma cells demonstrated significant reduction (by 71%, p=0.005) in tumor metastasis compared to gulo KO mice on the control diet.",
    "quote_verified": true,
    "round": 1,
    "says": "no",
    "section": "abstract",
    "to": "t11",
    "where": "b16f0 melanoma, gulo ko mice"
  }
]
```

Note `where` on each. That field is what `explain_disagreement` compares, which
is why it is worth populating carefully — with both set to the same conditions,
this would have been a flat contradiction instead of a boundary.

**The papers they cite**, deduped on DOI → PMID → title+year:

```json
[
  {
    "doi": "10.1016/j.redox.2023.102619",
    "first_author": "Muhammad Kashif",
    "id": "p3",
    "is_preprint": false,
    "journal": "Redox Biology",
    "pmid": "36774779",
    "retracted": false,
    "round": 1,
    "source_sha256": "fe82c529c647951375a7c01ddfab81f2319691686c87d9ebf513ec4c4005e01a",
    "study_type": "animal",
    "title": "ROS-lowering doses of vitamins C and A accelerate malignant melanoma metastasis",
    "year": 2023
  },
  {
    "doi": "10.3892/ijo.2012.1712",
    "first_author": "John Cha",
    "id": "p6",
    "is_preprint": false,
    "journal": "International Journal of Oncology",
    "pmid": "23175106",
    "retracted": false,
    "round": 1,
    "source_sha256": "7a56de56868e1b0dfe7a9655c59f5a2ffe03b298d4f100e305bb46889aac363e",
    "study_type": "animal",
    "title": "Ascorbate supplementation inhibits growth and metastasis of B16FO melanoma and 4T1 breast cancer cells in vitamin C-deficient mice",
    "year": 2013
  }
]
```

**The entities**, resolved against the whole graph rather than this round:

```json
[
  {
    "aliases": [
      "AA",
      "APM",
      "Asc",
      "IVC",
      "L-ascorbic acid 2-phosphate sesquimagnesium (APM)",
      "VitC",
      "ascorbate",
      "ascorbic acid",
      "pharmacological ascorbate",
      "sodium ascorbate",
      "vitamin C"
    ],
    "id": "t3",
    "kind": "small_molecule",
    "mentions": 2,
    "merged_from": [
      {
        "name": "Vitamin C",
        "via": "name"
      }
    ],
    "name": "Vitamin C"
  },
  {
    "aliases": [
      "B16FO melanoma metastasis",
      "distant metastasis by melanoma cells",
      "malignant melanoma metastasis",
      "melanoma metastatic spread"
    ],
    "id": "t11",
    "kind": "process",
    "mentions": 2,
    "merged_from": [
      {
        "name": "melanoma metastasis",
        "via": "name"
      }
    ],
    "name": "melanoma metastasis"
  }
]
```

Neither carries a `uniprot_accession`: one is a small molecule and the other a
process, and neither has a UniProt entry. The field belongs on `protein` and
`gene` nodes only — `coverage.proteins_without_accession` below reports the ones
that should have had it.

**What round 2 changed**, derived and returned with the graph:

```json
{
  "findings_added": [
    "f1_1",
    "f2_1"
  ],
  "gaps_added": [],
  "gaps_resolved": [],
  "links_added": [],
  "links_changed": [
    "L21"
  ],
  "papers_added": [
    "p20"
  ],
  "round": 2,
  "things_added": []
}
```

`resolve_link` moved its target and nothing else: no new things, no new links,
two findings from one new paper, and `L21` rescored from 0.64 to 0.6967.

**Coverage — what was *not* read:**

```json
{
  "confidence_profile": {
    "below_0_65": 2,
    "hedged_but_confident": [],
    "max": 0.9,
    "min": 0.6,
    "n": 44
  },
  "depth": "standard",
  "figures_read": 2,
  "found": 71,
  "has_disease_node": true,
  "interventions_without_target": [
    "t2",
    "t3",
    "t30",
    "t5",
    "t6",
    "t7"
  ],
  "interventions_without_target_count": 6,
  "limits": {
    "max_papers": 25,
    "max_queries": 4
  },
  "no_quote_discarded": 0,
  "proteins_without_accession": [],
  "proteins_without_accession_count": 0,
  "queries": [],
  "quotes_unverified": 9,
  "read": 7,
  "stop_reason": "no_new_results",
  "truncated": true,
  "used": 2
}
```

Read that honestly: **71 results found, 7 read,
2 used**, `truncated: true`, and `stop_reason` says the round ended
because it ran out of queries rather than because the literature ran out. Only
`stop_reason: "complete"` means the latter.

`confidence_profile` shows the self-reported scores spanning
0.6–0.9 with
2 findings below 0.65, and
`hedged_but_confident` empty — no finding claims to be hedged while also scoring
above 0.65.

`interventions_without_target` lists nodes that never said what they act on, and
`proteins_without_accession` lists protein nodes with no identifier. Both are
**reported, never enforced**: a shortfall is visible in the output instead of
silently absent.


### The interpretability block

Excerpted from the same artifact — `metrics` shows 4 of 14, `steps` 1 of 11,
`evidence` 1 of 44, `limitations` 4 of 13, `counterfactuals` 1 of 3:

```json
{
  "counterfactuals": [
    {
      "change": "One further animal study by a different group reports the opposite direction for L2 (N-acetylcysteine (NAC) --increases--> melanoma metastasis).",
      "meaning": "The strongest relationship in the graph is not robust to a single contradicting primary result; it is a claim about the literature, not about the biology.",
      "result": "state agreed -> disagreed, heuristic confidence 0.84 -> 0.74"
    }
  ],
  "evidence": [
    {
      "claim": "Vitamin C increases melanoma metastasis (braf v600e-driven melanoma, mouse)",
      "grade": "MODERATE",
      "id": "evidence.f6",
      "locator": "abstract",
      "quote": "Four diet-relevant compounds from this list—VitC, β-carotene, retinyl palmitate, and canthaxanthin—were selected and found to accelerate metastasis in mice with BRAF V600E -driven malignant melanoma.",
      "source_id": "doi:10.1016/j.redox.2023.102619",
      "source_type": "publication",
      "source_url": "https://doi.org/10.1016/j.redox.2023.102619",
      "synthetic": false
    }
  ],
  "headline": {
    "basis": [
      "OBSERVED",
      "INFERRED",
      "MODELED"
    ],
    "plain_language": "Read 2 papers and mapped 37 relationships from 44 quote-backed findings; 1 are corroborated by more than one paper, 28 rest on a single paper, and 1 are disputed.",
    "result": "EVIDENCE_MAPPED_DISPUTED",
    "status": "QUALIFIED",
    "title": "Evidence graph: 37 relationships from 20 papers"
  },
  "limitations": [
    {
      "code": "QUOTE_MISMATCH",
      "field_path": "output.findings[].quote_verified",
      "message": "1 retained findings carry a quote that was checked against source text and did NOT appear in it. This is not a wording difference -- the match is whitespace- and typography-normalized -- so treat these as unsourced: f26.",
      "severity": "ERROR"
    },
    {
      "code": "UNVERIFIED_QUOTE",
      "field_path": "output.findings[].quote_verified",
      "message": "8 of 44 retained findings carry a quote that was never checked against source text, because no source text was supplied for their paper. Their quotes are shown but must not be treated as verified, and their evidence grade is capped at LOW.",
      "severity": "ERROR"
    },
    {
      "code": "QUERIES_NOT_RECORDED",
      "field_path": "output.coverage.queries",
      "message": "The round did not record the search queries it issued, so the search cannot be reproduced or audited from this output.",
      "severity": "WARNING"
    },
    {
      "code": "HEURISTIC_CONFIDENCE",
      "field_path": "output.links[].confidence.overall",
      "message": "link.confidence.overall is a heuristic score (0.4*agreement + 0.4*evidence_quality + 0.2*independence), not a probability. Do not threshold it as one.",
      "severity": "INFO"
    }
  ],
  "metrics": [
    {
      "assumption_ids": [
        "assumption.limits.max_papers",
        "assumption.own_result_only"
      ],
      "direction": "neutral",
      "display": "2 used",
      "evidence_ids": [],
      "id": "metric.papers_used",
      "label": "Papers contributing evidence",
      "meaning": "Papers that produced at least one quote-backed finding. The gap between read and used is the screening loss.",
      "unit": "papers",
      "value": 2
    },
    {
      "assumption_ids": [],
      "direction": "positive",
      "display": "35 verified",
      "evidence_ids": [
        "evidence.f1",
        "evidence.f10",
        "evidence.f15",
        "evidence.f16",
        "evidence.f17",
        "evidence.f18",
        "evidence.f19",
        "evidence.f1_1",
        "evidence.f2",
        "evidence.f20",
        "evidence.f21",
        "evidence.f22",
        "evidence.f23",
        "evidence.f24",
        "evidence.f25",
        "evidence.f27",
        "evidence.f28",
        "evidence.f29",
        "evidence.f2_1",
        "evidence.f3",
        "evidence.f30",
        "evidence.f35",
        "evidence.f36",
        "evidence.f37",
        "evidence.f38",
        "evidence.f39",
        "evidence.f4",
        "evidence.f40",
        "evidence.f41",
        "evidence.f42",
        "evidence.f5",
        "evidence.f6",
        "evidence.f7",
        "evidence.f8",
        "evidence.f9"
      ],
      "id": "metric.findings_verified",
      "label": "Quote-verified findings",
      "meaning": "Findings whose quote was matched verbatim against the paper's source text. This is a string match, not a judgement.",
      "unit": "findings",
      "value": 35
    },
    {
      "assumption_ids": [
        "assumption.own_result_only"
      ],
      "direction": "mixed",
      "display": "1 disputed",
      "evidence_ids": [
        "evidence.f1_1",
        "evidence.f20",
        "evidence.f6"
      ],
      "id": "metric.relationships_disputed",
      "label": "Disputed relationships",
      "meaning": "Relationships where primary papers report opposite directions. These are the ones worth reading by hand.",
      "unit": "relationships",
      "value": 1
    },
    {
      "assumption_ids": [
        "assumption.confidence_weights",
        "assumption.preprint_penalty",
        "assumption.study_quality_table"
      ],
      "direction": "unknown",
      "display": "0.4426 mean",
      "evidence_ids": [],
      "id": "metric.mean_link_confidence",
      "label": "Mean heuristic confidence",
      "meaning": "Mean of the per-relationship heuristic score. A ranking aid, NOT a probability that any relationship is true.",
      "unit": "score",
      "value": 0.4426
    }
  ],
  "steps": [
    {
      "assumption_ids": [
        "assumption.confidence_weights"
      ],
      "evidence_ids": [],
      "formula": "0.4*agreement + 0.4*evidence_quality + 0.2*independence; label high >=0.7, medium >=0.4, else low",
      "id": "step.confidence.overall",
      "inputs": [
        {
          "path": "code.CONFIDENCE_WEIGHTS.agreement",
          "unit": "weight",
          "value": 0.4
        },
        {
          "path": "code.CONFIDENCE_WEIGHTS.evidence_quality",
          "unit": "weight",
          "value": 0.4
        },
        {
          "path": "code.CONFIDENCE_WEIGHTS.independence",
          "unit": "weight",
          "value": 0.2
        }
      ],
      "label": "Combine into a heuristic confidence score",
      "method": "Fixed-weight linear combination. HEURISTIC -- not a probability, not calibrated against any outcome",
      "result": {
        "unit": "score",
        "value": 0.4426
      }
    }
  ],
  "uncertainty": {
    "draws": null,
    "intervals": [
      {
        "central": 0.4426,
        "confidence_level": null,
        "high": 0.84,
        "interval_type": "observed_range",
        "low": 0.24,
        "metric_id": "metric.mean_link_confidence",
        "unit": "score"
      }
    ],
    "limitations": [
      "low/central/high are the observed minimum, mean and maximum of the per-relationship heuristic score across the 37 relationships in this graph. They are an observed range -- NOT percentiles, NOT a confidence interval, and NOT a sampling distribution.",
      "The score is a ranking aid over what was read. It is not a probability that a relationship is true, and it is not calibrated against any outcome.",
      "seed and draws are null because assembly is deterministic: the same round bundle produces byte-identical output.",
      "The dominant uncertainty is not in the score, it is in coverage: 7 of 71 matched papers were read this round."
    ],
    "method": "Heuristic confidence decomposition (agreement, evidence quality, independence). No sampling, no model fitting and no statistical inference is performed.",
    "seed": null
  }
}
```

Three things to read out of that. **`QUOTE_MISMATCH` is an ERROR on a real
artifact**: re-fetching the source text for this graph showed that one retained
finding's quote stitches together two sentences that are not adjacent in the
paper. It is reported, not deleted — a rebuild must not silently change what a
stored graph says. **`UNVERIFIED_QUOTE` covers 9 findings** whose three papers
could not be re-resolved in the corpus by exact title; their quotes are shown
and their evidence grade is capped at `LOW`, because a quote nobody checked
cannot carry its source's grade. And **`low`/`central`/`high` are an observed
range**, not percentiles and not a confidence interval — `interval_type` says
so, and `uncertainty.limitations` says it again in words.

## Where the graph lives

**Stage 1 owns storage. Stage 2 never holds or sends the graph** — it sends a
`graph_id` and gets the graph back.

State lives in the agent's memory store, mounted at
`/mnt/memory/research-evidence-mapper/`, and survives across sessions:

```
/mnt/memory/research-evidence-mapper/
  index.json              # graph_id → question, round, updated_at
  g_7f2a/
    meta.json             # question, round, rounds[], coverage, status
    things.json
    papers.json
    links.json
    gaps.json
    findings/r1.json      # appended per round, chunked at ~80KB
    findings/r2.json
```

Split because a single memory file caps at **100KB**. Findings chunk by round,
which matches how they arrive — rounds append, never rewrite.

Consequences:

- A request carries `graph_id`, not the graph. Stage 1 loads prior state itself.
- The reply still carries the **full graph**, so Stage 2 can render without a
  second call.
- `index.json` is how Stage 2 discovers what exists without guessing ids.
- Graphs persist until deleted. Nothing expires them automatically.

## What Stage 1 guarantees

Enforced in code, not merely intended. Stage 2 can build on these.

- **Every quote is verified verbatim.** Before a finding is written, its `quote` is
  string-matched against the abstract that was actually fetched. No match, no
  finding — dropped and counted in `coverage.no_quote_discarded`. If a claim is in
  this file, that exact sentence is in that paper. This is the one guarantee the
  whole system rests on, so it is a mechanical check, never a prompt instruction.
- **Papers are deduped** on normalized DOI → PMID → title+year. A preprint and its
  published version are one paper. Without this, `agreement` and `independence`
  silently inflate.
- **Reviews cannot manufacture consensus.** Findings with `is_own_result: false`
  are excluded from `agreement` and `independence`; they remain in the file and
  surface through `basis: "background_only"`. One review restating 40 studies is
  one paper, not 40.
- **Names merge against the whole graph**, not just the current round. "KRAS"
  arriving in round 3 joins the existing "K-Ras" node rather than forking it.
- **Every id that does not resolve is reported.** `from`, `to`, `paper`, and the
  ids inside `yes`, `no`, `no_effect` are meant to point to a row in the same
  file. This is *checked*, not enforced: assembly does not delete a row for a
  bad id — that would lose evidence over a bookkeeping error — it raises
  `DANGLING_REFERENCE` at severity ERROR naming every id that resolves to
  nothing, and `bun run validate` fails on it. In practice a dangling id means
  the extractor referenced an entity it never declared.
- **`links` are recomputable** from `findings` + `papers`. Same inputs, same
  scores.
- **Gaps are ranked and capped** — at most 50, ordered by the weaker of the two
  supporting links. Open triangles grow quadratically; an uncapped list is noise.

## Notes

1. **Nothing is filtered by score.** Low-confidence findings stay in: a hedged
   claim marks an emerging area, a lone claim is a gap candidate. Stage 2 sets its
   own threshold. Only claims with no verbatim quote are removed.
2. **Depth tiers** — `quick` 10 papers / `standard` 25 / `deep` 50 /
   `exhaustive` 300. Caps bound the *input*; nothing extracted is filtered out.
   `exhaustive` runs ~60 extraction passes — minutes, not seconds. Not a live-demo
   tier.
   **`quick` may never report "no evidence"** — it reads page 1, and page 1 lies.
   At `quick`, absence means unknown.
3. **`findings.confidence` is model self-reported** at extraction. Everything under
   `links.confidence` is arithmetic computed afterward — recompute it with your own
   weights from `findings` + `papers` if you disagree.
4. **Scores move between rounds.** A link at 0.81 can drop to 0.44 when round 2
   brings a contradicting paper. That is correct behavior. **Never cache a score
   across rounds** — it is valid for one `graph_id` + `round` pair only. Diff on
   `changed_in_round`.
5. **`state: "disagreed"` usually isn't conflict.** Check `where` on both findings
   first; different experimental conditions is the common case.
6. **One request = one round.** Stage 1 never loops on its own — the tier is the
   budget. It stops on `coverage.stop_reason`, and only `complete` means the
   literature was exhausted; the other four mean it ran out of budget.
7. **Failure is still a graph.** `status` is never an error blob: `empty` (search
   worked, no matches), `partial` (some queries failed, graph under-covered),
   `failed` (search unavailable, `error` populated). Lists are empty, `coverage` is
   always real. One parser, not two.
8. **`outcome: "nothing_new"` is an answer, not a failure** — it is what makes
   `test_gap` worth running. Re-asking the same `target` at the same `depth`
   returns the cached result without spending; escalate `depth` to re-search.
