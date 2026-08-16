#!/usr/bin/env python3
"""Deterministic graph assembly for literature-graph.

Stdlib only. No classes. Pure functions, so each piece is testable alone.

DETERMINISM IS THE POINT. Every score in the output must be recomputable from
findings + papers, and two runs on the same input must be byte-identical. That
means: sort before emitting, never iterate a set into output, no time, no
randomness, and stable sort keys everywhere. Non-determinism here silently
corrupts every score in the graph.
"""

import json
import math
import os
import sys
import re
import unicodedata

# --------------------------------------------------------------------------
# identity + dedup
# --------------------------------------------------------------------------

_DOI_PREFIX = re.compile(r"^(https?://)?(dx\.)?doi\.org/", re.I)
_WS = re.compile(r"\s+")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)

# Greek letters get spelled out: "TNFα" and "TNF-alpha" are the same node.
_GREEK = {
    "α": "alpha", "β": "beta", "γ": "gamma", "δ": "delta",
    "κ": "kappa", "λ": "lambda", "μ": "mu", "σ": "sigma",
    "ω": "omega",
}


def normalize_doi(s):
    """Strip resolver prefix and lowercase. '' for anything falsy."""
    if not s:
        return ""
    return _DOI_PREFIX.sub("", str(s).strip()).strip("/").lower()


def paper_key(paper):
    """Identity for dedup: doi > pmid > normalized title+year.

    A preprint and its published version are one paper when they share a DOI or
    PMID; otherwise title+year catches the common case.
    """
    doi = normalize_doi(paper.get("doi"))
    if doi:
        return "doi:" + doi
    pmid = str(paper.get("pmid") or "").strip()
    if pmid:
        return "pmid:" + pmid
    title = normalize_name(paper.get("title") or "")
    year = str(paper.get("year") or "")
    return "ty:" + title + "|" + year


def dedupe_papers(new, existing, round_n=None):
    """Merge new papers into existing. Returns (merged, id_map).

    id_map maps the incoming paper's id -> the surviving id, so findings can be
    repointed. Existing rows keep their ids forever.
    """
    merged = [dict(p) for p in existing]
    by_key = {}
    for p in merged:
        by_key.setdefault(paper_key(p), p)
    used = {p.get("id") for p in merged}
    id_map = {}
    counter = len(merged)
    for p in new:
        k = paper_key(p)
        hit = by_key.get(k)
        if hit is not None:
            id_map[p.get("id")] = hit.get("id")
            # Fill blanks from the newcomer; never overwrite what we already had.
            for field, value in sorted(p.items()):
                if field in ("id",):
                    continue
                if not hit.get(field) and value:
                    hit[field] = value
            continue
        counter += 1
        new_id = "p%d" % counter
        while new_id in used:
            counter += 1
            new_id = "p%d" % counter
        used.add(new_id)
        row = dict(p)
        row["id"] = new_id
        if round_n is not None and "round" not in row:
            row["round"] = round_n
        id_map[p.get("id")] = new_id
        merged.append(row)
        by_key[k] = row
    return merged, id_map


def normalize_name(s):
    """lowercase, strip punctuation, greek->latin, collapse space, de-pluralize."""
    if not s:
        return ""
    raw = s
    s = unicodedata.normalize("NFKC", str(s))
    s = "".join(_GREEK.get(ch, ch) for ch in s)
    s = s.lower()
    s = _PUNCT.sub(" ", s)
    s = _WS.sub(" ", s).strip()
    # De-pluralize common nouns only. Applying it to symbols eats the trailing
    # letter of real names -- KRAS -> KRA, RAS -> RA -- and silently forks the
    # node it was meant to merge.
    if (len(s) > 3 and s.endswith("s") and not s.endswith("ss")
            and not _looks_like_symbol(raw)):
        s = s[:-1]
    return s


def _looks_like_symbol(raw):
    """Gene/protein symbols: all-caps, or containing digits (KRAS, IL6, TNFA)."""
    letters = [c for c in str(raw) if c.isalpha()]
    if not letters:
        return False
    return all(c.isupper() for c in letters) or any(c.isdigit() for c in str(raw))


def compact_name(s):
    """Separator-insensitive key: alphanumerics only.

    normalize_name turns punctuation into spaces, so "IL-6" becomes "il 6" and
    never matches "IL6" -- which made the merge example in this module's own
    docstring untrue: "K-Ras" and "KRAS" did not join. This is the secondary
    index that makes them.
    """
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", str(s))
    s = "".join(_GREEK.get(ch, ch) for ch in s)
    return "".join(ch for ch in s.lower() if ch.isalnum())


def _coalesce_by_accession(things):
    """Collapse nodes ALREADY in the graph that share a UniProt accession.

    Merging only new things into existing ones leaves prior fragmentation in
    place forever. A real graph had four nodes for one concept -- an adenoviral
    knockdown, two inhibitor compounds and a knockout mouse -- so evidence could
    never pool onto one link and every confidence in that neighbourhood was
    understated. Repairing that needs a pass over what is already stored, not
    just over what is arriving.

    The lowest-numbered id survives, so ids stay stable across rounds. Returns
    (things, id_map); callers must remap findings through id_map, which is what
    rebuilds the links.
    """
    def _order(t):
        i = str(t.get("id") or "")
        return (int(i[1:]) if i[1:].isdigit() else 10**9, i)

    by_acc = {}
    for t in sorted(things, key=_order):
        acc = _accession(t)
        if acc:
            by_acc.setdefault(acc, []).append(t)

    id_map, dropped = {}, set()
    for acc in sorted(by_acc):
        group = by_acc[acc]
        if len(group) < 2:
            continue
        keep = group[0]
        for other in group[1:]:
            id_map[other.get("id")] = keep.get("id")
            dropped.add(other.get("id"))
            labels = [other.get("name", "")] + list(other.get("aliases") or [])
            known = {normalize_name(a)
                     for a in [keep.get("name", "")] + list(keep.get("aliases") or [])}
            extra = sorted({a for a in labels if a and normalize_name(a) not in known})
            if extra:
                keep["aliases"] = sorted(set(list(keep.get("aliases") or []) + extra))
            keep["mentions"] = int(keep.get("mentions") or 0) + int(other.get("mentions") or 0)
            amb = sorted(set(list(keep.get("ambiguity") or [])
                             + list(other.get("ambiguity") or [])))
            if amb:
                keep["ambiguity"] = amb
            prov = list(keep.get("merged_from") or [])
            prov.append({"name": other.get("name"), "via": "accession"})
            keep["merged_from"] = sorted(prov, key=lambda m: (str(m.get("name")),
                                                              str(m.get("via"))))
    return [t for t in things if t.get("id") not in dropped], id_map


