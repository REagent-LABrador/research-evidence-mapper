---
name: literature-search
description: Turns an ask type, a target and a depth tier into Paperclip queries, runs them, and returns a normalized papers[] record plus the fetched text for each paper. Owns query construction, tier budgets, coverage accounting and the Paperclip seam. It does not judge whether a paper is good, does not decide what any finding means, and does not resolve disagreements — it reports what the corpus returned and what it failed to reach.
---

# literature-search

**In** — ask type, target, depth.
**Out** — normalized `papers[]`, the fetched text for each, and a truthful
`coverage` block.

This is the only component that touches Paperclip. Everything downstream
consumes the normalized record, so a change in the CLI surface is absorbed here
and nowhere else.

Paperclip is the **only** source of papers. There is no `web_fetch` fallback and
no second corpus. If Paperclip cannot reach something, that is a `coverage` fact
to report — not a licence to go around it. A graph built partly from another
source silently breaks the guarantee that every quote is verifiable against a
Paperclip document id.

## Budget

The tier is the budget. One round per request; never escalate on your own.

| depth | max_papers | max_queries | text |
|---|---|---|---|
| `quick` | 10 | 2 | abstracts only |
| `standard` | 25 | 4 | abstracts, full text on the top few |
| `deep` | 50 | 6 | full text on every paper that yields a finding |
| `exhaustive` | 300 | 12 | full text throughout |

Copy the row you used into `coverage.limits` and `coverage.depth`.

## Query construction, by ask

- **`new_question`** — decompose into entities and the relationship between
  them. Spend the query budget on *genuinely different phrasings*, not
  synonyms of one phrasing: the drug's generic name and its code name, the gene
  symbol and the protein name, the mechanism and the disease. Add
  `-s pmc,biorxiv,medrxiv` so preprints are in scope.
- **`expand_node`** — search the thing's `name` and **every** alias, one query
  each until the budget is gone.
