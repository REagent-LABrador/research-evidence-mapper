# Knowledge Graph — Stage 1 ⇄ Stage 2 Contract

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
    "stop_reason": "max_papers"  // max_papers|queries_exhausted|no_new_results|time_limit|complete|search_unavailable
  },
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

## Request — how Stage 2 asks for more

One ask per request. Point at a row **by id** — never describe it in prose.

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
- **Every id resolves.** `from`, `to`, `paper`, and the ids inside `yes`, `no`,
  `no_effect`, `implied_by` all point to a row in the same file.
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