def _accession(t):
    """Normalized UniProt accession, or '' when absent or unusable.

    A node carrying a non-empty `ambiguity` list is deliberately excluded: an
    unresolved node must never become a merge key. Resolving the string "IL-6"
    yields P05231, the ligand, while receptor-blockade evidence is P08887 --
    merging on a contested accession propagates the wrong protein through every
    link that touches it.
    """
    if t.get("ambiguity"):
        return ""
    return str(t.get("uniprot_accession") or "").strip().upper()


def resolve_entities(new, existing, round_n=None):
    """Merge things against the WHOLE graph, accession first, then name/alias.

    Accession is the stronger key and is checked first, because names fragment
    in both directions. Four nodes -- KIC-0101, PF-06650833, adenovirus-mediated
    IRAK4 knockdown, IRAK4 kinase-deficient mice -- are one concept that no
    string comparison relates, while "IRAK4" alone matches three different
    UniProt entries across human, mouse and cow. An exact accession match is
    species-safe by construction: Q9NWZ3 and Q8R4K2 are simply different keys.

    The model proposes merges upstream (it is what knows a compound's target);
    this only applies them, so assembly stays deterministic.
    """
    merged, coalesced = _coalesce_by_accession([dict(t) for t in existing])
    id_map = {}
    index, acc_index = {}, {}
    for t in merged:
        for label in [t.get("name", "")] + list(t.get("aliases") or []):
            for key in (normalize_name(label), compact_name(label)):
                if key:
                    index.setdefault(key, t)
        acc = _accession(t)
        if acc:
            acc_index.setdefault(acc, t)

    used = {t.get("id") for t in merged}
    counter = len(merged)
    for t in new:
        labels = [t.get("name", "")] + list(t.get("aliases") or [])
        acc = _accession(t)
        hit = acc_index.get(acc) if acc else None
        via = "accession" if hit is not None else None
        if hit is None:
            for key in [k for lb in labels
                        for k in (normalize_name(lb), compact_name(lb)) if k]:
                if key in index:
                    cand = index[key]
                    # A contradicting accession OUTRANKS a name match. Human
                    # IRAK4 (Q9NWZ3) and mouse Irak4 (Q8R4K2) normalize to the
                    # same string, so without this the name fallback silently
                    # fuses two species into one node and pools their evidence.
                    cand_acc = _accession(cand)
                    if acc and cand_acc and acc != cand_acc:
                        continue
                    hit = cand
                    via = "name"
                    break
        if hit is not None:
            id_map[t.get("id")] = hit.get("id")
            known = {normalize_name(a)
                     for a in [hit.get("name", "")] + list(hit.get("aliases") or [])}
            extra = sorted({a for a in labels if a and normalize_name(a) not in known})
            if extra:
                hit["aliases"] = sorted(set(list(hit.get("aliases") or []) + extra))
            hit["mentions"] = int(hit.get("mentions") or 0) + int(t.get("mentions") or 1)
            # Carry identity forward, and keep every ambiguity ever raised --
            # a merge must never quietly resolve a question someone flagged.
            for field in ("uniprot_accession", "gene_symbol", "resolved_by"):
                if t.get(field) and not hit.get(field):
                    hit[field] = t[field]
            amb = sorted(set(list(hit.get("ambiguity") or [])
                             + list(t.get("ambiguity") or [])))
            if amb:
                hit["ambiguity"] = amb
            # Auditable, and reversible: a bad merge is otherwise unrecoverable.
            prov = list(hit.get("merged_from") or [])
            prov.append({"name": t.get("name"), "via": via})
            hit["merged_from"] = sorted(prov, key=lambda m: (str(m.get("name")),
                                                             str(m.get("via"))))
            for label in extra:
                for key in (normalize_name(label), compact_name(label)):
                    if key:
                        index.setdefault(key, hit)
            if acc:
                acc_index.setdefault(acc, hit)
            continue
        counter += 1
        new_id = "t%d" % counter
        while new_id in used:
            counter += 1
            new_id = "t%d" % counter
        used.add(new_id)
        row = dict(t)
        row["id"] = new_id
        row["mentions"] = int(t.get("mentions") or 1)
        if round_n is not None and "round" not in row:
            row["round"] = round_n
        row["aliases"] = sorted(set(row.get("aliases") or []))
        merged.append(row)
        id_map[t.get("id")] = new_id
        for label in labels:
            for key in (normalize_name(label), compact_name(label)):
                if key:
                    index.setdefault(key, row)
        if acc:
            acc_index.setdefault(acc, row)
    return merged, id_map, coalesced


# --------------------------------------------------------------------------
# integrity — the guarantee everything else rests on
# --------------------------------------------------------------------------

def verify_quote(quote, source_text):
    """Normalized-whitespace substring check. Mechanical, never a judgement.

    Unicode is NFKC-folded and the typographic characters that tools silently
    swap -- curly quotes, en/em dashes, non-breaking spaces -- are mapped to
    their ASCII forms first. Without that, a quote copied faithfully out of
    fetched text gets dropped over a character the reader never sees,
    which would mass-discard good findings for a cosmetic reason.
    """
    if not quote or not source_text:
        return False
    return _fold(quote) in _fold(source_text)


def _fold(s):
    s = unicodedata.normalize("NFKC", str(s))
    for a, b in (
        ("‘", "'"), ("’", "'"), ("“", '"'), ("”", '"'),
        ("–", "-"), ("—", "-"), ("−", "-"),
        (" ", " "), (" ", " "), (" ", " "), (" ", " "),
    ):
        s = s.replace(a, b)
    return _WS.sub(" ", s).strip().lower()


# --------------------------------------------------------------------------
# scoring
# --------------------------------------------------------------------------

# Keys are exactly the papers.study_type vocabulary. No synonyms: an unknown
# value scores as "unknown" rather than silently matching something close.
_STUDY_QUALITY = {
    "meta_analysis": 1.0,
    "clinical_trial": 0.9,
    "human_cohort": 0.8,
    "animal": 0.6,
    "test_tube": 0.5,
    "computational": 0.4,
    "review": 0.3,
    "unknown": 0.4,
}