- **`resolve_link`** — search the `from` / `how` / `to` triple. Bias toward the
  under-represented side: if `yes` has four findings and `no` has one, hunt for
  `no` with explicitly negative phrasings ("did not", "no significant", "failed
  to"). Bias also toward experimental conditions absent from every existing
  `where`. Pull full text.
- **`test_gap`** — search the missing pair directly and hard, then search it
  negatively. Full text. Both outcomes are legitimate; `nothing_new` here is a
  result.

## Commands

One command per call. The sandbox blocks `for`, `while` and `xargs`, so there is
no batching, and nothing carries between calls.

```
search -s pmc,biorxiv,medrxiv "query" -n 25
lookup pmc|doi|pmid|title|author|journal VALUE
ls /papers/<id>/
cat /papers/<id>/meta.json
head -n 200 /papers/<id>/content.lines
grep "pattern" /papers/<id>/content.lines
grep --bool '"A" AND "B"' /papers/ -m 2000
```

## Normalization

From `meta.json` build one record per paper:

| field | source |
|---|---|
| `doi` | `doi`, resolver prefix stripped, lowercased |
| `pmid` | `pmid` — carry it even though it is not in the output schema; dedup needs it |
| `title`, `year`, `journal` | as given |
| `first_author` | the **first** name in `authors`, not the last and not the corresponding author |
| `study_type` | `meta_analysis` \| `clinical_trial` \| `human_cohort` \| `animal` \| `test_tube` \| `computational` \| `review` — exact spellings, no substitutes |
| `is_preprint` | `source` is biorxiv/medrxiv/arxiv |
| `retracted` | as given; never infer it |

Keep the fetched text with its `L<n>` prefixes. Those line numbers are the quote
anchors and the citation anchors, and stripping them destroys both.

## The `years` window

`years: N` on the request means *published in the last N years*. Compute the
minimum year from today and pass `--year-min`:

```
search -s pmc,biorxiv,medrxiv "query" --year-min 2021 -n 25
```

**`--since` is a trap.** It is accepted on a PMC search and then does not
filter — `--since 2025-06-01` returned papers from 2024 and 2018. `--year-min`
was tested and binds correctly. Some sources reject the flag entirely
(`arxiv`, `medrxiv`, `abstracts` refuse `--since`), so check the error rather
than assuming the window applied.

Record the window in `coverage.years`, and record which sources it actually
reached. A window that silently does nothing is worse than no window, because
the graph then claims a bound its evidence does not honour.

## Failure modes

The longest section, because this is where runs actually go wrong. Every entry
below was observed, not imagined.

- **A dead Paperclip and an empty literature are indistinguishable in the
  output — and this is the most dangerous failure in the system.** If the tool
  errors, times out or returns nothing because the service is down, and you
  record that as "found 0", the graph says the science does not exist. Run the
  canary `search -s pmc "rheumatoid arthritis" -n 1` before the real queries: a
  healthy corpus cannot return nothing for it. If the canary fails, stop, do not
  run the planned queries, and hand back `status: "failed"` with
  `stop_reason: "search_unavailable"` and the tool's verbatim error. Absence is a
  claim; only make it when the search actually ran.
- **Distinguish "this query matched nothing" from "the tool failed."** A query
  returning zero after the canary passed is real evidence of sparsity. A query
  erroring is not evidence of anything. They must never land in `coverage` the
  same way.
- **Page 1 is not the corpus.** One search returning ten papers tells you what
  ranked highest for one phrasing, nothing more. Never conclude absence from a
  single query. Vary phrasing, add sources, then say what you did.
- **`quick` may never report "no evidence."** Ten papers is page one and page
  one lies. At `quick`, absence means *unknown*: set `status: "empty"` with
  `truncated: true` and never let it read as a negative result.
- **`cat` on `content.lines` does not return the text.** On a large paper it
  returns a **preview plus usage guidance** — one paper measured ~498,000
  tokens, and the CLI refuses rather than dumping it. If you feed that output to
  quote verification, every quote fails and you will wrongly discard a whole
  paper's findings. Use `head -n N` for a prefix and `grep` for targeted lines.
  Always confirm the text you fetched actually contains the sentence you are
  about to quote.
- **Corpus-wide `grep` counts are lower bounds, never totals.** It prints "hit
  the per-shard match cap — more matches exist" and time-bounds itself. Raising
  `-m` helps but does not remove the ceiling: a query capped at 100 was still
  capped at 2000 when re-run with `-m 2000`. Report grep counts as "at least N",
  and never derive sparsity from one.
- **A co-occurrence hit is not a finding.** `grep --bool '"A" AND "B"'` matches
  papers where both strings appear *anywhere* — including reference lists,
  supplementary tables and figure captions. Observed: a search for an
  inhibitor plus a cell type returned a paper whose only match was the drug name
  in a screening table; resolving the id showed a whole-blood assay, not the
  cell type at all. Resolve the id and read the line before believing the hit.
- **A near-miss paper looks like a hit until you resolve its id.** Search will
  hand you a plausible stranger — right drug, wrong system; right gene, wrong
  organism. Corroborate identity against `meta.json` before it enters `papers[]`.
- **Relevance ranking is not quality, and the top of the list skews to
  reviews.** Observed: a naive query on a mature topic returned reviews or
  meta-analyses in *all eight* top positions. If you need primary studies, the
  phrasing must ask for them — trial names, outcome measures, cohort language.
- **A review asserting X is not independent evidence for X.** Mark it
  `is_own_result: false`. One review restating forty studies is one paper, and
  counting it as agreement inflates every score downstream.
- **Distinct first authors are not distinct groups.** Independence is computed
  from first authors, and that is gameable by the corpus itself: three papers
  from one institution appeared under three different first authors while
  sharing most of the author block. When independence matters, read the
  affiliation lines (`L3`/`L4`), not just the first name.
- **Funding is disclosed in the text, not the metadata.** Two trials presented
  as independent replications turned out to be funded by the manufacturer of the
  drug under test, stated plainly in their own funding sections. If independence
  is load-bearing for the answer, `grep` for "funded by" / "Funding" and record
  what you find.
- **A landmark primary can be absent while hundreds of papers discuss it.**
  Observed: a trial's own report was not in the corpus, though it is cited by
  name in 300+ papers there. Do not infer a paper exists because everyone cites
  it, and do not infer a claim is unsupported because its primary is missing.
- **Query variants can return the same set.** Two phrasings that share the rare
  term retrieve the same ranking and burn two of your queries for one query's
  coverage. Detect it by comparing returned ids, not by how different the
  strings look; if they overlap almost completely, spend the next query on a
  different vocabulary entirely.
- **`sql` aggregates time out.** Broad `COUNT(*)` hits the 15s statement limit
  and returns an error, not a number. It is for narrow metadata lookups.
  `--tables` is not a flag — running `sql` with no valid query prints the schema
  as usage text, which is the documented way to see the columns.
- **`search` has no `--offset`.** There is no paging. Widen with a larger `-n`,
  more sources, and different phrasings.
- **Preprint and published version are one paper.** They dedupe on DOI, then
  PMID, then title+year. Two rows for one study double-counts it in both
  agreement and independence.
- **The MCP is stateless and unbatched.** Repo selection, working directory and
  everything else vanish between calls. Never write a command that depends on a
  previous one having run.
- **Never create, check out, add to or commit to a Paperclip repo**, and never
  run `login`, `logout`, `setup`, `config`, `install` or `update`. This
  component is read-only against the corpus.
- **A flag error is not a dead end, and not a retry loop.** Fix the flag and
  retry once. Two failures on the same query: move on and record the shortfall
  in `coverage`. Silent truncation is the one outcome that must never happen —
  if coverage was cut, `stop_reason` says so.
