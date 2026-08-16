# Research Evidence Mapper — deliverables contract

Nothing gets built until this is agreed. Every row below is a thing someone can
pick up and own.

Contract for the data itself is `managed/research-evidence-mapper/SCHEMA.md` (input JSON,
output JSON, guarantees, memory layout). Not repeated here.

---

## 1. System I/O

| | |
|---|---|
| **In** | `{graph_id?, ask, target, depth, reason}` — `SCHEMA.md` §Request |
| **Out** | full graph JSON — `SCHEMA.md` §Output |
| **State** | `/mnt/memory/research-evidence-mapper/` — Stage 1 owns it, Stage 2 never sends a graph |
| **Invocation** | `message` mode, `session_policy: "fresh"` |

Request arrives as the task string. One ask per request, one round per request.

---

## 2. MCP — Paperclip only

> **Correction, 2026-08-15.** An earlier revision of this section claimed
> "Paperclip is a CLI, not an MCP server" and told you to pip-install it into the
> sandbox. **That was wrong — ignore it.** Paperclip does run a hosted MCP server;
> it was missed because the *local* install is a CLI and no `.mcp.json` exists in
> this repo. The original design in this section was right, and it is restored
> below. The sandbox needs no pip install and no wheel download.

**Endpoint:** `https://paperclip.gxl.ai/mcp` — remote streamable HTTP, so it goes
straight into `manifest.mcp_servers` with `permission: "always_allow"`.

Verified live: `initialize` returns 200 with
`serverInfo {name: "paperclip", version: "1.0.0"}`, protocol `2025-03-26`,
capabilities `{tools: {listChanged: false}}`.

**Auth:** `Authorization: Bearer <token>` — proven working with a Paperclip token.
The CLI also supports `X-API-Key` for API keys; **which header the MCP endpoint
accepts for a long-lived API key is not yet settled** and needs a real key to test.
The key rides in a platform credential vault, id in `manifest.vault_ids`.

**The server exposes exactly one tool**, and this shapes the whole design:

| | |
|---|---|
| name | `paperclip` |
| input | `{ command: string }` — one required property |
| example | `search "CRISPR" -n 5` · `search -s fda "pembrolizumab"` · `cat /papers/bio_xxx/meta.json` |

It is a passthrough: the server runs the Paperclip CLI on its own infrastructure
in `PAPERCLIP_MCP=1` mode, with a denylist of local-only commands (`login`,
`logout`, `setup`, `config`, `install`, `update`, …). So "calling the MCP" means
**constructing a CLI command string** — the tool surface is the CLI's surface.

Two consequences worth holding onto:

- **`literature-search` is still the only component that touches Paperclip.** The
  seam was designed to absorb an unknown tool surface; it now absorbs command-string
  construction instead. Unchanged in value.
- **Hosted MCP is stateless** — repo selection never carries across tool calls.
  Any `repo`/`git` command must name its repo explicitly every time, via
  `--repo <name>` or `-f <name>`. Bare repo commands fail by design rather than
  falling back to hidden state.

| capability | command |
|---|---|
| search | `search -s pmc,biorxiv,medrxiv "<query>"` |
| metadata | `cat /papers/<id>/meta.json` |
| full text | `cat /papers/<id>/content.lines` — line-numbered `L<n>:`, the quote anchors |
| sections | `ls /papers/<id>/sections/` |
| **figures** | `ls /papers/<id>/figures/` then `ask-image /papers/<id>/figures/<f> "<q>"` |
| **supplements** | `ls /papers/<id>/supplements/` then `head`/`cat` |
| tables | inline in `content.lines`; large ones under `supplements/` |

**Figures and supplements are NEVER read automatically.** `content.lines` does not
contain figure contents — `figures/` holds image files (`.jpg`/`.gif`) and the only
way into them is an explicit `ask-image` call, one per figure, with
`--fn describe` or `--fn extract-data`. An agent that does not call `ask-image` has
not looked at the figures, however much it read. This matters because a figure
routinely asserts an effect size the body text only hedges at, and because
dose/timepoint conditions — the `where` field that powers `explain_disagreement` —
are frequently only in the figure axes or a supplementary table.

**When to read them — settled 2026-08-15.** Figures are read on **`resolve_link` and
`test_gap` only**, and even then only for papers that already yielded a relevant
finding from text. On `new_question` and `expand_node` the agent does **not** open
figures.

