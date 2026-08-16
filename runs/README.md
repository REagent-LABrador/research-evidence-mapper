# runs/

One reference graph, produced by the **deployed** agent. Not uploaded with the
skills — this is output, kept so downstream consumers have a real artifact to
develop against instead of a hand-made one.

## `g_e087` — the disputed fixture

`fixtures/q-disputed.txt`, **two rounds**, `depth: standard`. Round 2 was a
`resolve_link` on `L21`, the disagreed link.

| | |
|---|---|
| things / papers / findings / links | 31 / 19 / 42 / 37 |
| disease node | present |
| UniProt accessions | `BACH1` O14867, `GST-pi` P09211 |
| `how` verbs | `activates`, `increases`, `decreases`, `suppresses`, `drives` — all in the closed set |
| `state: "disagreed"` links | 1 |
| findings carry `round` / `flags` | yes |
| rounds | 2 — `r1.json` and `r2.json`, one file each |

The disagreed link is the one worth looking at:

```
L21  Vitamin C --increases--> melanoma metastasis   state: disagreed
why: "conditions differ: {braf v600e-driven melanoma, mouse}
                     vs  {b16f0 melanoma, gulo ko mice}"
```

Two labs published opposing in vivo results, and the boundary is the mouse
model: **Gulo-knockout mice cannot synthesise vitamin C**, which is the actual
reason the results differ. All three quotes behind it were spot-checked by
re-resolving each paper **by DOI** — not by the id the agent used — and every
quote appears verbatim in what the DOI returns.

## Round 2 — what `delta` looks like in practice

```jsonc
"delta": { "round": 2, "links_changed": ["L21"],
           "findings_added": ["f1_1","f2_1"], "papers_added": ["p20"],
           "links_added": [], "things_added": [], "gaps_added": [], "gaps_resolved": [] }
```

```
L21  Vitamin C --increases--> melanoma metastasis
     state disagreed -> disagreed | confidence 0.64 -> 0.70
```

Worth reading closely. `resolve_link` moved **its target and nothing else** —
`links_added` and `things_added` are both empty, two findings from one new paper
landed on `L21`. Confidence rose while the state stayed `disagreed`: new
corroborating evidence strengthens the link without erasing the conflict, and
the boundary condition is unchanged because the new paper did not dissolve it.

`links_changed` is the field a consumer watches to learn that a
previously-settled relationship has been reopened and rescored. Note the delta
is one link, two findings and one paper against a 51KB full graph — which is the
argument for keeping the reply complete and the delta derived.

## What this replaces

The previous committed graph, `g_1a4f`, was **hand-assembled before deployment**.
It carried no disease entity, no accessions and no typed nodes, so consumers
developing against it had to supply their own assumptions — reported downstream
as seven `ASSUMED` overrides. It has been removed rather than kept alongside:
a stale reference artifact is worse than none, because it looks authoritative.


## `interpretability` — and what it says about this graph

Both files carry the required `interpretability` block. Because it is derived
rather than stored, it was added to `g_e087.json` with

```bash
python3 skills/graph-assembly/assemble.py --rebuild runs/g_e087.json \
    --source-dir <re-fetched text> --memory-dir runs --save --out runs/g_e087.json
```

The rebuild re-fetched each paper's full text from Paperclip (resolved by exact
title, `head -n 100000 /papers/<PMC>/content.lines`, `L<n>:` prefixes stripped)
and re-checked every quote. The result is honest rather than flattering:

| | |
|---|---|
| findings re-checked and matched | **35** of 44 |
| findings never checked (`null`) | **8** — 3 papers could not be re-resolved in the corpus by exact title |
| findings checked and **not** found | **1** — `f26` |
| papers with a `source_sha256` | 17 of 20 |
| `headline.result` / `status` | `EVIDENCE_MAPPED_DISPUTED` / `QUALIFIED` |

`f26` is worth understanding, because it is exactly what verification exists to
catch: its first sentence is verbatim in the paper, but the quote then continues
into a second sentence that appears ~21,000 characters later. It reads as one
contiguous passage and is not. It is flagged `QUOTE_MISMATCH` at severity ERROR
and **kept** — a rebuild must not silently change what a stored graph says.

The 9 unchecked findings are marked `quote_verified: null`, not `false`, and
their evidence grade is capped at `LOW`. `null` never reads as verified.

Two further quotes failed the first pass and turned out to be corpus rendering,
not bad quotes: Paperclip's `content.lines` emits `( Figure 5F )` where the
paper reads `(Figure 5F)`. The matcher now ignores whitespace on both sides.

## `g_minimal.json`

One real paper, two real findings, and a 398-character real excerpt as
`source_text` — enough to verify the quotes and no more, so no paper full text
is stored in this repository. Assembled offline from
[`../fixtures/round-minimal.json`](../fixtures/round-minimal.json):

```bash
python3 skills/graph-assembly/assemble.py --input fixtures/round-minimal.json \
    --out runs/g_minimal.json
```

`bun run validate` re-runs that command and asserts the output is byte-identical
to the committed file, so the example can never drift from its generator. It is
also the checked-in case where `quote_verified` is `true` throughout.

## Caveat

Produced on agent **v10**, so it predates the `delta` block (v11). For a round-1
graph `delta` is just "everything added", so nothing meaningful is missing — but
a round-2 artifact would be needed to exercise `links_changed` and
`gaps_resolved`.
