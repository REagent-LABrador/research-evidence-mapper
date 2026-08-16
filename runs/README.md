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

## Caveat

Produced on agent **v10**, so it predates the `delta` block (v11). For a round-1
graph `delta` is just "everything added", so nothing meaningful is missing — but
a round-2 artifact would be needed to exercise `links_changed` and
`gaps_resolved`.
