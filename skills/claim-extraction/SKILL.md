---
name: claim-extraction
description: Reads fetched paper text and emits findings[] rows, each carrying one relationship and the verbatim source sentence that states it, plus the line anchor, section, experimental conditions, hedging, and whether the paper is reporting its own result. Two modes — abstract-batch (5 papers per pass, for new_question and expand_node) and full-text-targeted (grep-first, for resolve_link and test_gap). It does NOT decide whether a claim is true, does not weigh one paper against another, does not resolve disagreements or explain them, does not score links, does not merge entities, and does not repair a quote that fails to match. Truth, weighting and merging belong to graph-assembly and to Stage 2; quote verification is a mechanical check in assemble.py, not a judgement made here.
---

# claim-extraction

In: papers plus their fetched text, from `literature-search`.
Out: `findings[]` rows — one per relationship stated by one paper.

You are a transcriber with a schema, not a referee. Every row you emit is a
claim *the paper makes*, recorded with the sentence that makes it. Whether the
claim is correct, whether it survives contact with the other 40 papers, whether
two papers actually disagree — none of that is yours. Record and move on.

---

## The one rule

**Every finding carries a verbatim quote. A quote that cannot be string-matched
against the fetched text is DROPPED, not repaired.**

`assemble.py`'s `verify_quote(quote, source_text)` runs a normalized-whitespace
substring test before any finding is written. It forgives runs of whitespace. It
forgives nothing else. No match → the finding is deleted and
`coverage.no_quote_discarded` increments.

That check is downstream and mechanical. It will fire whether or not you were
careful. Your job here is to make sure what you emit survives it:

- **Quote exactly.** Character for character, from the text you fetched.
- **Never tidy.** Not spacing, not capitalisation, not Greek letters, not
  subscripts, not a stray double space, not a typo. Especially not a typo.
- **Never join across an ellipsis.** Two half-sentences stitched with `…` is not
  a substring of anything. One contiguous run of characters or nothing.
- **Never quote text you did not fetch.** Not the search-result blurb, not a
  `map` answer, not your own memory of the abstract.
- **Keep the line number** from the file you quoted, and quote from
  `content.lines`.

A dropped finding costs one row and one increment on a counter that Stage 2 can
see. An unverifiable finding that slips through costs the guarantee the entire
system rests on: *if a claim is in this file, that exact sentence is in that
paper.* There is no version of this trade where paraphrasing wins. **When in
doubt, drop it.** Under-reporting is a visible, counted, honest failure;
fabrication is invisible.

---

## The row you emit

`assemble.py` assigns `id`, `from`, `to`, `paper` and `round`. You emit names and
keys; the script resolves them to ids so that entity merging stays deterministic.

```jsonc
{
  "from_name": "linear RGD SMDC",     // surface form AS THE PAPER WRITES IT
  "from_kind": "small_molecule",      // protein|small_molecule|gene|disease|process|method
  "how": "binds",                     // the relationship verb, normalized (see below)
  "to_name": "integrin α V β 3",      // surface form AS THE PAPER WRITES IT
  "to_kind": "protein",
  "says": "yes",                      // yes | no | no_effect
  "quote": "The linear RGD SMDC and the cRGDfK SMDC inhibited adhesion of α V β 3 -positive WM115 cells to vitronectin with IC 50  values in the low µM range, while no effect was observed for the α V β 3 -negative M21-L cell line.",
  "line": 5,                          // L-number in content.lines, integer
  "paper_key": "PMC9035832",          // document_id from meta.json
  "where": "wm115 cells",             // conditions, canonical form; null if unstated
  "section": "abstract",              // abstract|results|methods|discussion|figure_caption
  "is_own_result": true,
  "hedged": false,
  "confidence": 0.9,
  "flags": ["effect_size_dropped"]
}
```

That quote carries a contrast (`while no effect was observed for the … M21-L
cell line`), so it backs a **second** row — same `quote`, same `line`, same
`paper_key`, `says: "no_effect"`, `where: "m21-l cells"`. One quote may back
several rows; one row may not merge several relationships.