The reason is budget, and it is not marginal: the MCP takes one command per call with
no shell loops, so figures cost one `ask-image` call each. An 8-figure paper is 8
extra calls; at `deep` (50 papers) that is potentially hundreds, on top of extraction.
Broad sweeps cannot afford it and targeted asks are exactly where a figure changes the
answer — `resolve_link` needs the conditions each camp measured under, and `test_gap`
needs to know whether a pair was actually tested or merely never mentioned.

When figures are skipped, that is **recorded in `coverage`, not silently omitted** —
so "we did not look" is never indistinguishable from "there was nothing there".

> **Scope, set 2026-08-15: Paperclip is the ONLY source of papers.** The earlier
> "fallback to Europe PMC REST via `web_fetch`" is **withdrawn**. No `web_fetch`,
> no direct REST, no other corpus. If Paperclip cannot supply a capability, that is
> a `coverage` fact to report — `status: "partial"` with a `stop_reason` — not a
> licence to go around it. A graph built partly from another source silently breaks
> the guarantee that every quote is verifiable against a Paperclip document id.

---

## 3. Skills — three

Each: frontmatter `name` + `description` (description states what it does **and
what it does not decide**), failure-modes section is the longest part.

### `literature-search`
- **In** ask type, target, depth · **Out** normalized `papers[]` + raw text per paper
- Owns: query construction per ask type, tier→budget, pagination, Paperclip seam, normalization
- Failure modes: page 1 is not the corpus · `quick` may never report absence · query variants returning the same set · relevance ranking ≠ quality · preprint vs published

### `claim-extraction`
- **In** papers + text · **Out** `findings[]` with verbatim quotes
- Two modes: abstract-batch (5/pass, broad asks) · full-text-targeted (`resolve_link`, `test_gap`)
- **Figures: `resolve_link` and `test_gap` only**, and only on papers already yielding
  a text finding. `ls figures/` then one `ask-image` per figure. Skips go in `coverage`.
- Failure modes: hedging read as assertion · background citation read as new result · mechanism inferred from co-occurrence · effect sizes lost in normalization · figure captions asserting what the text hedges · Methods conditions not matching Results claims

### `graph-assembly`
- **In** new findings + papers + prior graph · **Out** merged scored graph
- SKILL.md is thin — it says run `assemble.py`, what it does, what breaks
- Everything deterministic lives in the script, not prose. Arithmetic described in
  prose does not reproduce.

---

## 4. `assemble.py` — spec

Stdlib only. No classes; plain dicts and pure functions, so every piece is
testable alone.

```
main(prior_dir, new_findings, new_papers, round_n, ask) -> graph dict
```

**Identity + dedup**
```
normalize_doi(s)                     -> str        strip https://doi.org/, lowercase
paper_key(paper)                     -> str        doi > pmid > normalized title+year
dedupe_papers(new, existing)         -> merged, id_map
normalize_name(s)                    -> str        lowercase, strip punct, greek→latin, singularize
resolve_entities(new, existing)      -> merged, id_map
```
`resolve_entities` matches a normalized name against every existing name **and
alias across the whole graph**, not just this round. Unmatched → new node. The
model proposes merges upstream; the script only applies them, so it stays
deterministic.

**Integrity**
```
verify_quote(quote, source_text)     -> bool       normalized-whitespace substring
```
Called before a finding is written. False → dropped, `no_quote_discarded += 1`.
This is the guarantee the whole system rests on; it must be a check, not a prompt.

**Scoring**
```
evidence_quality(findings, papers)   -> float      mean of study_type table, ×0.8 preprint
agreement(yes, no)                   -> float      0.5 + (yes-no)/(2*(yes+no)); 1 source → 0.5
independence(findings, papers)       -> float      (distinct first_authors - 1)/(papers - 1)
score_link(findings, papers)         -> dict       0.4*agreement + 0.4*quality + 0.2*independence
link_state(yes, no, no_effect)       -> str        agreed|disagreed|single_source|no_effect
link_basis(findings)                 -> str        primary|hedged_only|background_only|mixed
```
Findings with `is_own_result: false` are excluded from `agreement` and
`independence` — one review restating 40 studies is one paper.

**The boundary-condition detector** (the demo moment)
```
explain_disagreement(yes_f, no_f)    -> str | None
```
Partition the two camps, compare `where` / `section` values. Disjoint non-empty
sets → `"conditions differ: {A} vs {B}"`. Otherwise `None`. Populates `links.why`.

**Gaps**
```
find_gaps(links, things, cap=50)     -> list
```
Open triangles: for each node B, each neighbour pair (A,C) with no A–C link → a
gap. Rank by `min(confidence of the two supporting links)`, truncate to `cap`.
Degree-capped to keep it near-linear; growth is quadratic otherwise.

