---
name: graph-assembly
description: Runs the bundled assemble.py to fold this round's findings and papers into the prior graph — paper dedup, entity resolution against the whole graph, link scoring, disagreement explanation, gap ranking, and the chunked write back to /mnt/memory. Use after claim-extraction has produced verbatim-quoted findings, once per round. It does NOT decide which papers to read, whether a claim is true, whether a disagreement is a real contradiction, or what to filter out — it computes, records, and applies merges proposed upstream. It removes exactly one class of row: a finding whose quote fails verbatim verification. All arithmetic lives in the script; this file never restates a formula, because prose arithmetic does not reproduce.
---

# graph-assembly

One round of evidence in, one full graph out. Everything deterministic is in
`assemble.py`. Read the script when you need to know *how* a number is produced —
do not reconstruct it from memory or from this file.

## Locate the script

Bundled files ship alongside this SKILL.md and materialize in the sandbox at
`/workspace/skills/<skill-name>/`. Resolve the path, never hardcode it blindly:

```bash
ASSEMBLE=""
for c in /workspace/skills/graph-assembly/assemble.py \
         /workspace/skills/*/assemble.py; do
  [ -f "$c" ] && ASSEMBLE="$c" && break
done
[ -n "$ASSEMBLE" ] || ASSEMBLE="$(find /workspace /mnt -maxdepth 5 -name assemble.py 2>/dev/null | head -1)"
echo "${ASSEMBLE:-NOT_FOUND}"
```

Resolve `python3` the same way: `command -v python3`, else `/usr/local/bin/python3`
(confirmed present, 3.11.x), else `command -v python`.

## Invoke

**One command per round. Do not write a driver script.**

```bash
python3 <path>/assemble.py \
  --input round.json \
  --memory-dir /mnt/memory/research-evidence-mapper \
  --save
```

`round.json` is everything this round produced. Write it once, then run the
command; the full graph comes back on stdout (or use `--out FILE`).

**Copy `example-round.json` from this skill's directory and fill it in.** It is
a complete, working round with every field annotated and the vocabularies
listed inline. Do not re-derive the shape by reading `assemble.py` — the example
is the contract, it is kept next to the script, and reading source to rebuild it
costs calls that belong to reading papers.

```bash
cp <skill-dir>/example-round.json /tmp/round.json   # then edit
```

```jsonc
{
  "graph_id": "g_7f2a",        // omit for new_question — a stable id is minted
  "question": "...",
  "round": 2,
  "ask": "new_question | expand_node | resolve_link | test_gap",
  "target": "L3",              // null for new_question
  "depth": "standard",
  "generated_at": "2026-08-16T02:05:00Z",
  "status": "ok",              // your judgement of the round; ok|empty|partial|failed
  "coverage": { "...": "the real numbers" },
  "things":   [ /* new/proposed entities */ ],
  "papers":   [ /* include "source_text" — it is used to verify quotes, then stripped */ ],
  "findings": [ /* every one with its verbatim quote */ ]
}
```

`--save` writes state back under `--memory-dir` and updates `index.json`.
Omit it to see what a round *would* produce without committing it.

**To read a graph, never re-issue its question.** Use the read-only paths:

```bash
python3 assemble.py --list --memory-dir /mnt/memory/research-evidence-mapper
python3 assemble.py --show g_e087 --memory-dir /mnt/memory/research-evidence-mapper
```

Re-issuing a question used to destroy the graph it was meant to show: the id is
deliberately stable per question, but the round loaded no prior state, so
`--save` wrote an empty assembly over several rounds of evidence. Fixed — a
`new_question` against an existing graph now loads it and continues its round
numbering — but reading via `--show` is still the right way, because it does not
assemble or write at all.

**Ids in `round.json` are LOCAL to that round.** Number your papers `p1, p2, …`
and your things `t1, t2, …` from one each time — they mean "the first paper I
found this round", not the `p1` already in storage. The assembler translates
them and never lets a round's ids reach stored rows.

That translation is the whole reason the rule is safe, and it was once broken:
this round's id map was being applied to stored findings too, repointing them at
whatever the incoming `p1` became. Their quotes were then checked against the
wrong paper's text, failed, and were discarded — the graph lost evidence while
`no_quote_discarded` made it look like quote hygiene working. Fixed, and covered
by `--selftest`. Do not try to avoid collisions by guessing storage's numbering;
that is the assembler's job.

Two behaviours worth relying on:

- **An extending ask against a graph that is not in memory returns
  `status: "failed"` and leaves memory untouched.** It does not silently start
  a new graph.