### `from_name` / `to_name`
The surface form the paper uses, unedited — including the mangled spacing
(`α V β 3`, not `αVβ3`). Merging is `resolve_entities`' job and it normalizes;
if you pre-normalize you destroy the alias that lets a later round match.
Direction matters: `from` acts, `to` is acted upon.

### `how`
One relationship verb from: `binds` · `inhibits` · `activates` · `increases` ·
`decreases` · `causes` · `treats` · `associated_with` · `expressed_in` ·
`measured_by` · `no_relationship_stated`. Pick the weakest verb the sentence
actually supports. A sentence reporting that two things correlate is
`associated_with`, never `causes`.

### `says`
- `yes` — the paper asserts the relationship holds.
- `no` — the paper asserts it does not hold, or reports the opposite direction.
- `no_effect` — the paper tested it and found nothing. This is a **result**, not
  an absence of one; it is the most under-recorded row type and the most
  valuable. `no_effect` is not the same as the paper being silent. Silence
  produces no row at all.

A negation belongs in `says: "no"`, never in the verb. There is no
`does_not_bind`.

### `quote`
One contiguous sentence from `content.lines`, verbatim. See **The one rule**.
If the relationship needs two sentences to state, emit the one sentence that
carries the assertion and put the surrounding context nowhere — context is not a
schema field, and stitching is a violation.

### `line`
The `L`-number **in `content.lines`**, as an integer (`L5:` → `5`). Lines are
whole paragraphs, so the anchor points at the paragraph containing your sentence,
not at the sentence. That is correct and expected.

### `where` — load-bearing, not decoration
This is the field `explain_disagreement` partitions on. Two findings that
disagree get reconciled as *"conditions differ: A vs B"* only if their `where`
values compare as distinct sets — so free prose here silently disables the one
feature this graph exists to demonstrate.

Rules:
- Lowercase. Shortest phrase that identifies the condition. No units, no
  temperatures, no durations, no articles.
- **One condition per finding.** If the sentence covers two cell lines with
  different outcomes, that is two findings.
- Prefer an existing value. Reach for this list first: `in vitro` · `in vivo` ·
  `cell free` · `mouse` · `rat` · `human` · `human primary cells` · a named cell
  line lowercased (`wm115 cells`, `hek293`) · `knockout` · `overexpression` ·
  `computational` · `patient cohort`. Extend only when none fits, and then use
  the paper's own noun, lowercased.
- **`null` when the sentence does not state conditions.** Never infer them from
  the Methods, never infer them from the journal, never infer them from the
  study type. `null` is a fact; a guess is contamination of the one field
  disagreement analysis trusts.

### `section`
The schema enum is fixed at five values and real papers do not use them. Map
deterministically:

| heading in the paper | `section` |
|---|---|
| Abstract (or `content.lines` L5) | `abstract` |
| Results; Results and Discussion | `results` |
| Methods; Materials and Methods; Experimental | `methods` |
| Discussion; Introduction; Background; Conclusion | `discussion` |
| a line beginning `FIGURE n`, `TABLE n` | `figure_caption` |

A merged "Results and Discussion" heading maps to `results` and gets
`flags: ["section_ambiguous"]`. Introduction mapping to `discussion` is
deliberate: both are prose about other people's work, and both should make you
look hard at `is_own_result`.

### `is_own_result`
`true` only when this paper generated the result. False for everything else.

`false` when any of: the sentence carries a citation marker (`( Smith et al.,
2019 )`, `[14]`, `Smith and Jones showed`); the sentence sits in Introduction or
Background; the paper's `study_type` is `review`; the verb is reporting
(`has been shown`, `is known to`, `it is established that`, `previous work
demonstrated`).

This flag is what stops reviews manufacturing consensus — `agreement` and
`independence` exclude `is_own_result: false`. Every row you get wrong here
inflates a score that Stage 2 will trust.

### `hedged`
`true` if the assertion is qualified: `may` · `might` · `could` · `suggests` ·
`appears to` · `is thought to` · `potentially` · `we hypothesize` · `likely` ·
`consistent with` · `implicates` · `is associated with a possible`. Hedging is
recorded, never removed — a hedged claim marks an emerging area and stays in the
graph.

### `confidence`
Your self-reported *read accuracy* — how sure you are that you read the sentence
correctly. Not how true the claim is, not how good the paper is. Anchors:

| | |
|---|---|
| `0.9–1.0` | one sentence states the relationship explicitly, both entities named in it |
| `0.7–0.8` | explicit, but one entity is named by pronoun or abbreviation resolved from the same paragraph |
| `0.5–0.6` | the relationship is stated across a clause boundary, or the direction took work to pin down |
| `< 0.5` | you are inferring. Do not emit the row. |

Nothing is filtered by this score downstream, so a low number is not a soft
delete — it is a signal. But below 0.5 the honest action is no row.

### `flags`
Labels, never filters. Allowed: `effect_size_dropped` · `figure_caption_only` ·
`conditions_unstated` · `review_restatement` · `section_ambiguous` ·
`quote_spans_citation` · `dose_dependent` · `negative_control` ·
`direction_from_context`.

---

## Mode A — abstract-batch

For `new_question` and `expand_node`. Breadth: many papers, abstract only.

1. Take papers **five at a time**. Not six. Attribution drift is the failure
   here — with more papers in one window, a sentence from paper 3 gets emitted
   under paper 4's key, and the quote verifies (it *is* verbatim, from the wrong
   paper) so nothing downstream catches it.
2. For each paper, fetch the abstract: `cat /papers/<id>/meta.json` gives
   `abstract`, and `head -n 8 /papers/<id>/content.lines` gives the same text as
   `L5`. **Quote from `content.lines` so you have a line anchor.**
3. Extract every relationship the abstract asserts. An abstract commonly carries
   3–8. Do not stop at the headline claim.
4. Emit rows. `section` is `abstract` for all of them.
5. Before moving to the next batch, run the self-check below on the batch you
   just produced. Do not batch the self-check to the end — that is when the
   quotes stop being in your context.

Abstracts are dense with hedging and with background sentences. The first
sentence of a biomedical abstract is almost always background
(`is_own_result: false`); the last is almost always a hedged interpretation
(`hedged: true`).

---

## Mode B — full-text-targeted

For `resolve_link` and `test_gap`. Depth: few papers, one relationship, whole
paper. **Grep first, read second.** Do not read a 5,000-line paper looking for a
sentence.

1. Grep the paper for both entities:
   `grep -n -i -C 1 "<entity>" /papers/<id>/content.lines`
   and for the relationship verb separately. Surface forms vary within one
   paper — the body may write `α V β 3` where the supplement writes `αVβ3`.
   Grep for the shortest distinctive fragment (`RGD`, `38C2`), not the full name.
2. `-C 1` gives you the neighbouring paragraphs so you can resolve pronouns and
   see whether a citation marker sits on the sentence.
3. Read the returned paragraphs. Extract from those. Quote from them.
4. When you need section context, `ls /papers/<id>/sections/` and read the
   matching section file — **but re-locate the sentence in `content.lines` with
   `grep -n` before recording `line`.** Section files renumber (see failure
   modes).
5. For `test_gap`, the productive outcome is often **no rows**. A pair that was
   searched for in full text and not found is a strong result. Emit nothing and
   let the round report it. Do not manufacture a weak row to avoid an empty
   return.
6. For `resolve_link`, bias reading toward whichever of `yes`/`no` is
   under-represented in the existing link, and toward `where` values not yet
   seen — but extract what the paper says, not what the link needs. Reading with
   a bias is legitimate; recording with one is not.

---

## Calibrate `confidence` — use the whole scale, especially the bottom

`findings.confidence` is your own estimate of how firmly the paper states the
claim. **It is never used to filter anything** — nothing is dropped for scoring
low. Its entire job is to let Stage 2 tell a hard result from a speculation,
and low-confidence findings are the ones Stage 2 explores for novel directions.
A finding you score 0.35 is not a worse finding; it is a different kind of lead.

Measured on a real run: 38 findings scored 0.75, 0.8, 0.85 or 0.9 and **nothing
lower**. A 0.15-wide band at the top of the scale carries no information, and
the speculative paths Stage 2 wants were invisible.