def evidence_quality(findings, papers):
    """Mean of the study-type table, x0.8 for preprints."""
    by_id = {p.get("id"): p for p in papers}
    scores = []
    for f in findings:
        p = by_id.get(f.get("paper")) or {}
        base = _STUDY_QUALITY.get(str(p.get("study_type") or "unknown"), 0.4)
        if p.get("is_preprint"):
            base *= 0.8
        scores.append(base)
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores), 4)


def agreement(yes, no):
    """0.5 + (yes-no)/(2*(yes+no)). A single source is 0.5, never 1.0."""
    total = yes + no
    if total <= 0:
        return 0.0
    if total == 1:
        return 0.5
    return round(0.5 + (yes - no) / (2.0 * total), 4)


def independence(findings, papers):
    """(distinct first authors - 1) / (papers - 1). One group is not a consensus."""
    by_id = {p.get("id"): p for p in papers}
    ids, authors = set(), set()
    for f in findings:
        p = by_id.get(f.get("paper"))
        if not p:
            continue
        ids.add(p.get("id"))
        authors.add(normalize_name(p.get("first_author") or p.get("id") or ""))
    n = len(ids)
    if n <= 1:
        return 0.0
    return round((len(authors) - 1) / float(n - 1), 4)


def _own(findings):
    """Findings that are this paper's own result. A review restating forty
    studies is one paper, and not independent evidence for any of them."""
    return [f for f in findings if f.get("is_own_result")]


def score_link(findings, papers):
    """0.4*agreement + 0.4*evidence_quality + 0.2*independence."""
    own = _own(findings)
    yes = len([f for f in own if f.get("says") == "yes"])
    no = len([f for f in own if f.get("says") == "no"])
    agr = agreement(yes, no)
    qual = evidence_quality(findings, papers)
    ind = independence(own, papers)
    overall = 0.4 * agr + 0.4 * qual + 0.2 * ind
    return {
        "overall": round(overall, 4),
        "label": confidence_label(overall),
        "agreement": agr,
        "evidence_quality": qual,
        "independence": ind,
    }


def confidence_label(overall):
    """Bucket for humans. The number stays authoritative."""
    if overall >= 0.7:
        return "high"
    if overall >= 0.4:
        return "medium"
    return "low"


def link_state(yes, no, no_effect):
    if no_effect and not yes and not no:
        return "no_effect"
    if yes and no:
        return "disagreed"
    if (yes + no) == 1:
        return "single_source"
    if yes and not no:
        return "agreed"
    if no and not yes:
        return "agreed"
    return "single_source"


def link_basis(findings):
    if not findings:
        return "background_only"
    own = [f for f in findings if f.get("is_own_result")]
    hedged = [f for f in own if f.get("hedged")]
    if not own:
        return "background_only"
    if len(hedged) == len(own):
        return "hedged_only"
    if hedged:
        return "mixed"
    return "primary"


# --------------------------------------------------------------------------
# the boundary-condition detector -- the demo moment
# --------------------------------------------------------------------------

def explain_disagreement(yes_f, no_f):
    """Partition the camps and compare conditions.

    Disjoint, non-empty condition sets mean the two camps measured different
    things -- which is the common case and far more interesting than "they
    disagree". Overlapping sets mean a real contradiction, and we say nothing
    rather than invent a reason.
    """
    a = sorted({str(f.get("where")) for f in yes_f if f.get("where")})
    b = sorted({str(f.get("where")) for f in no_f if f.get("where")})
    if not a or not b:
        return None
    if set(a) & set(b):
        return None
    return "conditions differ: {%s} vs {%s}" % (", ".join(a), ", ".join(b))


# --------------------------------------------------------------------------
# gaps
# --------------------------------------------------------------------------

def _basis_weight(basis):
    """How much a supporting link's evidence type is worth to a gap.

    A gap implied by two papers' own results is a real hole in the literature.
    One implied by two background citations is an artifact of how introductions
    are written, and should not outrank it.
    """
    return {
        "primary": 1.0,
        "mixed": 0.85,
        "hedged_only": 0.6,
        "background_only": 0.35,
    }.get(basis, 0.6)


def _link_papers(link, findings_by_id):
    """Papers behind a link, for the independence check."""
    out = set()
    for key in ("yes", "no", "no_effect"):
        for fid in link.get(key) or []:
            f = findings_by_id.get(fid)
            if f and f.get("paper"):
                out.add(f["paper"])
    return out


_INTERVENTION_KINDS = ("small_molecule", "method")
_TARGET_KINDS = ("protein", "gene")


def interventions_without_target(things, links):
    """Intervention nodes carrying no edge to a protein or gene.

    An intervention that never says what it acts on cannot pool its evidence
    with anything. A real graph reached seven IRAK4-inhibitor nodes -- KIC-0101,
    PF-06650833, KT-474, ND2158, BI1543673, an adenoviral knockdown and a
    knockout mouse -- of which only two stated the target, so five sat isolated
    and every relationship they supported was scored as if it stood alone.

    Collapsing them into one node would be the wrong repair: for "what inhibits
    IRAK4?" seven distinct compounds IS the answer, and merging destroys it.
    The fix is the edge, not the merge -- with `X inhibits IRAK4` present,
    evidence pools along the mechanism path while the compounds stay distinct.

    Reported, never dropped. This is a coverage fact, like a gap.
    """
    kind = {t.get("id"): t.get("kind") for t in things}
    linked = set()
    for l in links:
        a, b = l.get("from"), l.get("to")
        if kind.get(a) in _INTERVENTION_KINDS and kind.get(b) in _TARGET_KINDS:
            linked.add(a)
        if kind.get(b) in _INTERVENTION_KINDS and kind.get(a) in _TARGET_KINDS:
            linked.add(b)
    return sorted(t.get("id") for t in things
                  if t.get("kind") in _INTERVENTION_KINDS
                  and t.get("id") not in linked)