- **`new_question` mints its id from a hash of the question**, so a retried
  round rejoins the same graph instead of forking a second one for the same
  question.

Check the module itself with `python3 assemble.py --selftest` — it exercises
quote verification, dedupe, scoring, disagreement explanation, the
missing-directory case, and the twice-run byte-identical property.

## In / out

**In** — this round's findings (each carrying the fetched source text it was
extracted from, so quotes can be machine-verified), this round's papers, the prior
state directory, the round number, and the ask type.

**Out** — the complete graph per `SCHEMA.md` on stdout, and the state directory
written under `/mnt/memory/research-evidence-mapper/<graph_id>/` (`meta.json`,
`things.json`, `papers.json`, `links.json`, `gaps.json`, `findings/r<N>.json`).
The reply to Stage 2 is that full graph, never a delta and never a summary.

The script owns id assignment, dedup, entity merging, all four `links.confidence`
numbers, `state`, `basis`, `why`, gap ranking, `changed_in_round`, and the round
`outcome`. Do not compute, adjust, round, or "sanity-fix" any of these by hand.

## Determinism

Same inputs must give the same bytes. Check it, don't assume it:

```bash
"$PY" "$ASSEMBLE" ... > /tmp/a.json
"$PY" "$ASSEMBLE" ... > /tmp/b.json
diff <(grep -v generated_at /tmp/a.json) <(grep -v generated_at /tmp/b.json) && echo DETERMINISTIC
```

Run the second pass against a **copy** of the state directory, so the first pass's
write does not become the second pass's input. `generated_at` is wall clock and is
the only field allowed to differ. Any other diff is a bug in the script — report
it; a graph whose scores are not reproducible is worse than no graph, because
every downstream comparison silently lies.

## Scores are per (graph_id, round)

A `links.confidence.overall` is valid for exactly one `graph_id` + `round` pair.
**Never cache one across rounds and never carry one forward by hand.** A link at
0.81 dropping to 0.44 in round 2 is the system working: a contradicting paper
arrived and `agreement` fell. Diff on `changed_in_round`, and recompute by
re-running the script — never by editing a number.

## Failure modes

The longest section on purpose. Assembly fails quietly far more often than it
crashes, and a quiet failure produces a graph that looks fine and scores wrong.

**`assemble.py` not found.** The locate loop prints `NOT_FOUND`, or python reports
`No such file or directory`. **Report it and stop that stage** — set
`status: "partial"` (or `"failed"` if nothing can be produced), say plainly in the
reply that scoring did not run. Do **not** substitute prose: do not "estimate" a
confidence, do not eyeball agreement from finding counts, do not describe the
weighting in words and apply it mentally. Prose arithmetic does not reproduce, and
an unreproducible score is indistinguishable from a computed one once it is in the
JSON. Emitting findings and papers with **no** `links.confidence` block is a
smaller lie than emitting invented ones.

**`python3` absent or the wrong one.** `python3: command not found` on PATH does
not mean absent — try `/usr/local/bin/python3` before concluding anything. If a
`python` on PATH is 2.x it will fail with a `SyntaxError` on f-strings; that is a
wrong-interpreter symptom, not a broken script. Truly no interpreter → same rule
as above: report, never hand-compute.

**JSON passed through argv.** Quotes are the one thing this system guarantees, and
they contain apostrophes, quotation marks, dollar signs, backticks and non-ASCII.
Interpolating a findings blob into a shell argument mangles or truncates exactly
those characters, `verify_quote` then fails on the mangled copy, and the finding is
discarded as unverified — you lose real evidence and blame the extractor. Always
write the JSON to a file (heredoc with a quoted delimiter, or a `python3 -c` write)
and pass the path. Long payloads also hit argv length limits, which fails loudly at
best and truncates at worst.

**A findings chunk exceeds the memory file cap.** Memory files cap at 100KB; the
script chunks findings at ~80KB per round file. A single round with many long
quotes, or one runaway quote, can still push a chunk over — the symptom is a write
error, or worse a file that reads back truncated and parses as invalid JSON next
round. Check the written sizes after a large round
(`ls -l /mnt/memory/research-evidence-mapper/<gid>/findings/`). If a chunk is near the cap,
the cause is usually upstream: a quote that is a whole paragraph means
claim-extraction returned a passage instead of a sentence. Fix it there and re-run
the round. Never hand-split a chunk file — the script's reassembly expects its own
naming.