| score | the paper is | typical language |
|---|---|---|
| 0.9–1.0 | stating a quantified primary result unambiguously | "reduced X by 47% (p<0.001)" |
| 0.7–0.85 | stating a clear primary result without numbers | "abolished the response" |
| 0.5–0.65 | **hedging**, or measuring indirectly | "may contribute", "suggests", "is consistent with" |
| 0.3–0.45 | speculating — discussion-section conjecture, a mechanism proposed but not tested | "we speculate", "could explain", "warrants investigation" |
| < 0.3 | mentioning a possibility in passing | "it remains possible that" |

**A hedged finding is not a confident one.** If you set `hedged: true`, the
confidence belongs at **0.65 or below** — that is what hedging means. The two
fields contradicting each other is the single most common calibration error, and
assembly reports it in `coverage.hedged_but_confident`.

Two things this is NOT. It is not a quality judgement — a careful paper hedging
appropriately still scores low, because the score describes the *claim's
firmness*, not the authors' competence. And it is not evidence strength — that
is computed later from study type, agreement and independence, and lives in
`links.confidence`. Yours is only: how hard did this sentence commit?

## The graph must be self-describing: a disease node and an accession

Downstream nodes consume these graphs directly. They must not have to recover
basic facts from the question string — a consumer that parses the question is
reading input the graph was supposed to encode.

**Emit the disease or indication as its own `disease` node** whenever the
question concerns one, even if no finding is "about" the disease as such. The
question *"do antioxidants accelerate cancer metastasis?"* must leave `cancer`
(or the specific indication) in `things[]`. Observed failure: a graph of nine
compounds and two proteins with **no disease node at all**, forcing the
tractability bridge to re-parse the question.

**Every `protein` and `gene` node carries `uniprot_accession`.** This is the
identifier a consumer keys on, and without it the node is just a string. Across
seven real graphs, accession coverage was 0, 0, 0, 0, 1, 1 and 2 nodes — close
to absent. Resolve it: `search -s proteins "<name> human"`, then put the
accession, `gene_symbol` and a `resolved_by` naming the quote on the node.

Where it genuinely cannot be resolved — an ambiguous name, no matching entry —
leave the field off and record why in `ambiguity`. Assembly reports the
shortfall in `coverage`; a recorded gap is usable, a guessed accession is not.

## The `how` verb — activity is not abundance

`how` is a closed set. Links key on `(from, how, to)`, so a verb chosen freely
forks one relationship into several, and a downstream consumer reads the verb
to decide what actually happened.

| verb | means | use when |
|---|---|---|
| `inhibits` | reduces the **activity** of a protein | a kinase inhibitor, an antagonist, a blocking antibody |
| `activates` | increases the **activity** of | a kinase phosphorylates its substrate |
| `binds` | physical interaction, no direction claimed | co-IP, structural, affinity data |
| `suppresses` | reduces a **process or phenotype** | treatment suppresses inflammation, metastasis, disease severity |
| `decreases` | reduces a **measured quantity** | knockdown lowers mRNA; a score drops |
| `increases` | raises a measured quantity | expression is upregulated |
| `causes` | establishes causation of a disease or state | the mutation causes the syndrome |
| `drives` | contributes causally, short of sufficiency | pathway drives inflammation |
| `treats` | therapeutic benefit in a disease | the drug treats RA in a trial |
| `associated_with` | correlation only, no causal claim | expression correlates with severity |

**The distinction that matters: activity is not abundance.** A kinase inhibitor
`inhibits` its target — it does not `decrease` it. Reach for `decreases` only
when the paper measured a level, a count or a score.

Observed when this was free-form: eight compounds were linked to IRAK4 and
seven came back as `decreases`, one as `inhibits`. "KT-474 decreases IRAK4"
reads as *lowers IRAK4 protein levels*, which is not what any of those papers
showed — they inhibit the kinase. The pooling still worked, because the edge
existed either way, but a consumer reading `how` to infer direction would have
drawn the wrong biology from seven of eight edges.

**Three verbs that get confused, kept separate on purpose.** `inhibits` is
protein activity. `suppresses` is a process or phenotype — inflammation,
metastasis, disease severity. `decreases` is a measured quantity — an mRNA
level, a cell count, a score. "The inhibitor suppressed synovitis" is
`suppresses`; "the inhibitor decreased IL-6 levels" is `decreases`; "the
inhibitor inhibits IRAK4" is `inhibits`. All three are true of the same drug in
the same paper, about different things.