**Rounds**
```
round_outcome(prior_links, new_links) -> str       new_evidence|nothing_new|promoted|contradicted
mark_changed(prior_links, new_links, round_n)      sets changed_in_round
save_state(graph, dir)                             splits at 80KB, findings/r<N>.json
load_state(dir)                                    reassembles; missing dir → empty graph
```

Verification for the script: run it twice on the same inputs, byte-identical
output. Non-determinism here silently corrupts every score.

---

## 5. Artifacts

| artifact | source or compiled |
|---|---|
| `SCHEMA.md`, `BUILD.md` | done |
| `CLAUDE.md` | source — role, 4 asks, pipeline, tiers, memory layout, JSON skeleton |
| 3 × `SKILL.md` | source |
| `assemble.py` | source, bundled in `graph-assembly/` |
| `fixtures/` ×3 | source — well-studied, sparse, genuinely disputed |
| `manifest.json` | compiled — sonnet-5, message, fresh, Paperclip, `memory` block |
| `acl.ts` | compiled — `{ public: true }` |
| `agent/tools/research-evidence-mapper.ts` | compiled — eve wrapper |
| `render.html` | deferred until real data exists |

---

## 6. Steps to a running agent

| # | step | done when |
|---|---|---|
| 0 | ~~Spike~~ — **DONE 2026-08-15** | all four checks answered; see "Spike result" below |
| 1 | fixtures | 3 questions, each with a stated reason it's in the set |
| 2 | `CLAUDE.md` + 3 skills + `assemble.py` | `assemble.py` twice-run byte-identical on a fixture |
| 3 | hand-run in session on largest fixture | output validates against `SCHEMA.md` |
| 4 | `/managed-agent-deploy research-evidence-mapper` | deploy succeeds |
| 5 | smoke, blocking | six facts below |
| 6 | render | deferred |

### Spike result — sandbox-capability-probe, 2026-08-15

| check | result |
|---|---|
| bundled script executes in sandbox | **yes** — skills materialize at `/workspace/skills/<name>/`, non-`SKILL.md` files included |
| `python3` present | **yes** — 3.11.15 at `/usr/local/bin/python3` |
| `/mnt/memory` writable | **yes** — mounts at `/mnt/memory/<store>/`, write→read roundtrip OK |
| Paperclip reachable | **yes** — egress to `paperclip.gxl.ai` confirmed 200 from the sandbox |

Also observed: `/mnt/session/outputs` exists · `curl`, `jq`, `pip`, `pip3`, `uv`
present · **`sqlite3` ABSENT** (already ruled out by design, now confirmed) ·
egress open to `ebi.ac.uk`, `paperclip.gxl.ai` and `pypi.org`.

The probe also pip-installed the Paperclip CLI into the sandbox and ran it
successfully. **That path is now moot** — it was explored while this contract
wrongly said no MCP existed. Keep it only as a fallback if the MCP endpoint is
ever unavailable; the MCP is the design.

One unknown remains: which auth header the MCP endpoint accepts for a long-lived
API key. `Authorization: Bearer <token>` is proven; `X-API-Key` is what the CLI
uses against the REST API and may or may not be accepted by the MCP. Needs a real
key to settle. Everything else is green, so steps 1–3 are no longer gated.

**Smoke facts** (`bun run console research-evidence-mapper -- --once "$(cat fixtures/q-disputed.txt)"`):
1. Paperclip called at event level, not claimed in prose
2. reply carries full graph JSON, not a summary
3. disputed fixture yields ≥1 `state: "disagreed"` — zero is failure, not cleanliness
4. a low-confidence finding survives into output
5. second request loads round 1 from memory, `round` increments
6. three quotes spot-checked verbatim against their DOIs

---

## 7. Open, non-blocking

- ~~Paperclip transport → decided on sight~~ → **settled**: remote streamable
  HTTP MCP at `https://paperclip.gxl.ai/mcp`. See §2. (An intermediate revision
  of this file said "CLI, not MCP" — that was wrong and is retracted.)
- ~~Bundled-script execution → spike step 0~~ → **settled**: works.
  `assemble.py` stays a bundled script; no host-side custom tool is needed, so
  `tools.ts` does not get built.
- **Paperclip API key — the one blocking item.** Needs minting from
  paperclip.gxl.ai. The local credentials are OAuth (`refresh_token` /
  `id_token`) and expire, so they cannot back a long-lived vault entry.
- `read_write` memory + fetched papers = injection surface. `CLAUDE.md` must state
  memory holds data, never instructions.
- `bun run console <name>` does **not** attach memory — test via `--once` only
