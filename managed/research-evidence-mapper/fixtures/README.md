# fixtures

Three questions. One row per distinct failure mode, and the last column says
why that case is in the set — a fixture that does not grade something specific
is just a demo.

Each `.txt` holds one task string: the JSON request exactly as the agent
receives it.

| fixture | question | depth | what it grades — why it is in the set |
|---|---|---|---|
| `q-well-studied.txt` | EGFR mutations predicting TKI response in advanced NSCLC | `deep` | **Dense consensus.** Should produce a rich graph, mostly `state: "agreed"`, with high `independence`. Grades whether scoring survives volume without collapsing everything to "yes". |
| `q-disputed.txt` | Do antioxidant supplements accelerate or suppress cancer metastasis? | `standard` | **Real conflict with a boundary condition.** Must yield ≥1 link `state: "disagreed"`. Zero disagreed links is a failure of this fixture, not a clean result. Grades `explain_disagreement`. |
| `q-sparse.txt` | Does the gut microbiome influence heterotopic ossification in FOP? | `deep` | **Honest thinness.** Exactly one primary study exists. Grades paper dedup, the independence penalty, and the refusal to pad a graph with loosely-related work. |

## Why these three, and what each will catch

### `q-well-studied` — EGFR / TKI

Verified depth: corpus-wide boolean greps for `"EGFR mutation" AND "gefitinib"`
and `"exon 19 deletion" AND "L858R"` both hit the cap at `-m 2000`, so **≥2000
papers is a hard lower bound, not a count**. Consensus is replicated across six
first authors at six institutions on five continents, and one non-industry
nationwide cohort (PMC11892703) states in its own text that ~15 RCTs converge
on PFS 8.0–13.3 months.

Three traps this fixture sets, all confirmed in the corpus:

- **Sponsorship is disclosed in the text, not the metadata.** Two of the
  apparent independent replications are manufacturer-funded trials of that
  manufacturer's own drug (PMC3887309 L43, PMC5970529 L69). An agent scoring
  independence on first author alone will overstate it.
- **Same-group repetition defeats first-author counting.** PMC5672602,
  PMC5760863 and PMC5519810 share the Tata Memorial author block — one group,
  three apparent sources.
- **A landmark primary can be absent while the corpus discusses it.** The IPASS
  primary is not retrievable by title, yet ≥300 corpus papers name it. Absence
  of the paper is not absence of the evidence.

Expect `no_effect` handling to be exercised too — but note that adjuvant
(CTONG1104, resected stage II–IIIA) and advanced disease are **different
clinical settings**, not one null replicated. The question is scoped to
"advanced" deliberately.

### `q-disputed` — antioxidants and metastasis

Two independent labs publish directly opposing in vivo results, and one
challenges the other **in its own text**:

> "We conclude that a broad range of antioxidants accelerate melanoma migration
> and metastasis and that BACH1 is functionally linked to melanoma metastasis"
> — PMC9945759 L6

> "We challenged the finding of Le Gal et al. by giving Trolox (a lipophilic
> peroxyl radical scavenger) to metastatic B16-F10-bearing mice…"
> — PMC9331881 L73

**`quick` will fail this fixture and that is the point.** The opposing camp is
not reachable from the obvious phrasing — queries worded around "antioxidants
accelerate metastasis" return only one side. `standard` or deeper, with genuinely
different phrasings, is required. This fixture therefore also grades the
"page 1 lies" discipline, not just disagreement handling.

Two known hazards: one senior author (Bergo) appears on several of the
camp-accelerate papers **and** a review of the same question, so independence is
inflated if scored naively; and the canonical camp-A primaries (Le Gal 2015,
Sayin 2014) are **not in this corpus**, so the graph must not claim them.

### `q-sparse` — gut microbiome and FOP

The rare true `single_source` case. Exactly one primary study exists corpus-wide
— `bio_5c4232079773`, "Gut microbiome-dependent IL-1 signaling is a mediator of
ACVR1R206H-driven heterotopic ossification" (bioRxiv, 2026-04-01, 7 sibling
pairs, unreviewed).

**Deduplication is load-bearing here.** That one study appears as three corpus
records — `bio_5c4232079773` and `PMC13081864` share a DOI and author list, and
`PMC12545689` is the same lab. An agent counting records reports three
independent sources; the correct answer is one. The records also disagree with
each other on their own headline effect size (47.4% p<0.05 vs 37.8% p=0.20).

This fixture grades the independence and evidence-quality penalties and the
refusal to pad — **not** a global "empty" status. One genuinely on-point primary
exists, so the right output is a thin graph that says so, not an empty one.

Two operational notes: `PMC12545689`'s `content.lines` holds only the title and
repeated affiliation lines while the substantive text sits in `meta.json`, so an
extractor reading only `content.lines` will find nothing there; and this fixture
has the **shortest shelf life** of the three — the preprint is recent and
follow-ups will likely thicken the literature, at which point it stops testing
sparsity and needs replacing.

## Provenance

Candidates were proposed and then adversarially verified against the live
corpus; the verifier's job was to break them, and it corrected several claims in
the process (the primary-study count, a non-replicated `no_effect`, and the
independence inflation above). Every quote here was retrieved from the corpus at
its stated line, not paraphrased.