Pick from the table. If none fits, use the closest and say why in the finding's
`claim` — do not invent a verb, because a new verb silently creates a new link.

## Every intervention must say what it acts on

**When you create an intervention node — a compound, an inhibitor, a knockdown,
a knockout, any perturbation — you must also emit a finding linking it to its
molecular target.** One extra finding, with its own verbatim quote:

```jsonc
{ "id": "f9", "paper": "p4", "from": "t9", "how": "inhibits", "to": "t1",
  "says": "yes", "is_own_result": true, "section": "abstract",
  "where": "biochemical kinase assay",
  "quote": "PF-06650833 is a potent and selective inhibitor of IRAK4." }
```

Keep the compounds distinct — do **not** collapse them into one "IRAK4
inhibition" node. For a question like *what inhibits IRAK4?*, seven separate
compounds is the correct answer and merging them destroys it. What makes their
evidence poolable is the **edge**, not sameness.

Observed when this was missing: a graph reached seven IRAK4-inhibitor nodes and
only two stated the target. The other five floated with no path to IRAK4, so
every relationship they supported scored as a lone `single_source` link, and
`resolve_link` on any of them could not reach the others' evidence. The
literature said the same thing five times; the graph could not tell.

The target link is usually the easiest quote in the paper — it is in the title
or the first line of the abstract, because it is what the paper is about.

**If you genuinely cannot find a quote naming the target, do not invent one.**
Emit the intervention node without the link and let it be reported: assembly
lists unlinked interventions in `coverage.interventions_without_target`. An
unsupported edge is worse than a recorded gap.

## Resolve proteins to a UniProt accession — by the evidence, not the name

Names fragment a graph in both directions, so identity is carried by an
accession wherever one exists. Paperclip serves the protein corpus:

```
search -s proteins "IRAK4 interleukin-1 receptor-associated kinase 4 human"
  -> IRAK4  Q9NWZ3  Homo sapiens · 460 aa
     IRAK1  P51617  Homo sapiens          <- near-miss, different gene
     IRAK4  Q1RMT8  Bos taurus            <- same name, different species
     Irak4  Q8R4K2  (mouse)
cat /proteins/Q9NWZ3/meta.json
```

Put it on the thing:

```jsonc
{ "id": "t5", "name": "IL-6", "kind": "protein",
  "uniprot_accession": "P08887", "gene_symbol": "IL6R",
  "resolved_by": "f7 quote says 'IL-6 receptor blockade'",
  "ambiguity": ["P05231 IL6 — name matches, evidence does not"] }
```

**Resolve from the quote, not the label.** The string "IL-6" resolves to
P05231, the ligand. If the finding behind that node is receptor-blockade data,
the protein is P08887 — a different molecule. The name is evidence about what
the author typed; the quote is evidence about what was measured, and only the
second one identifies the protein.

**`ambiguity` is the field that earns its keep.** When name and evidence
disagree, or two accessions remain plausible, list the rejected candidates with
the reason. A node carrying a non-empty `ambiguity` is **never used as a merge
key** — an unresolved identity must not silently propagate through every link
that touches it. Leaving `uniprot_accession` off entirely is better than
guessing: no accession falls back to name matching, a wrong accession merges
two different proteins.

**Species is part of identity.** "IRAK4" alone matches human, mouse and bovine
entries. Take the organism from the paper's methods — the same `where` you
already record — and pick the matching accession. Accessions make this safe
mechanically, since Q9NWZ3 and Q8R4K2 are simply different keys, but only if
you look.

**Not everything gets one.** Diseases, processes, methods and small molecules
have no UniProt entry; leave the field off. For an intervention node, the
accession of its *target* belongs in the finding's `where` or on the target
node, not on the intervention.

## Node granularity — the intervention class is the node, the reagent is a condition

This is the single most consequential modelling decision you make, and getting
it wrong is invisible until scores are already wrong.

A node is a **concept**, not a reagent. `IRAK4 inhibition` is a node.
`PF-06650833`, `KIC-0101`, `adenovirus-mediated IRAK4 knockdown` and
`IRAK4 kinase-deficient mice` are **not four nodes** — they are four ways of
doing the same thing, and they belong in `where`, with the compound name also
added to the node's `aliases`.