def compute_delta(prior, graph, round_n):
    """What this round changed, derived from the graph rather than stored.

    The reply stays the FULL graph on purpose: one parser, no reassembly, and no
    ordering dependency, so a consumer that misses a round is not left holding a
    graph it cannot complete. This block just saves every consumer from
    recomputing the same diff.

    It is derived, never persisted separately. A delta file would be a second
    source of truth that can disagree with the graph, and a consumer would have
    to decide which one is right.

    Note what is NOT here: links and gaps are recomputed every round by design,
    because a link's confidence legitimately moves when new evidence arrives.
    `links_changed` reports that movement; it is not drift.
    """
    def ids(rows):
        return {r.get("id") for r in (rows or [])}

    prior_links = {l.get("id"): l for l in (prior.get("links") or [])}
    prior_gap_ids = ids(prior.get("gaps"))
    now_gap_ids = ids(graph.get("gaps"))

    added_links, changed_links = [], []
    for l in graph.get("links") or []:
        if l.get("id") not in prior_links:
            added_links.append(l.get("id"))
        elif l.get("changed_in_round") == round_n:
            changed_links.append(l.get("id"))

    return {
        "round": round_n,
        "things_added": sorted(ids(graph.get("things")) - ids(prior.get("things"))),
        "papers_added": sorted(ids(graph.get("papers")) - ids(prior.get("papers"))),
        "findings_added": sorted(f.get("id") for f in (graph.get("findings") or [])
                                 if f.get("round") == round_n),
        "links_added": sorted(added_links),
        "links_changed": sorted(changed_links),
        "gaps_added": sorted(now_gap_ids - prior_gap_ids),
        "gaps_resolved": sorted(prior_gap_ids - now_gap_ids),
    }


def confidence_profile(findings):
    """Report the spread of self-reported confidence, and the contradictions.

    findings.confidence is never used to filter -- the only removals are a
    failed quote match and a content duplicate. But a scale nobody uses is a
    scale that carries nothing: one real run produced 38 findings spanning
    0.75-0.9 and nothing lower, so Stage 2 had no speculative leads to explore
    and no way to tell a quantified result from a hedge.

    `hedged_but_confident` is the mechanically checkable half: a finding marked
    hedged while scoring above 0.65 has two fields disagreeing about the same
    sentence. Reported, never silently corrected -- the model's own judgement
    stays in the graph.
    """
    vals = [f.get("confidence") for f in findings
            if isinstance(f.get("confidence"), (int, float))]
    contradictions = sorted(
        f.get("id") for f in findings
        if f.get("hedged") and isinstance(f.get("confidence"), (int, float))
        and f["confidence"] > 0.65
    )
    profile = {
        "n": len(vals),
        "min": round(min(vals), 4) if vals else None,
        "max": round(max(vals), 4) if vals else None,
        "below_0_65": len([v for v in vals if v < 0.65]),
        "hedged_but_confident": contradictions,
    }
    return profile


def proteins_without_accession(things):
    """Protein/gene nodes carrying no UniProt accession.

    The accession is what a downstream consumer keys on; without it the node is
    a string. Across seven real graphs coverage was 0,0,0,0,1,1,2 nodes, and the
    tractability bridge had to recover identity from the question text instead.
    Reported, never invented.
    """
    return sorted(t.get("id") for t in things
                  if t.get("kind") in _TARGET_KINDS
                  and not str(t.get("uniprot_accession") or "").strip())


def has_disease_node(things):
    """A graph about a disease should say so in things[], not only in the
    question string."""
    return any(t.get("kind") == "disease" for t in things)


def find_gaps(links, things, cap=50, prior_gaps=None, searched_pair=None,
              round_n=None, findings=None):
    """Open triangles: A-B and B-C exist, A-C does not.

    Ranking is the whole problem here. Scoring a gap by the weaker supporting
    link alone produces mass ties -- every single_source link scores agreement
    0.5, so min() of two of them lands on the same handful of values, and a
    22-link graph yielded 38 gaps sharing 6 distinct scores. Ties make
    best-first expansion arbitrary, so the score has to carry signal that link
    confidence does not:

      base   weakest supporting link, as before
      qual   basis of BOTH links -- two background citations are not a hole
      indep  do the two links rest on different papers, or is this one paper's
             own framing showing up twice
      hub    a node of degree d spawns C(d,2) candidate gaps, all structurally
             alike; without a penalty one hub floods the ranking
      routes the same missing pair implied through several different
             intermediates is stronger evidence the edge should exist
    """
    findings_by_id = {f.get("id"): f for f in (findings or [])}
    by_id = {l.get("id"): l for l in links}

    present = set()
    for l in links:
        present.add((l.get("from"), l.get("to")))
        present.add((l.get("to"), l.get("from")))

    neighbours, conf, via_link = {}, {}, {}
    for l in links:
        a, b = l.get("from"), l.get("to")
        c = (l.get("confidence") or {}).get("overall", 0.0)
        neighbours.setdefault(a, set()).add(b)
        neighbours.setdefault(b, set()).add(a)
        if c >= conf.get((a, b), -1.0):
            via_link[(a, b)] = l.get("id")
            via_link[(b, a)] = l.get("id")
        conf[(a, b)] = max(conf.get((a, b), 0.0), c)
        conf[(b, a)] = conf[(a, b)]

    # Enumerate routes first so the hub penalty can see how many each hub spawns.
    routes = []
    for b in sorted(neighbours):
        nbrs = sorted(neighbours[b])
        if len(nbrs) > 24:                       # degree cap keeps this near-linear
            nbrs = nbrs[:24]
        spawned = []
        for i in range(len(nbrs)):
            for j in range(i + 1, len(nbrs)):
                a, c = nbrs[i], nbrs[j]
                if (a, c) in present:
                    continue
                spawned.append((a, c))
        penalty = 1.0 / math.sqrt(len(spawned)) if spawned else 1.0
        for a, c in spawned:
            ab, bc = via_link.get((a, b)), via_link.get((b, c))
            base = min(conf.get((a, b), 0.0), conf.get((b, c), 0.0))
            qual = _basis_weight((by_id.get(ab) or {}).get("basis")) * _basis_weight(
                (by_id.get(bc) or {}).get("basis")
            )
            pa = _link_papers(by_id.get(ab) or {}, findings_by_id)
            pc = _link_papers(by_id.get(bc) or {}, findings_by_id)
            indep = 1.0 if (pa and pc and not (pa & pc)) else 0.65
            routes.append({
                "pair": tuple(sorted((a, c))),
                "via": b,
                "implied_by": sorted({ab, bc} - {None}),
                "score": base * qual * indep * penalty,
            })

    best, route_count = {}, {}
    for r in routes:
        k = r["pair"]
        route_count[k] = route_count.get(k, 0) + 1
        if k not in best or r["score"] > best[k]["score"]:
            best[k] = r
    for k, r in best.items():
        # Several independent intermediates implying the same missing edge is a
        # stronger signal than one; log-scaled so it nudges rather than dominates.
        r["score"] *= 1.0 + 0.12 * math.log2(route_count[k])
        r["routes"] = route_count[k]

    ranked = sorted(best.values(),
                    key=lambda g: (-g["score"], g["pair"][0], g["pair"][1]))[:cap]

    # Gap ids must be stable across rounds, because test_gap targets one BY ID.
    prior_by_pair, used = {}, set()
    for g in (prior_gaps or []):
        prior_by_pair[tuple(sorted(g.get("missing") or []))] = g
        used.add(g.get("id"))

    gaps, counter = [], 0
    for g in ranked:
        pair = tuple(sorted(g["pair"]))
        old = prior_by_pair.get(pair)
        if old is not None:
            gid, searched = old.get("id"), old.get("searched_in_round")
        else:
            counter += 1
            gid = "g%d" % counter
            while gid in used:
                counter += 1
                gid = "g%d" % counter
            searched = None
        used.add(gid)
        if searched_pair and pair == tuple(sorted(searched_pair)):
            searched = round_n
        note = "both connect to %s; no direct link reported" % g["via"]
        if g["routes"] > 1:
            note += " (implied via %d intermediates)" % g["routes"]
        gaps.append({
            "id": gid,
            "missing": [pair[0], pair[1]],
            "implied_by": g["implied_by"],
            "note": note,
            "confidence": round(min(g["score"], 0.6), 4),   # a gap is a proposal
            "searched_in_round": searched,
        })
    gaps.sort(key=lambda x: (int(x["id"][1:]) if x["id"][1:].isdigit() else 0, x["id"]))
    return gaps