**`load_state` on a missing directory.** Must yield an **empty graph, not an
error** — this is the normal path for `new_question`. If it raises instead, that is
a script bug, report it. The dangerous inverse: an *extend* ask (`expand_node`,
`resolve_link`, `test_gap`) whose `--prior-dir` does not exist quietly starts from
empty, and you return a brand-new round-1 graph under a `graph_id` the caller
believes has history. Before any extend ask, confirm the directory exists and the
`graph_id` is in `index.json`. Unknown `graph_id` or unknown `target` is an
**error with no partial graph returned** — not an empty graph, not a fresh one.

**Non-determinism via set iteration.** The commonest source. Anything that builds
an ordered output from a Python `set` — alias lists, distinct first authors,
neighbour pairs feeding gap generation, dedup key collection — iterates in
hash order, which is randomized per process for strings. The graph then differs
between two runs on identical inputs: gaps appear in a different order, ties in
gap ranking resolve differently, and `changed_in_round` starts flagging links that
did not change. The twice-run diff above is what catches it. The fix is `sorted()`
at the point of materialization, inside the script. **Do not "fix" it by setting
`PYTHONHASHSEED`** — that masks the bug in your test and leaves it live in the
deployed agent, where the hash seed is not yours to control.

**`state: "disagreed"` read as a real contradiction.** Usually it is not. Two
findings pointing opposite ways most often come from different experimental
conditions — check `where` on both sides before the word "contradiction" appears
anywhere in the reply. That is exactly what `explain_disagreement` is for, and its
output lands in `links.why`. If `why` is populated ("conditions differ: …"), say
that and nothing stronger. If `why` is `null`, the detector found no clean split —
that means *unexplained*, not *proven conflict*; do not supply a reason of your own
invention to fill the field. Also check `basis`: a `disagreed` link whose opposing
side is `background_only` is a review restating someone else, not an independent
result, and reviews are already excluded from `agreement` for that reason.

**Scores moving between rounds reported as a bug.** They move. `agreement`,
`evidence_quality` and `independence` are all recomputed from the whole finding
set every round, so any new paper can move any link it touches, up or down, and
`changed_in_round` records it. This is the designed behavior — see the per-round
rule above. What *is* a bug: a score changing when **no** new finding touches that
link. That points at non-determinism, at a paper dedup that merged or un-merged
something, or at an entity resolution that split a node. Investigate in that order.

**Partial writes leaving state inconsistent.** `save_state` writes several files.
A crash, timeout, or a truncated chunk between them leaves `meta.json` claiming
round N while `links.json` still holds round N-1 — and the next round loads that
mixture as if it were coherent, producing scores computed over a half-updated
corpus. Symptoms: `meta.round` greater than the highest `round` value present in
`links.json`/`papers.json`, or a `findings/rN.json` that is missing or unparseable
while `meta.rounds[]` lists round N. Detect before trusting a load. Recovery is to
**re-run the whole round** from the same inputs — the script is deterministic, so a
re-run reproduces the intended state exactly. Do not hand-patch individual files to
make them agree; you will make them consistent and wrong.

**Entity resolution drift.** Merging is deliberately conservative: the model
proposes merges upstream, the script only applies them against the whole graph's
names and aliases. Two symptoms to watch after each round — the same concept
appearing as two `things` rows (mentions split, node too small, links that should
be one are two), and the opposite, an over-eager alias collapsing two genuinely
different entities into one node (mentions inflated, a link whose findings do not
actually talk about the same thing). Neither is fixed by editing `things.json`.
Fix the proposed aliases upstream and re-run the round.

**Gap list read as findings.** Gaps are proposals — open triangles nobody stated,
capped at 50 and ranked by their weaker supporting link, with confidence capped low
on purpose. A gap is not evidence of absence until `test_gap` has run and set
`searched_in_round`. An unsearched gap says "nobody in this sample connected these
two", nothing more, and at `quick` depth it does not even say that reliably.

**Silently empty output treated as success.** Zero links from a round with papers
read is a signal, not a result: quotes all failing verification (check
`coverage.no_quote_discarded` — a large number means the extractor is paraphrasing,
not quoting), findings referencing thing ids that no longer exist after merging, or
the findings file having been written but never passed. Reconcile
`coverage.read` / `used` against the row counts before returning.

## What this skill does not do

It does not search, does not read papers, does not judge truth, and does not
filter by score. Nothing is dropped for being weak — a hedged claim marks an
emerging area and a lone claim is a gap candidate. The only removal is a finding
whose quote does not verbatim-match its source text, and that removal is a
mechanical check inside the script, counted in `coverage.no_quote_discarded`.