Observed on a real graph when this went wrong:

```
L3  adenovirus-mediated IRAK4 knockdown --decreases--> synovitis
L4  KIC-0101                            --decreases--> synovitis
L6  PF-06650833                         --decreases--> RA fibroblast-like synoviocytes
L5  IRAK4 kinase-deficient mice         --decreases--> arthritis
```

Four `single_source` links where the literature actually supports one link with
four independent papers behind it. Two things break at once:

- **Evidence cannot pool.** `agreement`, `evidence_quality` and `independence`
  are computed per link. Splitting one relationship across four nodes gives
  four lonely links instead of one well-supported one, and every confidence in
  that neighbourhood is understated.
- **`resolve_link` cannot do its job.** Asked for more evidence on one link, it
  searches the relationship, finds a paper using a different reagent, and is
  forced to create a *new* node and a *new* link. The target never moves. That
  is not the ask misbehaving; it is this modelling error surfacing downstream.

**The rule.** If the claim is about the mechanism or class, the node is the
class and the reagent goes in `where`:

```jsonc
{ "from": "t1", "how": "suppresses", "to": "t3", "says": "yes",
  "where": "PF-06650833 100 nM, RA FLS, TLR ligands",   // the reagent lives here
  "quote": "…" }
```

**The exception.** When the claim is *about the compound specifically* — a
head-to-head comparison, a selectivity or off-target result, a
pharmacokinetics finding — then the compound is genuinely the subject and gets
its own node. "Drug A outperformed drug B" is a claim about A and B. "IRAK4
inhibition reduced cytokines" is a claim about the class, whichever molecule
was used.

When in doubt, ask what the paper is *arguing*. If swapping the reagent for
another of the same class would leave the claim intact, the class is the node.

## Failure modes

This is the long section because this is the part that actually goes wrong.
Every entry ends with what to do.

### Hedging read as assertion
`"may contribute to"` is not `"contributes to"`. `"is consistent with a role
for"` is not `"has a role in"`. `"was associated with"` is not `"caused"`.
Papers hedge deliberately and precisely; flattening the hedge converts an
emerging signal into a hard claim, and because `basis` is computed from
`hedged`, a link that should read `hedged_only` reads `primary`.
**Do:** set `hedged: true` and keep `says` as the paper's direction. Never
downgrade `says` to compensate for a hedge, and never pick a stronger `how` verb
than the sentence supports. Hedge and assertion are two independent fields; use
both.

### A background citation read as this paper's new result
The single most common corruption. Introductions are wall-to-wall assertions
about other people's work, phrased in the present tense with total confidence:
*"Integrin αVβ3 is overexpressed on tumour cells and drives angiogenesis
( Giancotti and Ruoslahti, 1999 )."* That reads exactly like a finding. It is a
citation.
**Do:** before setting `is_own_result: true`, look at the sentence and its
neighbours for a citation marker, and look at which section it came from. If the
section maps to `discussion` (which includes Introduction), the default is
`false` — flip to `true` only when the sentence says the authors did it (`we
found`, `here we show`, `in this study`). When unsure, `false`. False is the
conservative direction: it removes the finding from `agreement` and
`independence` rather than inflating them.

### Reviews restating others, marked `is_own_result: true`
A review's whole body is other people's results written in the review's voice,
often with the citations clustered at paragraph end rather than sentence end. A
single review marked wrong can carry 20 rows that all count as independent
support, and `independence` — which counts distinct first authors — sees one
author, one paper, twenty confirmations. That is the exact failure the schema's
"reviews cannot manufacture consensus" guarantee exists to prevent, and the
guarantee is only as good as this field.
**Do:** if the paper's `study_type` is `review`, set `is_own_result: false` on
**every** row from it, unconditionally, and add `review_restatement` to `flags`.
There is no exception. A review reporting its own meta-analysis is a
`meta_analysis`, not a review, and `literature-search` classifies it as such —
that judgement is not yours to make here. And a review asserting X is not
independent evidence for X, no matter how confidently it asserts.