# --------------------------------------------------------------------------
# rounds
# --------------------------------------------------------------------------

def round_outcome(prior_links, new_links):
    if not prior_links and new_links:
        return "new_evidence"
    prior = {l.get("id"): l for l in prior_links}
    changed = promoted = contradicted = False
    for l in new_links:
        p = prior.get(l.get("id"))
        if p is None:
            changed = True
            continue
        if l.get("state") != p.get("state"):
            changed = True
            if p.get("state") == "single_source" and l.get("state") == "agreed":
                promoted = True
            if l.get("state") == "disagreed":
                contradicted = True
        elif (l.get("confidence") or {}).get("overall") != (p.get("confidence") or {}).get("overall"):
            changed = True
    if contradicted:
        return "contradicted"
    if promoted:
        return "promoted"
    return "new_evidence" if changed else "nothing_new"


def mark_changed(prior_links, new_links, round_n):
    prior = {l.get("id"): l for l in prior_links}
    for l in new_links:
        p = prior.get(l.get("id"))
        if p is None or l.get("state") != p.get("state") or \
           (l.get("confidence") or {}).get("overall") != (p.get("confidence") or {}).get("overall"):
            l["changed_in_round"] = round_n
        else:
            l["changed_in_round"] = p.get("changed_in_round")
    return new_links


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

_CHUNK = 80 * 1024