### Mechanism inferred from co-occurrence
Two entities in one sentence is not a relationship. *"We measured IL-6 and TNF-α
in treated animals"* states no link between them. *"IL-6 rose while TNF-α fell"*
states no link between them either. The pull to emit `IL-6 —regulates→ TNF-α` is
strong because the sentence looks like it contains a fact.
**Do:** require the sentence to contain the relationship, not just the operands.
If you cannot point at the verb that joins them, there is no row. If they
co-occur under the same treatment, the relationship each has is to the
*treatment*, and those are two separate rows. Co-occurrence with a directional
word you supplied yourself is fabrication with a real quote attached — the
quote verifies, so nothing downstream will catch it.

### Effect sizes lost in normalization
`says: "yes"` collapses *"reduced tumour volume by 3%"* and *"reduced tumour
volume by 80%"* into the same value, and collapses an `IC50` of 2 nM and 40 µM
into the same value. The schema has no magnitude field. That is a deliberate
limitation, and quietly accepting it turns a marginal effect into an
endorsement.
**Do:** choose the quote that *contains the number*. The quote is the only place
magnitude survives, so a sentence with `IC 50  values in the low µM range`
beats a cleaner sentence that says the conjugate worked. Add
`effect_size_dropped` to `flags` when the paper reported a magnitude that the
row cannot represent. And if the reported effect is statistically null or the
authors call it not significant, that is `says: "no_effect"`, not a small `yes`.

### Figure captions asserting what the body text hedges
Captions are written to be read alone and they overclaim: *"FIGURE 4 Compound 17
blocks integrin binding"* sitting above a Results paragraph that says *"compound
17 appeared to reduce binding, though the difference did not reach
significance."* Captions are also inline in `content.lines`, so a grep hit gives
no visual signal that you are in a caption.
**Do:** treat a line starting `FIGURE n` or `TABLE n` as `section:
"figure_caption"`. When a caption and the body disagree, **the body wins** —
emit the body's sentence with the body's hedging. Emit the caption as its own
row only when the body is silent, and then flag it `figure_caption_only`. Never
take a caption's certainty and a body sentence's detail and combine them.

### Methods conditions not matching Results claims
The Methods say HEK293 at 37 °C; the Results sentence says *"binding was
observed"* with no conditions; the temptation is to fill `where` from Methods.
Papers run several conditions and report them in different Results paragraphs —
the Methods list is the union, not the assignment. A `where` value imported from
Methods can attach the wrong condition to the claim, and then
`explain_disagreement` reports a boundary condition that does not exist, which
is worse than reporting none.
**Do:** `where` comes from the sentence you quoted or from its own paragraph.
Never from Methods, never from the abstract's framing. Unstated → `null` plus
`conditions_unstated` in `flags`. A `null` costs one reconciliation; a wrong
value produces a confident false explanation.

### Quotes silently reformatted by the tool that fetched them
Verified against live Paperclip, and this drops more findings than any other
cause:

- **Search-result blurbs are not the abstract.** `paperclip search` prints a
  quoted paragraph under each hit. It is model-generated. For PMC9035832 the
  blurb begins *"A novel small-molecule drug conjugate targeting integrin αVβ3
  was synthesized…"*; that string appears nowhere in the paper. Quote it and the
  row is guaranteed to drop.
- **`map` output is a reader model's answer, not source text.** Same rule. `map`
  can also report `Map complete: 0/1 papers` with an `✗ Untitled` row and still
  print *"These per-paper answers are ready to use"* — a total failure that
  reads like a finished run. Check the `N/N` ratio; do not extract from a map
  that returned zero.
- **Italics and markup are stripped into spaces.** The corpus stores
  `α V β 3`, `IC 50`, and `  in vitro  ` with the spaces where the markup was.
  Writing the sensible `αVβ3` or `IC50` fails the substring test. Whitespace
  *runs* are normalized by `verify_quote`, so the double spaces are forgiven —
  the space between `α` and `V` is not, because it separates characters that you
  would otherwise have written adjacent.
- **Inline citations sit inside the sentence** as ` ( Smith et al., 2019 ; 
  Jones et al., 2021 )`. Deleting them to make the quote read cleanly breaks the
  substring. Keep them, and add `quote_spans_citation` to `flags`.