def _dump(obj):
    """One serializer everywhere: sorted keys, stable separators, trailing NL."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def save_state(graph, dir_path):
    os.makedirs(os.path.join(dir_path, "findings"), exist_ok=True)
    for name in ("things", "papers", "links", "gaps"):
        with open(os.path.join(dir_path, name + ".json"), "w", encoding="utf8") as fh:
            fh.write(_dump(graph.get(name, [])))
    meta = {k: graph.get(k) for k in
            ("schema_version", "graph_id", "question", "round", "status",
             "generated_at", "error", "coverage", "rounds")}
    with open(os.path.join(dir_path, "meta.json"), "w", encoding="utf8") as fh:
        fh.write(_dump(meta))
    rnd = graph.get("round", 1)
    # SCHEMA.md promises findings/r<N>.json is APPENDED per round. Writing every
    # finding into every round's file made each file a full snapshot, so a
    # reload counted prior findings once per round and produced fictitious
    # duplicates_dropped figures.
    #
    # But filing only the CURRENT round's findings silently drops the rest when
    # the earlier files are not already on disk -- saving a whole graph in one
    # call loses every prior round. Caught by a regression test that constructed
    # exactly that state. So each finding is filed under ITS OWN round: correct
    # incrementally AND when a complete graph is written in one go.
    by_round = {}
    for f in graph.get("findings", []):
        by_round.setdefault(f.get("round", rnd), []).append(f)
    for r_n in sorted(by_round, key=lambda x: (str(type(x)), x)):
        findings = by_round[r_n]
        blob = _dump(findings)
        if len(blob.encode("utf8")) <= _CHUNK:
            parts = [findings]
        else:                   # chunk; a single memory file caps at 100KB
            parts, cur, size = [], [], 0
            for f in findings:
                sz = len(_dump(f).encode("utf8"))
                if cur and size + sz > _CHUNK:
                    parts.append(cur)
                    cur, size = [], 0
                cur.append(f)
                size += sz
            if cur:
                parts.append(cur)
        for i, part in enumerate(parts):
            suffix = "" if i == 0 else "_%d" % (i + 1)
            path = os.path.join(dir_path, "findings", "r%s%s.json" % (r_n, suffix))
            with open(path, "w", encoding="utf8") as fh:
                fh.write(_dump(part))
    return dir_path


def load_state(dir_path):
    """A missing directory is an empty graph, never an exception."""
    empty = {"things": [], "papers": [], "links": [], "gaps": [], "findings": [],
             "round": 0, "rounds": [], "status": "empty"}
    if not dir_path or not os.path.isdir(dir_path):
        return empty
    out = dict(empty)
    meta_path = os.path.join(dir_path, "meta.json")
    if os.path.isfile(meta_path):
        with open(meta_path, encoding="utf8") as fh:
            out.update(json.load(fh))
    for name in ("things", "papers", "links", "gaps"):
        p = os.path.join(dir_path, name + ".json")
        if os.path.isfile(p):
            with open(p, encoding="utf8") as fh:
                out[name] = json.load(fh)
    fdir = os.path.join(dir_path, "findings")
    findings = []
    if os.path.isdir(fdir):
        for fn in sorted(os.listdir(fdir)):        # sorted: determinism
            if fn.endswith(".json"):
                with open(os.path.join(fdir, fn), encoding="utf8") as fh:
                    findings.extend(json.load(fh))
    out["findings"] = findings
    return out


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main(prior_dir, new_findings, new_papers, round_n, ask, question=None,
         graph_id=None, generated_at=None, coverage=None, new_things=None,
         target=None, depth=None, papers_added=None):
    """target: for test_gap, the gap id being tested -- resolved to its pair so
    the answer survives the gap being re-ranked."""
    prior = load_state(prior_dir)

    papers, pmap = dedupe_papers(new_papers or [], prior.get("papers") or [],
                                 round_n=round_n)
    things, tmap_new, tmap_existing = resolve_entities(
        new_things or [], prior.get("things") or [], round_n=round_n)

    kept, discarded, duplicates = [], 0, 0
    src = {p.get("id"): p for p in papers}
    seen_content, seen_ids = {}, set()
    # A round's ids are LOCAL to that round. A bundle naming its first paper
    # "p1" means "the first paper I found", not the p1 already in storage --
    # and the shipped example uses exactly those ids, so collisions are the
    # normal case, not an edge case.
    #
    # Applying this round's id map to STORED findings repoints them at whatever
    # the incoming p1 became. Their quotes are then checked against the wrong
    # paper's text, fail, and are discarded as unverifiable. The graph loses
    # evidence and the coverage counter reports it as quote hygiene working.
    # Reproduced: 2 findings in, 1 out, 1 silently dropped.
    #
    # So: stored findings already point at stored ids and take only the
    # coalesce remap. Incoming findings take this round's map.
    staged = ([(f, False) for f in (prior.get("findings") or [])]
              + [(f, True) for f in (new_findings or [])])
    for f, is_new in staged:
        f = dict(f)
        if is_new:
            f["paper"] = pmap.get(f.get("paper"), f.get("paper"))
        text = (src.get(f.get("paper")) or {}).get("source_text", "")
        if text and not verify_quote(f.get("quote"), text):
            discarded += 1
            continue
        remap = tmap_new if is_new else tmap_existing
        for side in ("from", "to"):
            if f.get(side) in remap:
                f[side] = remap[f[side]]

        # Dedupe by CONTENT, not by id. A round that is retried -- after a
        # timeout, a dropped stream, a re-run -- re-extracts the same sentences
        # from the same papers. Appending them again would double every
        # affected link's yes/no counts, which inflates `agreement` and
        # `independence` and quietly corrupts confidence. The prior copy wins so
        # ids and round numbers stay stable.
        key = (
            f.get("paper"), f.get("from"), f.get("how"), f.get("to"),
            f.get("says"), _fold(f.get("quote") or ""),
        )
        if key in seen_content:
            duplicates += 1
            continue
        # An id collision with DIFFERENT content is a different problem: two
        # rows sharing an id would break "every id resolves". Rename the newcomer.
        fid = f.get("id")
        if fid in seen_ids:
            n = 1
            while "%s_%d" % (fid, n) in seen_ids:
                n += 1
            fid = "%s_%d" % (fid, n)
            f["id"] = fid
        seen_ids.add(fid)
        seen_content[key] = fid
        # Schema drift found downstream: findings and papers carried no `round`,
        # so a consumer could not tell when evidence entered the graph and
        # save_state could not tell which findings a round owns.
        if "round" not in f:
            f["round"] = round_n if is_new else prior.get("round", 1)
        f.setdefault("flags", [])
        kept.append(f)
    kept.sort(key=lambda f: (str(f.get("from")), str(f.get("to")), str(f.get("paper")), str(f.get("quote"))[:80]))

    grouped = {}
    for f in kept:
        grouped.setdefault((f.get("from"), f.get("how"), f.get("to")), []).append(f)

    links = []
    for n, key in enumerate(sorted(grouped, key=lambda k: tuple(str(x) for x in k)), 1):
        fs = grouped[key]
        own = _own(fs)
        yes = [f for f in own if f.get("says") == "yes"]
        no = [f for f in own if f.get("says") == "no"]
        ne = [f for f in own if f.get("says") == "no_effect"]
        state = link_state(len(yes), len(no), len(ne))
        links.append({
            "id": "L%d" % n,
            "from": key[0], "how": key[1], "to": key[2],
            "state": state,
            "basis": link_basis(fs),
            "confidence": score_link(fs, papers),
            "yes": sorted(f.get("id") for f in yes if f.get("id")),
            "no": sorted(f.get("id") for f in no if f.get("id")),
            "no_effect": sorted(f.get("id") for f in ne if f.get("id")),
            "why": explain_disagreement(yes, no) if state == "disagreed" else None,
            "changed_in_round": None,
        })

    links = mark_changed(prior.get("links") or [], links, round_n)

    searched_pair = None
    if ask == "test_gap" and target:
        for g in (prior.get("gaps") or []):
            if g.get("id") == target:
                searched_pair = tuple(sorted(g.get("missing") or []))
                break
    gaps = find_gaps(links, things, prior_gaps=prior.get("gaps") or [],
                     searched_pair=searched_pair, round_n=round_n,
                     findings=kept)
    outcome = round_outcome(prior.get("links") or [], links)

    cov = dict(coverage or {})
    cov["no_quote_discarded"] = cov.get("no_quote_discarded", 0) + discarded
    orphans = interventions_without_target(things, links)
    cov["interventions_without_target"] = orphans
    cov["interventions_without_target_count"] = len(orphans)
    unresolved = proteins_without_accession(things)
    cov["proteins_without_accession"] = unresolved
    cov["proteins_without_accession_count"] = len(unresolved)
    cov["has_disease_node"] = has_disease_node(things)
    cov["confidence_profile"] = confidence_profile(kept)
    if duplicates:
        cov["duplicates_dropped"] = cov.get("duplicates_dropped", 0) + duplicates

    rounds = list(prior.get("rounds") or [])
    rounds.append({
        "n": round_n, "ask": ask, "target": target, "depth": depth,
        "papers_added": papers_added if papers_added is not None else len(new_papers or []),
        "outcome": outcome,
    })

    graph = {
        "schema_version": "1.1",
        "graph_id": graph_id or prior.get("graph_id"),
        "question": question or prior.get("question"),
        "round": round_n,
        "status": prior.get("status", "ok"),
        "generated_at": generated_at,
        "error": None,
        "things": things,
        "papers": papers,
        "findings": kept,
        "links": links,
        "gaps": gaps,
        "coverage": cov,
        "rounds": rounds,
    }
    graph["delta"] = compute_delta(prior, graph, round_n)
    return graph


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
#
# One command per round. Without this the caller has to hand-author a driver
# script every round to marshal findings into main(), which costs more tool
# calls than the extraction it exists to serve -- and worse, it puts freshly
# written, unreviewed glue in front of a core whose entire value is that it is
# deterministic. A byte-stable script reached through a different caller each
# time is not reproducible.
#
#   python3 assemble.py --input round.json --memory-dir /mnt/memory/<node> --save
#
# round.json carries everything for this round:
#   {"graph_id": "g_7f2a",        // omit for new_question; a fresh id is minted
#    "question": "...", "round": 2, "ask": "resolve_link", "target": "L3",
#    "depth": "standard", "generated_at": "...", "status": "ok",
#    "coverage": {...}, "things": [...], "papers": [...], "findings": [...]}
#
# papers[] may carry "source_text"; it is used for quote verification and
# stripped from the output. The full graph goes to stdout (or --out FILE).


def _mint_graph_id(memory_dir, question):
    """Deterministic id from the question, so a retried new_question round does
    not create a second graph for the same question."""
    import hashlib as _h
    base = _h.sha256(("q:" + (question or "")).encode("utf8")).hexdigest()[:4]
    index = {}
    idx_path = os.path.join(memory_dir or "", "index.json")
    if memory_dir and os.path.isfile(idx_path):
        with open(idx_path, encoding="utf8") as fh:
            index = json.load(fh)
    gid = "g_" + base
    n = 0
    while gid in index and index[gid].get("question") != question:
        n += 1
        gid = "g_" + _h.sha256(("q:%s:%d" % (question, n)).encode("utf8")).hexdigest()[:4]
    return gid


def cli(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Assemble one round into the graph.")
    ap.add_argument("--input", help="round bundle JSON (see header)")
    ap.add_argument("--memory-dir", default=None,
                    help="e.g. /mnt/memory/research-evidence-mapper")
    ap.add_argument("--out", default="-", help="write graph here ('-' = stdout)")
    ap.add_argument("--save", action="store_true",
                    help="write state back and update index.json")
    ap.add_argument("--selftest", action="store_true")
    ap.add_argument("--show", metavar="GRAPH_ID",
                    help="print a stored graph and exit. Read-only: never "
                         "assembles, never writes. Use this instead of "
                         "re-issuing a question to see its graph.")
    ap.add_argument("--list", action="store_true",
                    help="list stored graph ids and exit")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.list:
        idx = os.path.join(a.memory_dir or "", "index.json")
        _emit(json.load(open(idx, encoding="utf8")) if os.path.isfile(idx) else {}, a.out)
        return 0
    if a.show:
        if not a.memory_dir:
            raise SystemExit("--show needs --memory-dir")
        d = os.path.join(a.memory_dir, a.show)
        if not os.path.isdir(d):
            _emit({"error": "graph %r not found" % a.show, "status": "failed"}, a.out)
            return 1
        g = load_state(d)
        g["graph_id"] = a.show
        _emit(g, a.out)
        return 0
    if not a.input:
        ap.error("--input is required (or --selftest)")

    with open(a.input, encoding="utf8") as fh:
        r = json.load(fh)

    ask = r.get("ask") or "new_question"
    gid = r.get("graph_id")
    if ask == "new_question" or not gid:
        gid = gid or _mint_graph_id(a.memory_dir, r.get("question"))
        # _mint_graph_id is deliberately stable for a given question, so a
        # retried new_question rejoins its graph instead of forking one. That
        # only holds if the round also LOADS what is there: with prior_dir None
        # the round assembles from empty and --save writes that over a graph
        # that may already have several rounds of evidence in it. The stable id
        # then points at a graph that just lost its history.
        #
        # Reproduced: round 1, round 2 extending it, then the same question
        # re-issued -> 2 findings back to 1, on disk. So a new_question against
        # an EXISTING graph loads it like any other round; only a genuinely new
        # graph starts empty.
        candidate = os.path.join(a.memory_dir, gid) if a.memory_dir else None
        prior_dir = candidate if candidate and os.path.isdir(candidate) else None
        if prior_dir:
            # Round numbering must continue from what is stored, or the round
            # would overwrite an existing findings/r<N>.json.
            stored_round = load_state(prior_dir).get("round") or 0
            if r.get("round", 1) <= stored_round:
                r["round"] = stored_round + 1
    else:
        prior_dir = os.path.join(a.memory_dir, gid) if a.memory_dir else None
        if prior_dir and not os.path.isdir(prior_dir):
            # An extending ask against a graph that is not in memory is a
            # failed round, not an empty one -- do not silently start over.
            graph = {
                "schema_version": "1.1", "graph_id": gid,
                "question": r.get("question"), "round": r.get("round", 0),
                "status": "failed",
                "generated_at": r.get("generated_at"),
                "error": "graph_id %r not found in memory; nothing to extend" % gid,
                "rounds": [], "coverage": dict(r.get("coverage") or {}),
                "things": [], "papers": [], "findings": [], "links": [], "gaps": [],
            }
            _emit(graph, a.out)
            return 0

    graph = main(
        prior_dir=prior_dir,
        new_findings=r.get("findings") or [],
        new_papers=r.get("papers") or [],
        new_things=r.get("things") or [],
        round_n=r.get("round", 1),
        ask=ask,
        question=r.get("question"),
        graph_id=gid,
        generated_at=r.get("generated_at"),
        coverage=r.get("coverage") or {},
        target=r.get("target"),
        depth=r.get("depth"),
        papers_added=len(r.get("papers") or []),
    )
    if r.get("status"):
        graph["status"] = r["status"]
    # Carry the request's time window and any defaults the caller had to
    # substitute, so a consumer can tell a windowed absence from a real one and
    # can see which fields were not actually supplied.
    if "years" in r or "years" not in graph.get("coverage", {}):
        graph.setdefault("coverage", {})["years"] = r.get("years")
    if r.get("defaults_applied"):
        cov = graph.setdefault("coverage", {})
        cov["defaults_applied"] = sorted(
            set(list(cov.get("defaults_applied") or []) + list(r["defaults_applied"])))
    for p in graph.get("papers", []):
        p.pop("source_text", None)

    if a.save:
        if not a.memory_dir:
            raise SystemExit("--save needs --memory-dir")
        save_state(graph, os.path.join(a.memory_dir, gid))
        idx_path = os.path.join(a.memory_dir, "index.json")
        index = {}
        if os.path.isfile(idx_path):
            with open(idx_path, encoding="utf8") as fh:
                index = json.load(fh)
        index[gid] = {"question": graph.get("question"),
                      "round": graph.get("round"),
                      "updated_at": graph.get("generated_at")}
        with open(idx_path, "w", encoding="utf8") as fh:
            fh.write(_dump(index))

    _emit(graph, a.out)
    return 0


def _emit(graph, out):
    blob = _dump(graph)
    if out == "-":
        sys.stdout.write(blob)
    else:
        with open(out, "w", encoding="utf8") as fh:
            fh.write(blob)


def selftest():
    """Exercise the properties that matter, on synthetic input."""
    ok = True

    def check(name, cond):
        nonlocal ok
        ok = ok and bool(cond)
        print("%-46s %s" % (name, "PASS" if cond else "FAIL"))

    text = "Alpha inhibits Beta in cultured cells. Gamma had no effect."
    papers = [{"id": "p1", "doi": "10.1/a", "title": "A", "year": 2020,
               "study_type": "test_tube", "first_author": "Ann",
               "source_text": text},
              {"id": "p2", "doi": "10.1/b", "title": "B", "year": 2021,
               "study_type": "test_tube", "first_author": "Bob",
               "source_text": text}]
    things = [{"id": "t1", "name": "Alpha", "kind": "protein"},
              {"id": "t2", "name": "Beta", "kind": "protein"}]
    finds = [{"id": "f1", "paper": "p1", "from": "t1", "how": "inhibits",
              "to": "t2", "says": "yes", "is_own_result": True,
              "section": "results", "where": "cultured cells",
              "quote": "Alpha inhibits Beta in cultured cells."},
             {"id": "f2", "paper": "p2", "from": "t1", "how": "inhibits",
              "to": "t2", "says": "yes", "is_own_result": True,
              "section": "results", "where": "mouse",
              "quote": "Alpha inhibits Beta in cultured cells."},
             {"id": "f3", "paper": "p1", "from": "t1", "how": "inhibits",
              "to": "t2", "says": "yes", "is_own_result": True,
              "section": "results", "where": "x",
              "quote": "This sentence is not in the source text at all."}]
    g = main(None, finds, papers, 1, "new_question", question="q",
             graph_id="g_test", generated_at="T", new_things=things)
    check("unverifiable quote dropped", g["coverage"]["no_quote_discarded"] == 1)
    check("verified findings kept", len(g["findings"]) == 2)
    check("one link built", len(g["links"]) == 1)
    check("link is agreed", g["links"][0]["state"] == "agreed")

    g2 = main(None, finds + finds, papers, 1, "new_question", question="q",
              graph_id="g_test", generated_at="T", new_things=things)
    check("duplicate findings deduped", len(g2["findings"]) == 2)
    # 6 rows in, 2 of them the unverifiable f3 -- those are discarded on the
    # quote check BEFORE dedupe ever sees them, so only f1 and f2 repeat.
    check("duplicates counted", g2["coverage"].get("duplicates_dropped") == 2)
    check("discards counted separately",
          g2["coverage"]["no_quote_discarded"] == 2)

    check("quote match is whitespace-normalized",
          verify_quote("Alpha   inhibits\nBeta in cultured cells.", text))
    check("paraphrase rejected", not verify_quote("Alpha blocks Beta.", text))
    check("agreement of a lone source is 0.5", agreement(1, 0) == 0.5)
    check("agreement is symmetric", agreement(3, 1) == 1 - agreement(1, 3) + 0.0
          or abs((agreement(3, 1) - 0.5) + (agreement(1, 3) - 0.5)) < 1e-9)
    check("no-overlap conditions explained",
          explain_disagreement([{"where": "a"}], [{"where": "b"}]) is not None)
    check("overlapping conditions not explained",
          explain_disagreement([{"where": "a"}], [{"where": "a"}]) is None)
    check("missing prior dir is an empty graph, not an error",
          load_state("/nonexistent/dir/xyz")["things"] == [])

    # Regression: round-local ids colliding with stored ids must not repoint
    # stored findings. Before the fix this discarded prior evidence and reported
    # it as a quote failure.
    import tempfile as _tf
    _d = _tf.mkdtemp()
    save_state({"schema_version": "1.1", "graph_id": "g_c", "question": "q",
                "round": 1, "status": "ok", "generated_at": "T", "error": None,
                "things": [{"id": "t1", "name": "Alpha", "kind": "gene"}],
                "papers": [{"id": "p1", "doi": "10.1/A", "title": "A",
                            "source_text": "Alpha inhibits Beta."}],
                "findings": [{"id": "f1", "paper": "p1", "from": "t1",
                              "how": "inhibits", "to": "t2", "says": "yes",
                              "is_own_result": True,
                              "quote": "Alpha inhibits Beta."}],
                "links": [], "gaps": [], "coverage": {}, "rounds": []}, _d)
    gc = main(_d,
              [{"id": "f1", "paper": "p1", "from": "t1", "how": "activates",
                "to": "t3", "says": "yes", "is_own_result": True,
                "quote": "Gamma activates Delta."}],
              [{"id": "p1", "doi": "10.1/B", "title": "B",
                "source_text": "Gamma activates Delta."}],
              2, "expand_node", graph_id="g_c", generated_at="T",
              new_things=[{"id": "t1", "name": "Gamma", "kind": "gene"}])
    check("colliding round ids keep both findings", len(gc["findings"]) == 2)
    check("colliding round ids discard nothing", gc["coverage"]["no_quote_discarded"] == 0)
    _by = {p["id"]: p.get("doi") for p in gc["papers"]}
    check("stored finding still points at its own paper",
          any(_by.get(f["paper"]) == "10.1/A" and "Alpha" in f["quote"]
              for f in gc["findings"]))

    # Regression: a retried new_question must REJOIN its graph, not overwrite it.
    # _mint_graph_id is stable per question, so without loading prior state the
    # stable id pointed at a graph that had just lost its history.
    _d2 = _tf.mkdtemp()
    save_state({"schema_version": "1.1", "graph_id": "g_r", "question": "q",
                "round": 2, "status": "ok", "generated_at": "T", "error": None,
                "things": [{"id": "t1", "name": "X", "kind": "gene"}],
                "papers": [{"id": "p1", "doi": "10.1/A", "title": "A",
                            "source_text": "X drives Y."}],
                "findings": [{"id": "f1", "paper": "p1", "from": "t1",
                              "how": "drives", "to": "t2", "says": "yes",
                              "is_own_result": True, "round": 1,
                              "quote": "X drives Y."}],
                "links": [], "gaps": [], "coverage": {}, "rounds": []}, _d2)
    gr = main(_d2, [], [], 3, "new_question", graph_id="g_r", question="q",
              generated_at="T")
    check("retried new_question keeps prior findings", len(gr["findings"]) == 1)

    a1 = _dump(main(None, finds, papers, 1, "new_question", question="q",
                    graph_id="g_test", generated_at="T", new_things=things))
    a2 = _dump(main(None, finds, papers, 1, "new_question", question="q",
                    graph_id="g_test", generated_at="T", new_things=things))
    check("twice-run byte-identical", a1 == a2)

    print("\nSELFTEST:", "GREEN" if ok else "RED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(cli())