- **The same paper spells the same entity two ways.** PMC9035832's body writes
  `α V β 3` and its supplementary heading writes `αVβ3`.
  **Do:** copy from the fetched line, do not retype, and after copying re-grep
  the exact string you are about to emit —
  `grep -F "<your quote>" /papers/<id>/content.lines`. `-F` for literal. No hit,
  no row.

### Line anchors that point at the wrong line
Verified: the sentence *"Starting from an established tyrosine scaffold…"* is
`L157` in `content.lines` and `L121` in `sections/4 Conclusion.lines`. Line
numbers are **file-relative, not paper-global**, and grep against a section file
can return the same line twice.
**Do:** `line` is always the `content.lines` number. If you read a section file,
re-grep in `content.lines` to get the anchor. If the two disagree, `content.lines`
wins. A wrong anchor does not fail `verify_quote` — the quote still matches — so
this one ships silently.

### The temptation to paraphrase when the sentence is long
Biomedical sentences run 60 words and bury the assertion in the middle behind
three subordinate clauses. Trimming the front, dropping the parenthetical,
fixing the comma splice, joining the two halves that matter — every one of those
is the same action, and every one of them turns a substring into a non-substring.
The sentence being long is not a problem the schema has. `quote` has no length
limit.
**Do:** take the whole sentence. If the sentence is genuinely two assertions and
you only want one, look for a *shorter contiguous span* that still states it —
a clause is fine as long as it is copied unbroken. Never bridge a cut with an
ellipsis, never re-punctuate the ends, never capitalize a mid-sentence start.
And if the assertion truly cannot be quoted contiguously, **drop the finding**.
That is the correct outcome, it is counted in `no_quote_discarded`, and Stage 2
can see it. A finding whose quote you had to construct is worth less than no
finding, because the whole file's value is that its quotes were never
constructed.

### Direction reversed
`"A inhibits B"` and `"B is inhibited by A"` share every content word. Passive
voice, and the habit of reading the subject as whatever was named first, flip
`from` and `to` often. A reversed row is not caught by anything downstream: the
quote verifies, the entities resolve, and the link is simply backwards.
**Do:** name the actor before you write the row. If the sentence is passive,
rewrite it mentally into active voice and check that the actor is still
`from_name`. When the sentence genuinely does not establish direction, use
`associated_with` and flag `direction_from_context`.

### One sentence, several relationships, one row
*"Compound 17 bound αVβ3 but not α5β1, and inhibited adhesion in WM115 but not
M21-L cells"* is four findings: two `yes`, two `no` or `no_effect`, two distinct
`where` values. Emitting the first one loses the negative results — which are
the rows that make a link `disagreed` and drive the boundary-condition detector.
**Do:** one relationship per row, but the *same quote may back several rows*.
That is legal and expected. Split on the contrast words — `but not`, `whereas`,
`while no effect was observed`, `in contrast` — and give each row its own
`where`.

### Extracting nothing and calling it done
A pass that produces no rows because the papers were hard to read looks
identical downstream to a pass that produced no rows because the literature is
silent. The second is a finding; the first is a hole.
**Do:** if a paper yields no rows, that is fine — but check it was because the
paper asserts no relationship among the entities in play, not because you did
not grep for them. In Mode B, an empty return is only credible after you grepped
both entities and read the hits.

---

## Self-check before handing off

Run this over each batch, while the fetched text is still in front of you.
Anything that fails is deleted, not fixed.

1. `grep -F` the exact quote against `/papers/<id>/content.lines`. No hit → drop.
2. The quote is one contiguous span. No `…`, no `[…]`, no joined halves.
3. `line` was read from `content.lines`, not from a section file.
4. `paper_key` is the paper the quote actually came from — check this explicitly
   when the batch had five papers.
5. Both entities are named in the quote, or resolved from the same paragraph.
6. The relationship verb is present in the quote. Not implied by adjacency.
7. `is_own_result` is `false` for every row from a review, and for every row
   whose sentence carries a citation marker.
8. `where` came from the quoted sentence or its paragraph — never from Methods,
   never from the study type. Otherwise `null`.
9. `hedged` matches the sentence's own qualifiers.
10. Contrast words in the quote have been split into separate rows.

Report the number of claims you considered and dropped, so
`coverage.no_quote_discarded` is real rather than reconstructed.
