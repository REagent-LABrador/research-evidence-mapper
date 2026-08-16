#!/usr/bin/env python3
"""Deterministic graph assembly for literature-graph.

Stdlib only. No classes. Pure functions, so each piece is testable alone.

DETERMINISM IS THE POINT. Every score in the output must be recomputable from
findings + papers, and two runs on the same input must be byte-identical. That
means: sort before emitting, never iterate a set into output, no time, no
randomness, and stable sort keys everywhere. Non-determinism here silently
corrupts every score in the graph.
"""

import hashlib
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
    return _despace(_fold(quote)) in _despace(_fold(source_text))


_PUNCT_SPACE = re.compile(r"(?<=[^0-9A-Za-z])\s+|\s+(?=[^0-9A-Za-z])")


def _despace(s):
    """Drop whitespace that sits against punctuation, and only that.

    Normalizing runs of whitespace to one space is not enough. Corpora render
    the SAME sentence with different spacing around punctuation -- Paperclip's
    content.lines emits "( Figure 5F )" where the PDF reads "(Figure 5F)" --
    and a quote copied faithfully from one rendering then fails against the
    other. Two real findings in runs/g_e087.json failed for exactly that and
    nothing else.

    Dropping whitespace ENTIRELY was the first attempt and it was too weak: with
    no spaces on either side, a "quote" fused from three non-adjacent table
    cells -- "NAC", "124", "31" -> "NAC12431" -- matches a source that never
    contains that string, and gets reported verbatim-verified. So the rule is
    narrower: whitespace touching a non-alphanumeric is typesetting and goes;
    whitespace BETWEEN two word characters is structure and stays. Punctuation
    stops deciding whether a quote is verbatim, and word boundaries still do.
    """
    return _PUNCT_SPACE.sub("", s)


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
              round_n=None, findings=None, id_floor=0):
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
    # id_floor is the highest gap number ever issued for this graph, carried in
    # coverage.id_seq. Without it a gap that drops out of the ranking frees its
    # id, and a later round re-mints it for a DIFFERENT missing pair -- so
    # `test_gap g1` searches a question nobody asked.
    prior_by_pair, used = {}, set()
    for g in (prior_gaps or []):
        prior_by_pair[tuple(sorted(g.get("missing") or []))] = g
        used.add(g.get("id"))
    used.update("g%d" % i for i in range(1, int(id_floor or 0) + 1))

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
# interpretability -- the shared LABrador output contract
#
# schema/interpretability.schema.json is the machine contract; this builds it.
# It is a PURE FUNCTION of the finished graph, for the same reason `delta` is
# derived: a stored second copy is a second source of truth, and when the two
# disagree a consumer has to guess which one is right. That also means `--show`
# can rebuild the block for a graph assembled before this contract existed.
# --------------------------------------------------------------------------

INTERPRETABILITY_SCHEMA_VERSION = "1.0.0"

# The link score's constants, named once so nothing stays hidden in code.
CONFIDENCE_WEIGHTS = {"agreement": 0.4, "evidence_quality": 0.4,
                      "independence": 0.2}
PREPRINT_PENALTY = 0.8
GAP_RANKING_CONSTANTS = {
    "cap": 50,                # gaps returned
    "confidence_ceiling": 0.6,  # a gap is a proposal, never a finding
    "degree_cap": 24,         # neighbours enumerated per hub
    "route_bonus_coefficient": 0.12,   # x log2(routes)
    "shared_paper_penalty": 0.65,      # both links rest on the same paper
}

# Study design -> evidence grade. Deliberately coarse: this grades the DESIGN,
# not the result. Nothing here upgrades a quote that was never checked.
_GRADE_BY_STUDY = {
    "meta_analysis": "HIGH",
    "clinical_trial": "HIGH",
    "human_cohort": "MODERATE",
    "animal": "MODERATE",
    "test_tube": "LOW",
    "computational": "LOW",
    "review": "LOW",
    "unknown": "UNSUPPORTED",
}
_GRADE_ORDER = ["UNSUPPORTED", "LOW", "MODERATE", "HIGH"]


def _demote(grade, floor):
    """Cap a grade at `floor`. Grades only ever move down here."""
    return _GRADE_ORDER[min(_GRADE_ORDER.index(grade), _GRADE_ORDER.index(floor))]


def _evidence_grade(paper, quote_verified):
    grade = _GRADE_BY_STUDY.get(str(paper.get("study_type") or "unknown"),
                                "UNSUPPORTED")
    if paper.get("is_preprint"):
        grade = _demote(grade, "LOW")
    if paper.get("retracted"):
        grade = "UNSUPPORTED"
    if quote_verified is not True:
        # Fail closed. A quote nobody checked cannot carry a source's grade.
        grade = _demote(grade, "LOW")
    return grade


def _source_type(paper):
    """An ABSENT is_preprint is unknown, not false.

    Reporting it as "publication" asserts peer review nobody recorded -- and the
    same absent flag is what lets the paper past the x0.8 preprint penalty in
    evidence_quality. The score is left as computed; this stops the evidence row
    from claiming more than the record supports.
    """
    flag = paper.get("is_preprint")
    if flag is True:
        return "preprint"
    if flag is False:
        return "publication"
    return "publication_status_unrecorded"


def _source_ref(paper):
    """(durable id, resolvable url) -- never invented, null when absent."""
    doi = normalize_doi(paper.get("doi") or "")
    if doi:
        return "doi:%s" % doi, "https://doi.org/%s" % doi
    pmid = str(paper.get("pmid") or "").strip()
    if pmid:
        return "pmid:%s" % pmid, "https://pubmed.ncbi.nlm.nih.gov/%s/" % pmid
    return None, None


def _claim(finding, names):
    a = names.get(finding.get("from"), finding.get("from"))
    b = names.get(finding.get("to"), finding.get("to"))
    how = finding.get("how") or "relates to"
    says = finding.get("says")
    if says == "no":
        claim = "%s does not %s %s" % (a, how, b)
    elif says == "no_effect":
        claim = "%s has no effect on %s" % (a, b)
    else:
        claim = "%s %s %s" % (a, how, b)
    where = finding.get("where")
    return "%s (%s)" % (claim, where) if where else claim


def _link_label(link, names):
    return "%s --%s--> %s" % (names.get(link.get("from"), link.get("from")),
                              link.get("how"),
                              names.get(link.get("to"), link.get("to")))


def _round4(x):
    return round(float(x), 4)


_TAGS = re.compile(r"<[^>]*>")


def _plain(text, limit=400):
    """Untrusted text -> a short, tag-free display string.

    `error` can carry whatever the far end produced, including an HTML error
    page. Interpolated raw it would put markup into plain_language, which the
    contract forbids and the schema rejects -- so an upstream outage would turn
    into an unparseable graph on top of it.
    """
    out = _WS.sub(" ", _TAGS.sub(" ", str(text or ""))).strip()
    return out[:limit - 1] + "\u2026" if len(out) > limit else out


def _plural(n, noun):
    """"1 relationship", not "1 relationships". plain_language is a sentence a
    non-specialist reads, and a count of one is the common case in a thin graph.
    """
    return "%d %s%s" % (n, noun, "" if n == 1 else "s")


def _count(n, unit=""):
    """A count that was never recorded reads as 'not recorded', never as the
    Python token None and never as 0."""
    if n is None:
        return "not recorded"
    return "%s %s" % (n, unit) if unit else str(n)


_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.:-]+")


def _slug_id(text, taken):
    """Free text -> a contract-legal id suffix, unique within its collection.

    Ids here are built from strings the CALLER chose, and the shared contract
    constrains an id's characters. `coverage.defaults_applied` is documented as
    short prose -- "depth: standard (not supplied)" -- and that produced an id
    with spaces and parentheses, which made the whole graph fail its own schema.
    The readable original is not lost: it goes in the assumption's `basis`.
    """
    slug = _ID_UNSAFE.sub("_", str(text)).strip("_.:-") or "unnamed"
    out, n = slug, 1
    while out in taken:
        n += 1
        out = "%s_%d" % (slug, n)
    taken.add(out)
    return out


def _counterfactuals(graph, names):
    """What would move the conclusion, computed through the real score_link --
    hypothetical evidence, never asserted as real, never written to the graph."""
    links = graph.get("links") or []
    papers = graph.get("papers") or []
    by_finding = {f.get("id"): f for f in (graph.get("findings") or [])}
    if not links:
        return []

    def own(link):
        return [by_finding[i] for k in ("yes", "no", "no_effect")
                for i in (link.get(k) or []) if i in by_finding]

    def score(fs, extra_paper=None, extra_finding=None):
        ps = list(papers) + ([extra_paper] if extra_paper else [])
        rows = list(fs) + ([extra_finding] if extra_finding else [])
        yes = len([f for f in rows if f.get("is_own_result")
                   and f.get("says") == "yes"])
        no = len([f for f in rows if f.get("is_own_result")
                  and f.get("says") == "no"])
        ne = len([f for f in rows if f.get("is_own_result")
                  and f.get("says") == "no_effect"])
        return score_link(rows, ps), link_state(yes, no, ne)

    out = []
    ranked = sorted(links, key=lambda l: (-(l.get("confidence") or {}).get("overall", 0.0),
                                          str(l.get("id"))))
    top = ranked[0]
    fs = own(top)
    if fs:
        by_id = {p.get("id"): p for p in papers}
        model = by_id.get((fs[0] or {}).get("paper")) or {}
        hp = {"id": "__counterfactual__", "first_author": "__counterfactual__",
              "study_type": model.get("study_type") or "unknown",
              "is_preprint": False}
        hf = {"id": "__counterfactual__", "paper": "__counterfactual__",
              "from": top.get("from"), "how": top.get("how"),
              "to": top.get("to"), "says": "no", "is_own_result": True}
        conf, state = score(fs, hp, hf)
        was = (top.get("confidence") or {}).get("overall")
        out.append({
            "change": "One further %s study by a different group reports the "
                      "opposite direction for %s (%s)."
                      % (hp["study_type"], top.get("id"),
                         _link_label(top, names)),
            "result": "state %s -> %s, heuristic confidence %s -> %s"
                      % (top.get("state"), state, was, conf["overall"]),
            "meaning": "The strongest relationship in the graph is not robust "
                       "to a single contradicting primary result; it is a "
                       "claim about the literature, not about the biology.",
        })

    singles = [l for l in ranked if l.get("state") == "single_source"]
    if singles:
        one = singles[0]
        fs = own(one)
        by_id = {p.get("id"): p for p in papers}
        model = by_id.get((fs[0] or {}).get("paper")) if fs else {}
        model = model or {}
        hp = {"id": "__counterfactual__", "first_author": "__counterfactual__",
              "study_type": model.get("study_type") or "unknown",
              "is_preprint": False}
        hf = {"id": "__counterfactual__", "paper": "__counterfactual__",
              "from": one.get("from"), "how": one.get("how"),
              "to": one.get("to"), "says": "yes", "is_own_result": True}
        conf, state = score(fs, hp, hf)
        out.append({
            "change": "One independent replication of %s (%s) by a different "
                      "first author." % (one.get("id"), _link_label(one, names)),
            "result": "state %s -> %s, heuristic confidence %s -> %s"
                      % (one.get("state"), state,
                         (one.get("confidence") or {}).get("overall"),
                         conf["overall"]),
            "meaning": "Shows how much of this relationship's score is the "
                       "independence term rather than the evidence itself.",
        })

    # Always, not gated on a loop variable: coverage is the dominant
    # uncertainty in every round, including the ones with no disputed link.
    out.append({
        "change": "The literature search is re-run without the corpus limits "
                  "in `coverage.limits`.",
        "result": "unknown -- %s of %s papers found were read this round"
                  % (_count((graph.get("coverage") or {}).get("read")),
                     _count((graph.get("coverage") or {}).get("found"))),
        "meaning": "Absence of a relationship in this graph is evidence about "
                   "what was read, not about what exists. Only "
                   "`stop_reason: complete` means the literature was exhausted "
                   "rather than the budget.",
    })
    return out


def normalize_coverage(cov, findings):
    """Fields the output contract requires to be present even when nothing set
    them. `queries` empty and `quotes_unverified` are facts about the round, so
    they are computed here rather than defaulted at read time by each consumer."""
    cov = dict(cov or {})
    cov.setdefault("queries", [])
    cov.setdefault("no_quote_discarded", 0)
    for key in ("found", "read", "used", "truncated", "stop_reason"):
        cov.setdefault(key, None)
    cov.setdefault("limits", {})
    # A graph-level fact, recomputed every round rather than accumulated, so a
    # re-run cannot inflate it.
    cov["quotes_unverified"] = sum(
        1 for f in (findings or []) if f.get("quote_verified") is not True)
    return cov


def build_interpretability(graph):
    """The shared LABrador interpretability block for one graph."""
    things = graph.get("things") or []
    papers = graph.get("papers") or []
    findings = graph.get("findings") or []
    links = graph.get("links") or []
    gaps = graph.get("gaps") or []
    cov = graph.get("coverage") or {}
    status = graph.get("status") or "ok"
    names = {t.get("id"): t.get("name") for t in things}
    by_paper = {p.get("id"): p for p in papers}

    limitations, seen_limits = [], set()

    def limit(code, severity, message, field_path=None):
        key = (code, field_path)
        if key in seen_limits:
            return
        seen_limits.add(key)
        limitations.append({"code": code, "severity": severity,
                            "message": message, "field_path": field_path})

    # ---------------- evidence: one row per verbatim finding ----------------
    evidence, ev_of, unverified, mismatched = [], {}, [], []
    ev_taken = set()
    for f in sorted(findings, key=lambda r: str(r.get("id"))):
        fid, p = f.get("id"), by_paper.get(f.get("paper")) or {}
        # Finding ids are supplied by the extractor, so they are free text as
        # far as this module is concerned. Normal ones (f1, f2_1) pass through
        # unchanged; a pathological one is slugged rather than breaking the id
        # contract for the whole graph.
        eid = "evidence.%s" % _slug_id(fid, ev_taken)
        ev_of[fid] = eid
        qv = f.get("quote_verified")
        if qv is not True:
            unverified.append(fid)
        if qv is False:
            mismatched.append(fid)
        src_id, src_url = _source_ref(p)
        if not src_id:
            limit("SOURCE_ID_MISSING", "WARNING",
                  "Paper %s carries neither a DOI nor a PMID, so its evidence "
                  "row has no durable identifier." % f.get("paper"),
                  "output.papers[id=%s]" % f.get("paper"))
        evidence.append({
            "id": eid,
            "claim": _claim(f, names),
            "source_type": _source_type(p),
            "source_id": src_id,
            "source_url": src_url,
            "locator": f.get("section") or None,
            "quote": f.get("quote") or None,
            "grade": _evidence_grade(p, qv),
            "synthetic": False,
        })

    unchecked = [i for i in unverified if i not in set(mismatched)]
    if unchecked:
        limit("UNVERIFIED_QUOTE", "ERROR",
              "%d of %d retained findings carry a quote that was never checked "
              "against source text, because no source text was supplied for "
              "their paper. Their quotes are shown but must not be treated as "
              "verified, and their evidence grade is capped at LOW."
              % (len(unchecked), len(findings)),
              "output.findings[].quote_verified")
    if mismatched:
        limit("QUOTE_MISMATCH", "ERROR",
              "%d retained findings carry a quote that was checked against "
              "source text and did NOT appear in it. This is not a wording "
              "difference -- the match is whitespace- and typography-normalized "
              "-- so treat these as unsourced: %s."
              % (len(mismatched), ", ".join(sorted(mismatched)[:8])),
              "output.findings[].quote_verified")
    if cov.get("no_quote_discarded"):
        limit("QUOTES_DISCARDED", "INFO",
              "%d extracted findings were discarded because their quote did "
              "not appear verbatim in the paper's source text."
              % cov["no_quote_discarded"], "output.coverage.no_quote_discarded")
    if not evidence:
        limit("EMPTY_EVIDENCE", "ERROR",
              "No source-backed finding survived quote verification, so there "
              "is no evidence to show.", "output.findings")

    # ---------------- assumptions: inputs chosen, not observed ---------------
    assumptions = []

    def assume(aid, path, value, unit, basis, synthetic=False):
        assumptions.append({"id": aid, "path": path, "value": value,
                            "unit": unit, "basis": basis,
                            "synthetic": bool(synthetic)})
        return aid

    a_depth = assume(
        "assumption.depth", "input.depth", cov.get("depth"), "tier",
        "Depth tier requested by the caller; it sets the paper budget for the "
        "round. null means the caller did not state one.")
    if cov.get("depth") is None:
        limit("DEPTH_NOT_SPECIFIED", "INFO",
              "No depth tier was recorded for this round, so the paper budget "
              "cannot be attributed to a requested tier.", "input.depth")
    limits = cov.get("limits") or {}
    a_papers = assume(
        "assumption.limits.max_papers", "input.coverage.limits.max_papers",
        limits.get("max_papers"), "papers",
        "Hard ceiling on papers read this round. Every coverage count is "
        "bounded by it, so an absent relationship may be a budget artefact.")
    a_queries = assume(
        "assumption.limits.max_queries", "input.coverage.limits.max_queries",
        limits.get("max_queries"), "queries",
        "Hard ceiling on distinct corpus queries issued this round.")
    a_years = assume(
        "assumption.years", "input.years", cov.get("years"), "year range",
        "Publication window the search was restricted to. null means no window "
        "was requested, NOT that the corpus is complete.")
    if cov.get("years") is None:
        limit("YEARS_NOT_SPECIFIED", "INFO",
              "No publication window was requested, so a windowed absence "
              "cannot be distinguished from a real one.", "input.years")
    a_weights = assume(
        "assumption.confidence_weights", "code.score_link", CONFIDENCE_WEIGHTS,
        "weight",
        "Fixed weights in score_link. Chosen, not fitted: no labelled corpus "
        "exists to fit them against.")
    a_quality = assume(
        "assumption.study_quality_table", "code._STUDY_QUALITY",
        dict(_STUDY_QUALITY), "score",
        "Study-design quality prior. An out-of-vocabulary study_type scores as "
        "`unknown` (0.4) rather than silently matching something close.")
    a_preprint = assume(
        "assumption.preprint_penalty", "code.evidence_quality",
        PREPRINT_PENALTY, "factor",
        "Multiplier applied to a preprint's design score, for absence of peer "
        "review.")
    a_own = assume(
        "assumption.own_result_only", "code._own", True, "flag",
        "Only a paper's own results count toward a link's STATE, its agreement "
        "term and its independence term -- a review restating forty studies is "
        "one paper, and is not independent evidence for any of them. Note the "
        "exception: `evidence_quality` averages over every finding on the link, "
        "cited background included.")
    a_gap = assume(
        "assumption.gap_ranking", "code.find_gaps", dict(GAP_RANKING_CONSTANTS),
        "constants",
        "Constants in the open-triangle gap ranking, including the 0.6 ceiling "
        "that keeps a gap a proposal rather than a finding.")
    taken = set()
    for field in sorted(cov.get("defaults_applied") or []):
        # The documented shape is prose ("depth: standard (not supplied)"), so
        # the field name is whatever precedes the colon.
        name = str(field).split(":", 1)[0].strip() or field
        assume("assumption.default.%s" % _slug_id(name, taken),
               "input.%s" % name, None, None,
               "The caller did not supply this field and a default was "
               "substituted. Recorded by the round as: %s" % field,
               synthetic=True)
    if cov.get("defaults_applied"):
        limit("DEFAULTS_APPLIED", "WARNING",
              "The caller omitted %s; defaults were substituted."
              % ", ".join(sorted(cov["defaults_applied"])),
              "output.coverage.defaults_applied")

    # ---------------- metrics ----------------
    disputed = [l for l in links if l.get("state") == "disagreed"]
    agreed = [l for l in links if l.get("state") == "agreed"]
    single = [l for l in links if l.get("state") == "single_source"]
    no_effect = [l for l in links if l.get("state") == "no_effect"]
    overalls = sorted((l.get("confidence") or {}).get("overall", 0.0)
                      for l in links)
    mean_conf = _round4(sum(overalls) / len(overalls)) if overalls else None
    def ev_for(ls):
        return sorted({ev_of[i] for l in ls
                       for k in ("yes", "no", "no_effect")
                       for i in (l.get(k) or []) if i in ev_of})

    metrics = []

    def metric(mid, label, value, unit, display, meaning, direction,
               evidence_ids=None, assumption_ids=None):
        metrics.append({
            "id": mid, "label": label, "value": value, "unit": unit,
            "display": display, "meaning": meaning, "direction": direction,
            "evidence_ids": sorted(evidence_ids or []),
            "assumption_ids": sorted(assumption_ids or []),
        })

    metric("metric.papers_found", "Papers matched by search", cov.get("found"),
           "papers", _count(cov.get("found"), "found"),
           "How many papers the corpus returned before any reading. Bounded by "
           "the query budget, not by what exists.", "neutral",
           [], [a_queries, a_depth])
    metric("metric.papers_read", "Papers read", cov.get("read"), "papers",
           _count(cov.get("read"), "read"),
           "How many matched papers were actually opened this round.",
           "neutral", [], [a_papers, a_depth])
    metric("metric.papers_used", "Papers contributing evidence",
           cov.get("used"), "papers", _count(cov.get("used"), "used"),
           "Papers that produced at least one quote-backed finding. The gap "
           "between read and used is the screening loss.", "neutral",
           [], [a_papers, a_own])
    metric("metric.findings_verified", "Quote-verified findings",
           len(findings) - len(unverified), "findings",
           "%d verified" % (len(findings) - len(unverified)),
           "Findings whose quote was matched verbatim against the paper's "
           "source text. This is a string match, not a judgement.", "positive",
           [ev_of[f["id"]] for f in findings
            if f.get("quote_verified") is True and f.get("id") in ev_of], [])
    metric("metric.findings_unverified", "Findings with unchecked quotes",
           len(unchecked), "findings", "%d unchecked" % len(unchecked),
           "Findings kept without source text to check their quote against. "
           "Never counted as verified.", "negative",
           [ev_of[i] for i in unchecked if i in ev_of], [])
    metric("metric.findings_quote_mismatch", "Findings whose quote did not match",
           len(mismatched), "findings", "%d mismatched" % len(mismatched),
           "Findings whose quote was checked against source text and was not "
           "found in it. Treat as unsourced.", "negative",
           [ev_of[i] for i in mismatched if i in ev_of], [])
    metric("metric.relationships", "Relationships", len(links),
           "relationships", "%d relationships" % len(links),
           "Distinct (subject, predicate, object) triples supported by at "
           "least one paper's own result.", "neutral", [], [a_own])
    metric("metric.relationships_disputed", "Disputed relationships",
           len(disputed), "relationships", "%d disputed" % len(disputed),
           "Relationships where primary papers report opposite directions. "
           "These are the ones worth reading by hand.", "mixed",
           ev_for(disputed), [a_own])
    metric("metric.relationships_agreed", "Corroborated relationships",
           len(agreed), "relationships", "%d corroborated" % len(agreed),
           "Relationships where two or more papers agree on direction.",
           "positive", ev_for(agreed), [a_own])
    metric("metric.relationships_single_source", "Single-source relationships",
           len(single), "relationships", "%d single-source" % len(single),
           "Relationships resting on exactly one paper. Uncorroborated, not "
           "wrong.", "negative", ev_for(single), [a_own])
    metric("metric.relationships_no_effect", "Reported-null relationships",
           len(no_effect), "relationships", "%d null results" % len(no_effect),
           "Relationships where the only primary evidence is an explicit "
           "no-effect result. Kept separately so a null is never read as an "
           "absence of evidence.", "neutral", ev_for(no_effect), [a_own])
    metric("metric.gaps_open", "Untested relationships proposed", len(gaps),
           "gaps", "%d open gaps" % len(gaps),
           "Pairs connected through an intermediate but with no direct link "
           "reported. Proposals for the next round, not findings.", "neutral",
           [], [a_gap])
    metric("metric.mean_link_confidence", "Mean heuristic confidence",
           mean_conf, "score",
           "not recorded" if mean_conf is None else "%s mean" % mean_conf,
           "Mean of the per-relationship heuristic score. A ranking aid, NOT a "
           "probability that any relationship is true.", "unknown",
           [], [a_weights, a_quality, a_preprint])
    metric("metric.entities", "Distinct entities", len(things), "entities",
           "%d entities" % len(things),
           "Nodes after accession-first merging of synonyms.", "neutral",
           [], [])

    untagged = [m["id"] for m in metrics
                if not m["evidence_ids"] and not m["assumption_ids"]]
    if untagged:
        limit("UNTAGGED_VALUE", "WARNING",
              "These metrics are structural counts of the graph itself and "
              "reference neither evidence nor an assumption: %s."
              % ", ".join(untagged), "interpretability.metrics")
    if not metrics:
        limit("EMPTY_METRICS", "ERROR", "No metrics could be derived.",
              "interpretability.metrics")

    # ---------------- steps: how each derived value was calculated ----------
    queries = list(cov.get("queries") or [])
    steps = [
        {
            "id": "step.search",
            "label": "Search the corpus",
            "method": "Paperclip full-text search over the biomedical corpus",
            "formula": None,
            "inputs": [
                {"path": "input.coverage.queries",
                 "value": [q.get("q") for q in queries], "unit": None},
                {"path": "input.coverage.limits.max_queries",
                 "value": limits.get("max_queries"), "unit": "queries"},
                {"path": "input.depth", "value": cov.get("depth"),
                 "unit": "tier"},
            ],
            "result": {"value": cov.get("found"), "unit": "papers"},
            "evidence_ids": [], "assumption_ids": [a_queries, a_depth, a_years],
        },
        {
            "id": "step.screen",
            "label": "Read and screen",
            "method": "Full-text read, then keep papers that yield a "
                      "quote-backed finding",
            "formula": "papers_found -> papers_read -> papers_used",
            "inputs": [
                {"path": "output.coverage.found", "value": cov.get("found"),
                 "unit": "papers"},
                {"path": "output.coverage.limits.max_papers",
                 "value": limits.get("max_papers"), "unit": "papers"},
            ],
            "result": {"value": cov.get("used"), "unit": "papers"},
            "evidence_ids": [], "assumption_ids": [a_papers],
        },
        {
            "id": "step.quote_verification",
            "label": "Verify every quote against source text",
            "method": "Mechanical substring match, not a model judgement",
            "formula": "fold(NFKC(quote)) is a substring of "
                       "fold(NFKC(source_text)); non-matching findings are "
                       "discarded, findings with no source text are retained "
                       "and marked quote_verified=null",
            "inputs": [
                {"path": "input.findings[].quote", "value": len(findings) +
                 int(cov.get("no_quote_discarded") or 0), "unit": "findings"},
                {"path": "input.papers[].source_text",
                 "value": len([p for p in papers if p.get("source_sha256")]),
                 "unit": "papers"},
            ],
            "result": {"value": len(findings) - len(unverified),
                       "unit": "findings"},
            "evidence_ids": [], "assumption_ids": [],
        },
        {
            "id": "step.entity_resolution",
            "label": "Merge synonymous entities",
            "method": "UniProt accession first, then separator-insensitive "
                      "normalized name",
            "formula": "a conflicting accession BLOCKS a name merge, so "
                       "human Q9NWZ3 and mouse Q8R4K2 stay distinct",
            "inputs": [{"path": "input.things", "value": len(things),
                        "unit": "entities"}],
            "result": {"value": len(things), "unit": "entities"},
            "evidence_ids": [], "assumption_ids": [],
        },
        {
            "id": "step.link_assembly",
            "label": "Group findings into relationships",
            "method": "Group by (from, how, to) over own-result findings only",
            "formula": "state = disagreed if yes and no; agreed if >=2 one "
                       "way; single_source if exactly one; no_effect if only "
                       "explicit nulls",
            "inputs": [{"path": "output.findings", "value": len(findings),
                        "unit": "findings"}],
            "result": {"value": len(links), "unit": "relationships"},
            "evidence_ids": [], "assumption_ids": [a_own],
        },
        {
            "id": "step.confidence.agreement",
            "label": "Agreement term",
            "method": "Direction balance across a relationship's own results",
            "formula": "0.5 + (yes - no) / (2 * (yes + no)); a lone source is "
                       "0.5, never 1.0",
            "inputs": [{"path": "output.links[].yes|no", "value": len(links),
                        "unit": "relationships"}],
            "result": {"value": _round4(sum((l.get("confidence") or {})
                                            .get("agreement", 0.0)
                                            for l in links) / len(links))
                       if links else None, "unit": "score"},
            "evidence_ids": [], "assumption_ids": [a_own],
        },
        {
            "id": "step.confidence.evidence_quality",
            "label": "Evidence-quality term",
            "method": "Study-design prior, averaged over the relationship's "
                      "findings",
            "formula": "mean(_STUDY_QUALITY[study_type]), x0.8 per preprint",
            "inputs": [{"path": "output.papers[].study_type",
                        "value": len(papers), "unit": "papers"}],
            "result": {"value": _round4(sum((l.get("confidence") or {})
                                            .get("evidence_quality", 0.0)
                                            for l in links) / len(links))
                       if links else None, "unit": "score"},
            "evidence_ids": [], "assumption_ids": [a_quality, a_preprint],
        },
        {
            "id": "step.confidence.independence",
            "label": "Independence term",
            "method": "Distinct first authors behind a relationship",
            "formula": "(distinct first authors - 1) / (papers - 1); one group "
                       "is not a consensus, so a single group scores 0.0",
            "inputs": [{"path": "output.papers[].first_author",
                        "value": len({p.get("first_author") for p in papers}),
                        "unit": "authors"}],
            "result": {"value": _round4(sum((l.get("confidence") or {})
                                            .get("independence", 0.0)
                                            for l in links) / len(links))
                       if links else None, "unit": "score"},
            "evidence_ids": [], "assumption_ids": [],
        },
        {
            "id": "step.confidence.overall",
            "label": "Combine into a heuristic confidence score",
            "method": "Fixed-weight linear combination. HEURISTIC -- not a "
                      "probability, not calibrated against any outcome",
            "formula": "0.4*agreement + 0.4*evidence_quality + "
                       "0.2*independence; label high >=0.7, medium >=0.4, "
                       "else low",
            "inputs": [
                {"path": "code.CONFIDENCE_WEIGHTS.agreement", "value": 0.4,
                 "unit": "weight"},
                {"path": "code.CONFIDENCE_WEIGHTS.evidence_quality",
                 "value": 0.4, "unit": "weight"},
                {"path": "code.CONFIDENCE_WEIGHTS.independence", "value": 0.2,
                 "unit": "weight"},
            ],
            "result": {"value": mean_conf, "unit": "score"},
            "evidence_ids": [], "assumption_ids": [a_weights],
        },
        {
            "id": "step.gap_ranking",
            "label": "Rank untested relationships",
            "method": "Open triangles (A-B and B-C exist, A-C does not), "
                      "ranked multiplicatively to break the ties a min() "
                      "score produces",
            "formula": "min(conf_AB, conf_BC) * basis_AB * basis_BC * "
                       "independence * (1/sqrt(gaps spawned by the hub)) * "
                       "(1 + 0.12*log2(routes)), capped at 0.6",
            "inputs": [
                {"path": "output.links", "value": len(links),
                 "unit": "relationships"},
                {"path": "code.GAP_RANKING_CONSTANTS",
                 "value": dict(GAP_RANKING_CONSTANTS), "unit": None},
            ],
            "result": {"value": len(gaps), "unit": "gaps"},
            "evidence_ids": [], "assumption_ids": [a_gap],
        },
        {
            "id": "step.stop",
            "label": "Stop the round",
            "method": "Stopping rule: %s" % (cov.get("stop_reason") or
                                             "not recorded"),
            "formula": None,
            "inputs": [
                {"path": "output.coverage.stop_reason",
                 "value": cov.get("stop_reason"), "unit": None},
                {"path": "output.coverage.truncated",
                 "value": cov.get("truncated"), "unit": None},
            ],
            "result": {"value": cov.get("used"), "unit": "papers"},
            "evidence_ids": [], "assumption_ids": [a_papers, a_queries],
        },
    ]

    # ---------------- uncertainty ----------------
    intervals = []
    if overalls:
        intervals.append({
            "metric_id": "metric.mean_link_confidence",
            "low": _round4(overalls[0]),
            "central": mean_conf,
            "high": _round4(overalls[-1]),
            "unit": "score",
            "confidence_level": None,
            "interval_type": "observed_range",
        })
    uncertainty = {
        "method": "Heuristic confidence decomposition (agreement, evidence "
                  "quality, independence). No sampling, no model fitting and "
                  "no statistical inference is performed.",
        "intervals": intervals,
        "seed": None,
        "draws": None,
        "limitations": [
            "low/central/high are the observed minimum, mean and maximum of "
            "the per-relationship heuristic score across the %d relationships "
            "in this graph. They are an observed range -- NOT percentiles, "
            "NOT a confidence interval, and NOT a sampling distribution."
            % len(links),
            "The score is a ranking aid over what was read. It is not a "
            "probability that a relationship is true, and it is not calibrated "
            "against any outcome.",
            "seed and draws are null because assembly is deterministic: the "
            "same round bundle produces byte-identical output.",
            "The dominant uncertainty is not in the score, it is in coverage: "
            "%s of %s matched papers were read this round."
            % (cov.get("read"), cov.get("found")),
        ],
    }

    # ---------------- limitations from coverage ----------------
    limit("HEURISTIC_CONFIDENCE", "INFO",
          "link.confidence.overall is a heuristic score "
          "(0.4*agreement + 0.4*evidence_quality + 0.2*independence), not a "
          "probability. Do not threshold it as one.",
          "output.links[].confidence.overall")
    if cov.get("truncated"):
        limit("TRUNCATED_SEARCH", "WARNING",
              "The search was truncated (stop_reason=%s). Absence of a "
              "relationship is evidence about what was read, not about what "
              "exists." % (cov.get("stop_reason") or "unrecorded"),
              "output.coverage.truncated")
    if (cov.get("stop_reason") or "") != "complete":
        limit("COVERAGE_INCOMPLETE", "WARNING",
              "stop_reason=%s. Only `complete` means the literature was "
              "exhausted rather than the budget."
              % (cov.get("stop_reason") or "not recorded"),
              "output.coverage.stop_reason")
    if not queries:
        limit("QUERIES_NOT_RECORDED", "WARNING",
              "The round did not record the search queries it issued, so the "
              "search cannot be reproduced or audited from this output.",
              "output.coverage.queries")
    if single:
        limit("SINGLE_SOURCE_LINKS", "WARNING",
              "%d of %d relationships rest on exactly one paper."
              % (len(single), len(links)), "output.links[].state")
    if disputed:
        limit("DISPUTED_LINKS", "INFO",
              "%d relationships have primary papers reporting opposite "
              "directions; `why` names the differing conditions where they "
              "could be identified." % len(disputed), "output.links[].why")
    if cov.get("proteins_without_accession_count"):
        limit("PROTEINS_WITHOUT_ACCESSION", "WARNING",
              "%d protein nodes carry no UniProt accession, so cross-species "
              "and synonym merging for them rests on name matching alone."
              % cov["proteins_without_accession_count"],
              "output.coverage.proteins_without_accession")
    if cov.get("interventions_without_target_count"):
        limit("INTERVENTIONS_WITHOUT_TARGET", "WARNING",
              "%d intervention nodes have no molecular target in the graph."
              % cov["interventions_without_target_count"],
              "output.coverage.interventions_without_target")
    if things and cov.get("has_disease_node") is False:
        limit("NO_DISEASE_NODE", "WARNING",
              "The graph contains no disease node, so no relationship reaches "
              "a clinical endpoint.", "output.things")
    if any(p.get("retracted") for p in papers):
        limit("RETRACTED_SOURCE", "ERROR",
              "At least one contributing paper is flagged retracted; its "
              "evidence is graded UNSUPPORTED but is NOT removed.",
              "output.papers[].retracted")
    missing_hash = [p.get("id") for p in papers if not p.get("source_sha256")]
    if missing_hash:
        limit("INTERPRETABILITY_PARTIAL", "INFO",
              "No source-text hash for %d of %d papers, and no Paperclip "
              "document id or page/figure locator is recorded for any paper -- "
              "the corpus does not expose stable document ids through the "
              "search interface used here. Evidence locators fall back to the "
              "finding's section label."
              % (len(missing_hash), len(papers)), "output.papers[].source_sha256")

    if status == "partial":
        limit("ROUND_PARTIAL", "WARNING",
              "The round reported status 'partial': it completed, but not "
              "everything it set out to read was read. Treat the graph as a "
              "sample of a sample.", "output.status")
    if status == "empty":
        limit("ROUND_EMPTY", "WARNING",
              "The round reported status 'empty': the search ran and returned "
              "nothing usable. That is not the same as the relationship not "
              "existing.", "output.status")
    if graph.get("error"):
        limit("ROUND_ERROR", "ERROR", _plain(graph.get("error")), "output.error")
    missing_counts = [k for k in ("found", "read", "used")
                      if cov.get(k) is None]
    if missing_counts:
        limit("COVERAGE_NOT_RECORDED", "WARNING",
              "The round did not record %s, so how much of the literature this "
              "graph covers cannot be stated. They are null, not zero."
              % ", ".join("coverage.%s" % k for k in missing_counts),
              "output.coverage")
    if not (cov.get("limits") or {}).get("max_papers"):
        limit("LIMITS_NOT_RECORDED", "INFO",
              "No paper budget was recorded for this round, so `papers_read` "
              "cannot be attributed to a limit.",
              "output.coverage.limits")
    unattributed = [p.get("id") for p in papers if not p.get("first_author")]
    if unattributed:
        limit("INDEPENDENCE_OVERSTATED", "WARNING",
              "%d of %d papers carry no `first_author`. `independence` falls "
              "back to the paper id when the author is missing, so distinct "
              "papers count as distinct groups and the term reads HIGHER than "
              "the evidence supports. The score is unchanged and reported as "
              "computed; this says what it is measuring."
              % (len(unattributed), len(papers)),
              "output.links[].confidence.independence")

    # SCHEMA.md promised "every id resolves, enforced in code" and nothing
    # enforced it: a finding whose `from`, `to` or `paper` matched nothing in
    # this round's id map is passed through verbatim, links are built from those
    # ids, and the graph ships naming rows that do not exist. Dropping them here
    # would delete evidence on a bookkeeping error, so they are REPORTED --
    # and the promise in SCHEMA.md is corrected to match.
    thing_ids = {t.get("id") for t in things}
    paper_ids = {p.get("id") for p in papers}
    finding_ids = {f.get("id") for f in findings}
    dangling = set()
    for f in findings:
        dangling.update(i for i in (f.get("from"), f.get("to"))
                        if i not in thing_ids)
        if f.get("paper") not in paper_ids:
            dangling.add(f.get("paper"))
    for l in links:
        dangling.update(i for i in (l.get("from"), l.get("to"))
                        if i not in thing_ids)
        dangling.update(i for k in ("yes", "no", "no_effect")
                        for i in (l.get(k) or []) if i not in finding_ids)
    for g in gaps:
        dangling.update(i for i in (g.get("missing") or [])
                        if i not in thing_ids)
    dangling.discard(None)
    if dangling:
        limit("DANGLING_REFERENCE", "ERROR",
              "%d id(s) referenced by findings, links or gaps do not resolve to "
              "a row in this graph: %s. The rows are kept rather than deleted, "
              "but anything joining on these ids will find nothing, and any "
              "label rendered from them shows the raw id."
              % (len(dangling), ", ".join(sorted(str(x) for x in dangling)[:10])),
              "output.findings[].from|to|paper")

    unknown = ([m["id"] for m in metrics if m["value"] is None]
               + [st["id"] for st in steps if st["result"]["value"] is None]
               + [a["id"] for a in assumptions if a["value"] is None])
    if unknown:
        # The contract: an unknown is null WITH a limitation. The codes above
        # each explain one cause; this is the inventory, so a consumer can
        # enumerate every null without knowing which code covers which field.
        limit("UNKNOWN_VALUE", "INFO",
              "%d values in this block are null because they were never "
              "recorded or could not be computed. Null is not zero and not "
              "false. Full list: %s." % (len(unknown), ", ".join(sorted(unknown))),
              "interpretability")

    no_preprint_flag = [p.get("id") for p in papers
                        if p.get("is_preprint") not in (True, False)]
    if no_preprint_flag:
        limit("PREPRINT_STATUS_UNRECORDED", "WARNING",
              "%d of %d papers do not record whether they are preprints. "
              "`evidence_quality` applies its 0.8 preprint penalty only on an "
              "explicit true, so those papers are scored as if peer-reviewed. "
              "The score is unchanged and reported as computed; this says what "
              "it assumed." % (len(no_preprint_flag), len(papers)),
              "output.papers[].is_preprint")

    counterfactuals = _counterfactuals(graph, names)
    if not counterfactuals:
        limit("EMPTY_COUNTERFACTUALS", "INFO",
              "No relationship was assembled, so there is nothing whose "
              "confidence a further paper could move.",
              "interpretability.counterfactuals")

    # ---------------- lineage ----------------
    lineage = [
        {"output_path": "output.findings[]",
         "input_paths": ["input.findings[]", "input.papers[].source_text"],
         "transformation": "Kept only when the quote matches the paper's "
                           "source text verbatim after NFKC and whitespace "
                           "folding; deduplicated by (paper, relationship, "
                           "folded quote) so a retried round cannot "
                           "double-count."},
        {"output_path": "output.papers[].source_sha256",
         "input_paths": ["input.papers[].source_text"],
         "transformation": "SHA-256 of the exact source text the quotes were "
                           "checked against. null when none was supplied."},
        {"output_path": "output.things[]",
         "input_paths": ["input.things[]"],
         "transformation": "Merged on UniProt accession first, then on "
                           "normalized name; a conflicting accession blocks a "
                           "name merge."},
        {"output_path": "output.links[].id",
         "input_paths": ["output.links[].from", "output.links[].how",
                         "output.links[].to", "prior.links[].id"],
         "transformation": "Bound to the (from, how, to) triple the round it "
                           "first appears and carried forward through entity "
                           "merges, so delta and changed_in_round keep naming "
                           "the same relationship across rounds."},
        {"output_path": "output.links[].state",
         "input_paths": ["output.findings[].says",
                         "output.findings[].is_own_result"],
         "transformation": "Counts of yes/no/no_effect over own-result "
                           "findings only."},
        {"output_path": "output.links[].confidence.overall",
         "input_paths": ["output.findings[].says",
                         "output.papers[].study_type",
                         "output.papers[].is_preprint",
                         "output.papers[].first_author"],
         "transformation": "0.4*agreement + 0.4*evidence_quality + "
                           "0.2*independence."},
        {"output_path": "output.links[].why",
         "input_paths": ["output.findings[].where"],
         "transformation": "Emitted only for a disagreed link whose "
                           "supporting and contradicting findings report "
                           "non-overlapping conditions."},
        {"output_path": "output.gaps[]",
         "input_paths": ["output.links[]", "output.things[]"],
         "transformation": "Open triangles ranked multiplicatively; ids keyed "
                           "on the missing pair so test_gap can target one by "
                           "id across rounds."},
        {"output_path": "output.delta",
         "input_paths": ["prior graph", "output.links[].changed_in_round"],
         "transformation": "Diff against the stored prior round. Derived, "
                           "never stored."},
    ]

    # ---------------- extensions: module-specific, never UI-required --------
    # Deliberately NOT a second copy of links[]. state, basis, confidence, why
    # and changed_in_round are already on the link under this same id; repeating
    # them here would make the block most of the graph's bytes and give a
    # consumer two places to read the same number from. What this adds is the
    # human label and the three evidence axes in interpretability ids.
    ledger = []
    for l in sorted(links, key=lambda r: str(r.get("id"))):
        ledger.append({
            "id": l.get("id"),
            "label": _link_label(l, names),
            # The three axes stay SEPARATE. Collapsing them loses the
            # difference between "nobody looked" and "somebody looked and
            # found nothing".
            "supporting": [ev_of[i] for i in (l.get("yes") or []) if i in ev_of],
            "contradicting": [ev_of[i] for i in (l.get("no") or []) if i in ev_of],
            "no_effect": [ev_of[i] for i in (l.get("no_effect") or []) if i in ev_of],
        })

    extensions = {
        "module": "research-evidence-mapper",
        "graph": {
            "graph_id": graph.get("graph_id"),
            "round": graph.get("round"),
            "rounds": list(graph.get("rounds") or []),
            "schema_version": graph.get("schema_version"),
        },
        # The literal flag the shared contract asks for: true only when EVERY
        # retained quote was checked against source text. Fails closed.
        "quote_verified": bool(findings) and not unverified,
        "quote_verification": {
            "method": "verbatim substring match after NFKC and typographic "
                      "folding; mechanical, not a model judgement",
            "verified": len(findings) - len(unverified),
            "unchecked": len(unchecked),
            "mismatched": len(mismatched),
            "discarded": int(cov.get("no_quote_discarded") or 0),
            "fails_closed": True,
            "by_evidence": {ev_of[f["id"]]: f.get("quote_verified")
                            for f in findings if f.get("id") in ev_of},
        },
        "search": {
            "queries": queries,
            "depth": cov.get("depth"),
            "limits": dict(limits),
            "found": cov.get("found"),
            "read": cov.get("read"),
            "used": cov.get("used"),
            "figures_read": cov.get("figures_read"),
            "truncated": cov.get("truncated"),
            "stop_reason": cov.get("stop_reason"),
            "years": cov.get("years"),
        },
        "confidence_decomposition": {
            "heuristic": True,
            "is_probability": False,
            "weights": dict(CONFIDENCE_WEIGHTS),
            "terms": ["agreement", "evidence_quality", "independence"],
            "labels": {"high": ">= 0.7", "medium": ">= 0.4", "low": "< 0.4"},
        },
        "relationships": ledger,
        # Same rule: the gap's own fields live on gaps[] under this id. Only the
        # resolved entity names are new, and only because a UI would otherwise
        # have to join t-ids by hand.
        "gaps": [{"id": g.get("id"),
                  "missing_labels": [names.get(x, x) for x in
                                     (g.get("missing") or [])]}
                 for g in gaps],
        "id_conventions": {
            "evidence": "evidence.<finding id, any character outside "
                        "[A-Za-z0-9_.:-] replaced by _>",
            "relationships": "the link id in output.links[]",
            "gaps": "the gap id in output.gaps[]",
            "note": "Every id here resolves into the graph beside it. The block "
                    "annotates the graph; it does not restate it.",
        },
    }

    # ---------------- headline ----------------
    if status == "failed":
        result, hstatus = "GRAPH_UNAVAILABLE", "FAILED"
    elif not findings:
        result, hstatus = "NO_EVIDENCE_FOUND", "INCONCLUSIVE"
    elif disputed:
        result, hstatus = "EVIDENCE_MAPPED_DISPUTED", "QUALIFIED"
    elif links and len(single) == len(links):
        result, hstatus = "EVIDENCE_MAPPED_SINGLE_SOURCE", "QUALIFIED"
    elif unverified or cov.get("truncated") or \
            (cov.get("stop_reason") or "") != "complete":
        result, hstatus = "EVIDENCE_MAPPED", "QUALIFIED"
    else:
        result, hstatus = "EVIDENCE_MAPPED", "SUPPORTED"

    basis = ["OBSERVED"] if findings else []
    if gaps:
        basis.append("INFERRED")
    if links:
        basis.append("MODELED")
    if any(a.get("synthetic") for a in assumptions) or \
            any(e.get("synthetic") for e in evidence):
        # The contract: SYNTHETIC must appear whenever ANY evidence or
        # assumption is synthetic. A substituted default is exactly that.
        basis.append("SYNTHETIC")
    if not basis:
        # Nothing was read, so nothing was observed. The conclusion "no graph"
        # is inferred from the absence, not observed in the literature.
        basis = ["INFERRED"]

    if status == "failed":
        plain = ("This round produced no graph: %s"
                 % (_plain(graph.get("error"))
                    or "the request could not be served."))
    elif not findings:
        plain = ("No paper in the corpus produced a quote-backed finding for "
                 "this question, so nothing was mapped.")
    else:
        plain = ("Read %s papers and mapped %s from %s; %d %s corroborated by "
                 "more than one paper, %d %s on a single paper, and %d %s "
                 "disputed."
                 % (_count(cov.get("used")),
                    _plural(len(links), "relationship"),
                    _plural(len(findings), "quote-backed finding"),
                    len(agreed), "is" if len(agreed) == 1 else "are",
                    len(single), "rests" if len(single) == 1 else "rest",
                    len(disputed), "is" if len(disputed) == 1 else "are"))

    headline = {
        "title": ("Evidence graph: %d relationships from %d papers"
                  % (len(links), len(papers))) if status != "failed"
                 else "No graph produced",
        "result": result,
        "plain_language": plain,
        "status": hstatus,
        "basis": basis,
    }

    return {
        "schema_version": INTERPRETABILITY_SCHEMA_VERSION,
        "headline": headline,
        "metrics": metrics,
        "steps": steps,
        "evidence": evidence,
        "assumptions": assumptions,
        "uncertainty": uncertainty,
        "limitations": sorted(limitations,
                              key=lambda l: (["ERROR", "WARNING", "INFO"]
                                             .index(l["severity"]),
                                             l["code"], str(l["field_path"]))),
        "counterfactuals": counterfactuals,
        "lineage": lineage,
        "extensions": extensions,
    }


def reverify(graph, source_dir):
    """Re-check every quote in a stored graph against re-fetched source text.

    This is how a graph assembled before quote_verified existed becomes
    honest: assembly-time verification is not recorded in the artifact, and
    this contract refuses to infer that it happened. Fails closed the same way
    assembly does -- a paper with no text file leaves its findings at null,
    never true.

    Unlike assembly it does NOT discard a non-matching finding. A rebuild must
    not silently change what a stored graph says; it marks the finding False
    and raises QUOTE_MISMATCH so a human decides.
    """
    texts = {}
    for p in graph.get("papers") or []:
        path = os.path.join(source_dir, "%s.txt" % p.get("id"))
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf8") as fh:
            text = fh.read()
        if not text.strip():
            continue
        texts[p.get("id")] = text
        p["source_sha256"] = hashlib.sha256(text.encode("utf8")).hexdigest()
    for f in graph.get("findings") or []:
        text = texts.get(f.get("paper"))
        if text is None:
            # Both earned verdicts survive a rebuild that could not re-fetch
            # this paper. Resetting a False to None would erase a KNOWN
            # mismatch and quietly downgrade it to "never checked" -- the
            # graph would stop reporting a quote it had already caught.
            if f.get("quote_verified") not in (True, False):
                f["quote_verified"] = None
        else:
            f["quote_verified"] = bool(verify_quote(f.get("quote"), text))
    return graph


def abstain_graph(graph_id, request, error, status="failed"):
    """A round that cannot be served is still a result, not a dropped call.

    It carries the same contract as a successful round -- empty collections
    plus an interpretability block that says FAILED and why -- so a consumer
    parses one shape rather than branching on whether the graph arrived.
    Infrastructure failures that produce no result file at all remain the
    orchestrator's problem, not this one.
    """
    graph = {
        "schema_version": "1.1",
        "graph_id": graph_id,
        "question": (request or {}).get("question"),
        "round": (request or {}).get("round", 0),
        "status": status,
        "generated_at": (request or {}).get("generated_at"),
        "error": error,
        "things": [], "papers": [], "findings": [], "links": [], "gaps": [],
        "coverage": normalize_coverage((request or {}).get("coverage"), []),
        "rounds": [],
    }
    graph["interpretability"] = build_interpretability(graph)
    graph["interpretability"]["limitations"].insert(0, {
        "code": "GRAPH_NOT_FOUND" if "not found" in (error or "") else
                "ROUND_NOT_SERVED",
        "severity": "ERROR",
        "message": error,
        "field_path": "output.error",
    })
    return graph


# --------------------------------------------------------------------------
# state
# --------------------------------------------------------------------------

_CHUNK = 80 * 1024


def _reject_nonjson(token):
    """json.load accepts bare NaN/Infinity tokens by DEFAULT -- they are Python
    extensions, not JSON. Left alone they travel all the way to the reply, where
    a consumer's JSON.parse rejects the whole graph. Refuse them at the door,
    where the message can still name the file."""
    raise ValueError(
        "input contains %s, which is not JSON. Numbers must be finite." % token)


def _load_json(path):
    """The only place this module reads JSON. Strict by construction."""
    with open(path, encoding="utf8") as fh:
        return json.load(fh, parse_constant=_reject_nonjson)


def _dump(obj):
    """One serializer everywhere: sorted keys, stable separators, trailing NL."""
    return json.dumps(obj, indent=2, sort_keys=True, ensure_ascii=False,
                      allow_nan=False) + "\n"


def save_state(graph, dir_path):
    # Every blob is serialized BEFORE the first file is opened. Serialization is
    # the step that can fail -- a non-finite number raises here -- and a failure
    # partway through the writes left things/papers/links/gaps/meta on disk with
    # no findings/ at all. load_state tolerates a missing findings dir, so the
    # next round loaded that graph with ZERO findings and reported nothing wrong.
    # Building the whole set first turns a corrupting half-write into a clean
    # refusal that changes nothing.
    pending = []
    for name in ("things", "papers", "links", "gaps"):
        pending.append((os.path.join(dir_path, name + ".json"),
                        _dump(graph.get(name, []))))
    meta = {k: graph.get(k) for k in
            ("schema_version", "graph_id", "question", "round", "status",
             "generated_at", "error", "coverage", "rounds")}
    pending.append((os.path.join(dir_path, "meta.json"), _dump(meta)))
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
            pending.append((path, _dump(part)))

    os.makedirs(os.path.join(dir_path, "findings"), exist_ok=True)
    for path, blob in pending:
        with open(path, "w", encoding="utf8") as fh:
            fh.write(blob)
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
            out.update(json.load(fh, parse_constant=_reject_nonjson))
    for name in ("things", "papers", "links", "gaps"):
        p = os.path.join(dir_path, name + ".json")
        if os.path.isfile(p):
            with open(p, encoding="utf8") as fh:
                out[name] = json.load(fh, parse_constant=_reject_nonjson)
    fdir = os.path.join(dir_path, "findings")
    findings = []
    if os.path.isdir(fdir):
        for fn in sorted(os.listdir(fdir)):        # sorted: determinism
            if fn.endswith(".json"):
                with open(os.path.join(fdir, fn), encoding="utf8") as fh:
                    findings.extend(json.load(fh, parse_constant=_reject_nonjson))
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

    # The text a quote was checked against is provenance, and cli() strips
    # source_text before emitting. Hash it here or lose it.
    for p in papers:
        text = p.get("source_text")
        if text:
            # Recompute, never keep the old one: a paper re-supplied with a
            # different excerpt is verified against the NEW text, so a stale
            # hash would attest to text nobody checked against.
            p["source_sha256"] = hashlib.sha256(text.encode("utf8")).hexdigest()
        else:
            p.setdefault("source_sha256", None)

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
            # A caller cannot vouch for its own quote. Whatever the round
            # asserts here is dropped; only the string match below, or a value
            # a PRIOR round earned, may say a quote is verified. Without this,
            # sending quote_verified:true with no source_text got an invented
            # sentence graded MODERATE and reported as checked.
            f.pop("quote_verified", None)
        text = (src.get(f.get("paper")) or {}).get("source_text", "")
        if text:
            if not verify_quote(f.get("quote"), text):
                if is_new:
                    discarded += 1
                    continue
                # A STORED finding is evidence already in the graph. A later
                # round supplying a shorter excerpt of the same paper would
                # fail it and delete it -- losing evidence because of what this
                # round happened to fetch. Mark it instead: QUOTE_MISMATCH says
                # so, and a human decides.
                f["quote_verified"] = False
            else:
                f["quote_verified"] = True
        elif f.get("quote_verified") not in (True, False):
            # No source text means verification never RAN. That is not the same
            # as a quote that passed, and it must never be presented as one:
            # keep the finding -- dropping it would lose evidence the caller
            # did supply -- and mark it null so every consumer downstream can
            # see the check did not happen. Fails closed by construction: the
            # field is only ever set True on the branch above, where a real
            # string match succeeded.
            f["quote_verified"] = None
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

    # A link id must name the SAME relationship next round. Numbering by
    # position (L1..Ln over the sorted triples) meant one new relationship
    # sorting early renumbered every link after it -- and mark_changed and
    # compute_delta both key on the id, so links_added/links_changed then
    # reported movement for relationships that had not moved. So an id is
    # bound to its (from, how, to) triple the round it first appears, carried
    # through entity merges, and never reused.
    #
    # Reuse is the other half of stability. Taking the high-water mark from the
    # PRIOR links alone frees the number of any link that stopped existing, and
    # a later round re-mints it for a different relationship -- so an id a
    # consumer stored still resolves, and now means something else. The counter
    # is therefore carried in coverage, where it survives the row's removal.
    link_id_by_key, used_nums = {}, set()
    seq = dict((prior.get("coverage") or {}).get("id_seq") or {})
    used_nums.update(range(1, int(seq.get("link") or 0) + 1))
    for l in sorted(prior.get("links") or [],
                    key=lambda r: (len(str(r.get("id"))), str(r.get("id")))):
        key = (tmap_existing.get(l.get("from"), l.get("from")),
               l.get("how"),
               tmap_existing.get(l.get("to"), l.get("to")))
        link_id_by_key.setdefault(key, l.get("id"))
        lid = str(l.get("id") or "")
        if lid.startswith("L") and lid[1:].isdigit():
            used_nums.add(int(lid[1:]))
    next_num = 1

    links = []
    for key in sorted(grouped, key=lambda k: tuple(str(x) for x in k)):
        lid = link_id_by_key.get(key)
        if lid is None:
            while next_num in used_nums:
                next_num += 1
            lid = "L%d" % next_num
            used_nums.add(next_num)
            link_id_by_key[key] = lid
        fs = grouped[key]
        own = _own(fs)
        yes = [f for f in own if f.get("says") == "yes"]
        no = [f for f in own if f.get("says") == "no"]
        ne = [f for f in own if f.get("says") == "no_effect"]
        state = link_state(len(yes), len(no), len(ne))
        links.append({
            "id": lid,
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
                     findings=kept, id_floor=int(seq.get("gap") or 0))
    outcome = round_outcome(prior.get("links") or [], links)

    cov = dict(coverage or {})
    cov["id_seq"] = {
        "link": max([int(str(l["id"])[1:]) for l in links
                     if str(l.get("id", ""))[1:].isdigit()] + [0]
                    + [int(seq.get("link") or 0)]),
        "gap": max([int(str(g["id"])[1:]) for g in gaps
                    if str(g.get("id", ""))[1:].isdigit()] + [0]
                   + [int(seq.get("gap") or 0)]),
    }
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
    graph["coverage"] = normalize_coverage(graph["coverage"], kept)
    graph["interpretability"] = build_interpretability(graph)
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
        index = _load_json(idx_path)
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
    ap.add_argument("--rebuild", metavar="GRAPH_JSON",
                    help="re-derive coverage defaults and the interpretability "
                         "block for an existing graph JSON, then re-emit it. "
                         "Pure: no assembly, no memory access, no score "
                         "changes. Use to upgrade a graph assembled before "
                         "the interpretability contract existed.")
    ap.add_argument("--source-dir", metavar="DIR",
                    help="with --rebuild: re-verify every quote against "
                         "<DIR>/<paper_id>.txt. A paper with no file keeps "
                         "quote_verified=null -- never true.")
    a = ap.parse_args(argv)

    if a.selftest:
        return selftest()
    if a.rebuild:
        g = _load_json(a.rebuild)
        if a.source_dir:
            g = reverify(g, a.source_dir)
        g["coverage"] = normalize_coverage(g.get("coverage"),
                                           g.get("findings") or [])
        g["interpretability"] = build_interpretability(g)
        if a.save:
            # Otherwise the next round loads findings with no quote_verified,
            # fails them closed, and throws away the verification just done.
            if not a.memory_dir:
                raise SystemExit("--save needs --memory-dir")
            save_state(g, os.path.join(a.memory_dir, g.get("graph_id")))
        _emit(g, a.out)
        return 0
    if a.list:
        idx = os.path.join(a.memory_dir or "", "index.json")
        _emit(_load_json(idx) if os.path.isfile(idx) else {}, a.out)
        return 0
    if a.show:
        if not a.memory_dir:
            raise SystemExit("--show needs --memory-dir")
        d = os.path.join(a.memory_dir, a.show)
        if not os.path.isdir(d):
            _emit(abstain_graph(a.show, {},
                                "graph %r not found in memory" % a.show), a.out)
            return 1
        g = load_state(d)
        g["graph_id"] = a.show
        # delta is deliberately absent: a read-back replays stored state and
        # has no prior round to diff against. interpretability is not -- it is
        # a pure function of the graph, so it rebuilds here for graphs that
        # were assembled before this contract existed.
        g["coverage"] = normalize_coverage(g.get("coverage"),
                                           g.get("findings") or [])
        g["interpretability"] = build_interpretability(g)
        _emit(g, a.out)
        return 0
    if not a.input:
        ap.error("--input is required (or --selftest)")

    r = _load_json(a.input)

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
            _emit(abstain_graph(
                gid, r,
                "graph_id %r not found in memory; nothing to extend" % gid),
                a.out)
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
    # status, years and defaults_applied are applied above, after main() ran.
    # The block is a pure function of the graph, so rebuild it on the FINAL
    # graph rather than shipping one that describes an intermediate state.
    graph["coverage"] = normalize_coverage(graph.get("coverage"),
                                           graph.get("findings") or [])
    graph["interpretability"] = build_interpretability(graph)

    if a.save:
        if not a.memory_dir:
            raise SystemExit("--save needs --memory-dir")
        save_state(graph, os.path.join(a.memory_dir, gid))
        idx_path = os.path.join(a.memory_dir, "index.json")
        index = {}
        if os.path.isfile(idx_path):
            with open(idx_path, encoding="utf8") as fh:
                index = _load_json(idx_path)
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
    check("spacing around punctuation does not decide verbatim",
          verify_quote("GST-pi was decreased (Figure 5F).",
                       "...gst-pi was decreased ( figure 5f ). taken together"))
    check("text fused across a table row is still rejected",
          not verify_quote("NAC 12431",
                           "compound dose n\nNAC 124 31\nvitamin e 90 22"))
    check("and its unspaced form too",
          not verify_quote("NAC12431",
                           "compound dose n\nNAC 124 31\nvitamin e 90 22"))
    check("a quote split across a line break still verifies",
          verify_quote("Alpha inhibits Beta in cultured cells.",
                       "we found that\nAlpha inhibits\nBeta in cultured cells."))
    check("stitching two non-adjacent sentences is still rejected",
          not verify_quote("Antioxidants raise risk. Our results suggest more.",
                           "Antioxidants raise risk 41-43. Something else "
                           "entirely. Our results suggest more."))
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

    # ----------------------------------------------------------------
    # interpretability contract (schema/interpretability.schema.json)
    # ----------------------------------------------------------------
    gi = main(None, finds, papers, 1, "new_question", question="q",
              graph_id="g_test", generated_at="T", new_things=things,
              coverage={"found": 9, "read": 4, "used": 2, "truncated": True,
                        "stop_reason": "max_papers", "depth": "quick",
                        "limits": {"max_papers": 10, "max_queries": 2}})
    ip = gi.get("interpretability") or {}
    check("interpretability present", bool(ip))
    check("interpretability schema_version pinned",
          ip.get("schema_version") == "1.0.0")

    def _ids(rows):
        return [r.get("id") for r in (rows or [])]

    def _unique(rows):
        got = _ids(rows)
        return len(got) == len(set(got)) and all(got)

    check("interpretability ids unique",
          all(_unique(ip.get(k)) for k in
              ("metrics", "steps", "evidence", "assumptions")))
    _ev = set(_ids(ip.get("evidence")))
    _as = set(_ids(ip.get("assumptions")))
    _me = set(_ids(ip.get("metrics")))
    _dangling = [
        (r.get("id"), i)
        for r in (ip.get("metrics") or []) + (ip.get("steps") or [])
        for key, pool in (("evidence_ids", _ev), ("assumption_ids", _as))
        for i in (r.get(key) or []) if i not in pool
    ] + [("interval", iv.get("metric_id"))
         for iv in ((ip.get("uncertainty") or {}).get("intervals") or [])
         if iv.get("metric_id") not in _me]
    check("interpretability references resolve", not _dangling)
    check("numeric metrics carry a unit",
          all(m.get("unit") for m in (ip.get("metrics") or [])
              if isinstance(m.get("value"), (int, float))
              and not isinstance(m.get("value"), bool)))
    check("ids are not positional",
          all(str(i).split(".")[0] in ("metric", "step", "evidence",
                                       "assumption")
              for i in _me | _ev | _as))

    _codes = {l.get("code") for l in (ip.get("limitations") or [])}
    check("confidence declared heuristic",
          "HEURISTIC_CONFIDENCE" in _codes
          and "heuristic" in (ip.get("uncertainty") or {}).get("method", "").lower())
    check("interval says what it is",
          all(iv.get("interval_type")
              for iv in ((ip.get("uncertainty") or {}).get("intervals") or [])))
    check("search coverage carried in extensions",
          (ip.get("extensions") or {}).get("search", {}).get("stop_reason")
          == "max_papers")
    check("truncation reported", "TRUNCATED_SEARCH" in _codes)

    # Unknown must stay null with a limitation, never 0/false/"".
    check("queries absent -> empty plus limitation",
          (ip["extensions"]["search"]["queries"] == []
           and "QUERIES_NOT_RECORDED" in _codes))
    _years = [a for a in ip["assumptions"] if a["id"] == "assumption.years"]
    check("unknown year window stays null",
          len(_years) == 1 and _years[0]["value"] is None
          and "YEARS_NOT_SPECIFIED" in _codes)

    # Quote verification must fail CLOSED. A paper with no source text means the
    # quote was never checked -- that is not the same as verified.
    _unchecked = main(None,
                      [{"id": "f1", "paper": "p1", "from": "t1",
                        "how": "inhibits", "to": "t2", "says": "yes",
                        "is_own_result": True, "quote": "Never checked."}],
                      [{"id": "p1", "doi": "10.1/x", "title": "X",
                        "study_type": "animal"}],
                      1, "new_question", question="q", graph_id="g_u",
                      generated_at="T", new_things=things)
    check("unverifiable quote kept, not silently dropped",
          len(_unchecked["findings"]) == 1)
    check("unchecked quote is null, never true",
          _unchecked["findings"][0].get("quote_verified") is None)
    check("unchecked quote counted",
          _unchecked["coverage"].get("quotes_unverified") == 1)
    _uc = {l.get("code") for l in _unchecked["interpretability"]["limitations"]}
    check("unchecked quote raises UNVERIFIED_QUOTE", "UNVERIFIED_QUOTE" in _uc)
    check("unverified evidence is not graded above LOW",
          all(e["grade"] in ("LOW", "UNSUPPORTED")
              for e in _unchecked["interpretability"]["evidence"]))
    check("aggregate quote_verified is false when any is unchecked",
          _unchecked["interpretability"]["extensions"]["quote_verified"] is False)
    check("verified quote marked true",
          all(f.get("quote_verified") is True for f in gi["findings"]))
    check("source hash recorded when source text supplied",
          all(len(p.get("source_sha256") or "") == 64 for p in gi["papers"]))
    check("no source text -> null hash, not a fabricated one",
          _unchecked["papers"][0].get("source_sha256") is None)

    # Empty arrays are legal only when a limitation explains them.
    _empty = main(None, [], [], 1, "new_question", question="q",
                  graph_id="g_e", generated_at="T")
    _ec = {l.get("code") for l in _empty["interpretability"]["limitations"]}
    check("empty graph abstains rather than concluding",
          _empty["interpretability"]["headline"]["status"] == "INCONCLUSIVE")
    check("empty arrays are explained",
          all("EMPTY_%s" % k.upper() in _ec
              for k in ("evidence", "counterfactuals")
              if not _empty["interpretability"][k]))

    # Ids are built from strings the CALLER chose. `defaults_applied` is
    # documented as prose and finding ids come from the extractor, so both can
    # carry spaces, colons and parentheses -- which used to produce ids that
    # failed the shared contract's own pattern and sank the whole graph.
    _idpat = re.compile(r"^[a-z][a-z0-9_]*\.[A-Za-z0-9_.:\-]+$")
    _messy = main(None,
                  [{"id": "f 1 (a)", "paper": "p1", "from": "t1",
                    "how": "inhibits", "to": "t2", "says": "yes",
                    "is_own_result": True, "quote": "Alpha inhibits Beta in cultured cells."}],
                  [{"id": "p1", "doi": "10.1/a", "study_type": "animal",
                    "source_text": text}],
                  1, "new_question", question="q", graph_id="g_m",
                  generated_at="T", new_things=things,
                  coverage={"defaults_applied": ["depth: standard (not supplied)",
                                                 "years: none (not supplied)"]})
    _mi = _messy["interpretability"]
    _all_ids = ([r["id"] for k in ("metrics", "steps", "evidence", "assumptions")
                 for r in _mi[k]])
    check("free-text ids are slugged, not emitted raw",
          all(_idpat.match(i) for i in _all_ids))
    check("slugged ids stay unique", len(_all_ids) == len(set(_all_ids)))
    check("the prose default is preserved in basis, not lost",
          any("depth: standard (not supplied)" in a["basis"]
              for a in _mi["assumptions"]))
    check("references still resolve after slugging",
          all(i in {e["id"] for e in _mi["evidence"]}
              for m in _mi["metrics"] for i in m["evidence_ids"]))

    # Domain abstention: an extending ask against a graph that is not in memory.
    _abst = abstain_graph("g_missing", {"question": "q", "round": 4,
                                        "generated_at": "T"},
                          "graph_id 'g_missing' not found in memory")
    check("abstention still emits interpretability",
          _abst["interpretability"]["headline"]["status"] == "FAILED")
    check("abstention names the reason in a limitation",
          "GRAPH_NOT_FOUND" in {l["code"] for l
                                in _abst["interpretability"]["limitations"]})

    # Relationship ids must survive a round that inserts a link sorting FIRST,
    # because mark_changed and compute_delta both key on the id: renumbering
    # makes links_added/links_changed name the wrong relationships.
    _d3 = _tf.mkdtemp()
    _txt = "Mid inhibits Zed. Aaa inhibits Zed."
    _r1 = main(None,
               [{"id": "f1", "paper": "p1", "from": "t1", "how": "inhibits",
                 "to": "t2", "says": "yes", "is_own_result": True,
                 "quote": "Mid inhibits Zed."}],
               [{"id": "p1", "doi": "10.1/m", "title": "M",
                 "study_type": "animal", "source_text": _txt}],
               1, "new_question", question="q", graph_id="g_s",
               generated_at="T",
               new_things=[{"id": "t1", "name": "Mid", "kind": "gene"},
                           {"id": "t2", "name": "Zed", "kind": "gene"}])
    _first = {(l["from"], l["how"], l["to"]): l["id"] for l in _r1["links"]}
    save_state(_r1, _d3)
    _r2 = main(_d3,
               [{"id": "f9", "paper": "p2", "from": "t9", "how": "inhibits",
                 "to": "t2", "says": "yes", "is_own_result": True,
                 "quote": "Aaa inhibits Zed."}],
               [{"id": "p2", "doi": "10.1/a", "title": "A",
                 "study_type": "animal", "source_text": _txt}],
               2, "expand_node", graph_id="g_s", generated_at="T",
               new_things=[{"id": "t9", "name": "Aaa", "kind": "gene"}])
    _second = {(l["from"], l["how"], l["to"]): l["id"] for l in _r2["links"]}
    check("link ids stable when a new link sorts first",
          all(_second.get(k) == v for k, v in _first.items()))
    check("a genuinely new link gets a fresh id",
          len(set(_second.values())) == len(_second) == 2)
    check("delta names the actually-new link",
          _r2["delta"]["links_added"] ==
          sorted(set(_second.values()) - set(_first.values())))

    # A caller cannot vouch for its own quote.
    check("the headline sentence agrees in number",
          "1 relationship " in main(
              None,
              [{"id": "f1", "paper": "p1", "from": "t1", "how": "inhibits",
                "to": "t2", "says": "yes", "is_own_result": True,
                "quote": "Alpha inhibits Beta in cultured cells."}],
              [{"id": "p1", "doi": "10.1/a", "study_type": "animal",
                "is_preprint": False, "source_text": text}],
              1, "new_question", question="q", graph_id="g_pl",
              generated_at="T", new_things=things
          )["interpretability"]["headline"]["plain_language"])

    _asserted = main(None,
                     [{"id": "f1", "paper": "p1", "from": "t1", "how": "inhibits",
                       "to": "t2", "says": "yes", "is_own_result": True,
                       "quote": "A sentence in no paper.", "quote_verified": True}],
                     [{"id": "p1", "doi": "10.1/x", "study_type": "animal"}],
                     1, "new_question", question="q", graph_id="g_a",
                     generated_at="T", new_things=things)
    check("caller-asserted quote_verified is not trusted",
          _asserted["findings"][0].get("quote_verified") is None)
    check("caller-asserted quote is not graded as checked",
          _asserted["interpretability"]["evidence"][0]["grade"] == "LOW")

    # A stored finding must not be deleted because a later round happened to
    # fetch a shorter excerpt of the same paper.
    _d5 = _tf.mkdtemp()
    save_state({"schema_version": "1.1", "graph_id": "g_x", "round": 1,
                "things": [{"id": "t1", "name": "Alpha", "kind": "gene"},
                           {"id": "t2", "name": "Beta", "kind": "gene"}],
                "papers": [{"id": "p1", "doi": "10.1/a"}],
                "findings": [{"id": "f1", "paper": "p1", "from": "t1",
                              "how": "inhibits", "to": "t2", "says": "yes",
                              "is_own_result": True, "round": 1,
                              "quote_verified": True,
                              "quote": "Alpha inhibits Beta in cultured cells."}],
                "links": [], "gaps": [], "coverage": {}, "rounds": []}, _d5)
    _short = main(_d5, [], [{"id": "p1", "doi": "10.1/a",
                             "source_text": "Gamma had no effect."}],
                  2, "expand_node", graph_id="g_x", generated_at="T")
    check("a shorter excerpt does not delete stored evidence",
          len(_short["findings"]) == 1)
    check("it is marked mismatched rather than dropped",
          _short["findings"][0].get("quote_verified") is False)
    check("and QUOTE_MISMATCH says so",
          "QUOTE_MISMATCH" in {l["code"] for l
                               in _short["interpretability"]["limitations"]})

    # An id a consumer stored must never come back meaning something else.
    _d6 = _tf.mkdtemp()
    _r1 = main(None, finds, papers, 1, "new_question", question="q",
               graph_id="g_seq", generated_at="T", new_things=things)
    _high = _r1["coverage"]["id_seq"]["link"]
    _dropped = dict(_r1)
    _dropped["findings"] = []
    save_state(_dropped, _d6)
    _r2 = main(_d6, [], [], 2, "expand_node", graph_id="g_seq", generated_at="T")
    check("the id counter survives the rows it numbered",
          _r2["coverage"]["id_seq"]["link"] == _high)

    # Untrusted text must not become markup in a display string.
    _html = abstain_graph("g_h", {}, "<html><body>502 Bad Gateway</body></html>")
    check("an HTML error page does not become HTML in the headline",
          "<" not in _html["interpretability"]["headline"]["plain_language"])

    # A substituted default is synthetic, and the headline has to say so.
    _syn = main(None, [], [], 1, "new_question", question="q", graph_id="g_y",
                generated_at="T",
                coverage={"defaults_applied": ["depth: standard (not supplied)"]})
    check("a synthetic assumption reaches headline.basis",
          "SYNTHETIC" in _syn["interpretability"]["headline"]["basis"])
    check("a round that read nothing does not claim OBSERVED",
          "OBSERVED" not in _syn["interpretability"]["headline"]["basis"])

    # Unknown is null plus a limitation, never 0 and never the token "None".
    _unk = main(None, [], [], 1, "new_question", question="q", graph_id="g_u2",
                generated_at="T", coverage={})
    _uc2 = {l["code"] for l in _unk["interpretability"]["limitations"]}
    check("unrecorded coverage counts raise a limitation",
          "COVERAGE_NOT_RECORDED" in _uc2)
    check("unrecorded counts never render as the token None",
          not any("None" in m["display"]
                  for m in _unk["interpretability"]["metrics"]))
    check("unrecorded counts are null, not zero",
          _unk["interpretability"]["metrics"][0]["value"] is None)

    # Every null must be inventoried: the contract is null PLUS a limitation,
    # and a consumer should be able to enumerate the unknowns without knowing
    # which specific code covers which field.
    _nul = main(None, [], [], 1, "new_question", question="q", graph_id="g_n2",
                generated_at="T", coverage={})
    _ni = _nul["interpretability"]
    _inv = [l for l in _ni["limitations"] if l["code"] == "UNKNOWN_VALUE"]
    _nulls = ([m["id"] for m in _ni["metrics"] if m["value"] is None]
              + [st["id"] for st in _ni["steps"] if st["result"]["value"] is None]
              + [a["id"] for a in _ni["assumptions"] if a["value"] is None])
    check("every null value is inventoried",
          bool(_nulls) and len(_inv) == 1
          and all(n in _inv[0]["message"] for n in _nulls))

    # An absent is_preprint is unknown, not false.
    _pp = main(None,
               [{"id": "f1", "paper": "p1", "from": "t1", "how": "inhibits",
                 "to": "t2", "says": "yes", "is_own_result": True,
                 "quote": "Alpha inhibits Beta in cultured cells."}],
               [{"id": "p1", "doi": "10.1/a", "study_type": "animal",
                 "source_text": text}],
               1, "new_question", question="q", graph_id="g_pp",
               generated_at="T", new_things=things)
    check("an unrecorded preprint flag is not asserted as a publication",
          _pp["interpretability"]["evidence"][0]["source_type"]
          == "publication_status_unrecorded")
    check("and the escaped preprint penalty is reported",
          "PREPRINT_STATUS_UNRECORDED" in {l["code"] for l
                                           in _pp["interpretability"]["limitations"]})

    # A rebuild that cannot re-fetch a paper must not erase what an earlier
    # rebuild already established about its quotes.
    _known = {"papers": [{"id": "p1"}],
              "findings": [{"id": "f1", "paper": "p1", "quote": "x",
                            "quote_verified": False},
                           {"id": "f2", "paper": "p1", "quote": "y",
                            "quote_verified": True}]}
    reverify(_known, _tf.mkdtemp())          # empty dir: nothing to check against
    check("a rebuild keeps a known mismatch",
          _known["findings"][0]["quote_verified"] is False)
    check("a rebuild keeps a known pass",
          _known["findings"][1]["quote_verified"] is True)

    # An id naming a row that does not exist is reported, not deleted.
    _dang = main(None,
                 [{"id": "f1", "paper": "p404", "from": "t1", "how": "inhibits",
                   "to": "t404", "says": "yes", "is_own_result": True,
                   "quote": "Alpha inhibits Beta in cultured cells."}],
                 [{"id": "p1", "doi": "10.1/a", "study_type": "animal"}],
                 1, "new_question", question="q", graph_id="g_dang",
                 generated_at="T", new_things=things)
    _dl = [l for l in _dang["interpretability"]["limitations"]
           if l["code"] == "DANGLING_REFERENCE"]
    check("an unresolvable id is reported", len(_dl) == 1)
    check("and the finding is kept, not deleted", len(_dang["findings"]) == 1)
    check("the dangling ids are named", "t404" in _dl[0]["message"]
          and "p404" in _dl[0]["message"])

    # Strict JSON: Python emits bare NaN/Infinity tokens unless told not to,
    # and json.load ACCEPTS them by default -- so a bundle carrying NaN parsed
    # cleanly, travelled the whole pipeline, and produced a reply that no
    # JSON.parse would accept.
    try:
        _dump({"x": float("inf")})
        _strict = False
    except ValueError:
        _strict = True
    check("serializer refuses NaN/Infinity", _strict)

    _nan_path = os.path.join(_tf.mkdtemp(), "b.json")
    with open(_nan_path, "w", encoding="utf8") as _fh:
        _fh.write('{"findings": [{"confidence": NaN}]}')
    try:
        _load_json(_nan_path)
        _rejected = False
    except ValueError:
        _rejected = True
    check("reader refuses NaN/Infinity on the way IN", _rejected)

    # A serialization failure must change nothing on disk. It used to write
    # things/papers/links/gaps/meta and then raise before findings/, and
    # load_state tolerates a missing findings dir -- so the next round loaded
    # that graph with zero findings and reported nothing wrong.
    _d4 = _tf.mkdtemp()
    try:
        save_state({"schema_version": "1.1", "graph_id": "g_n", "round": 1,
                    "things": [{"id": "t1", "name": "A", "kind": "gene"}],
                    "papers": [{"id": "p1"}], "links": [], "gaps": [],
                    "coverage": {}, "rounds": [],
                    "findings": [{"id": "f1", "paper": "p1", "round": 1,
                                  "confidence": float("nan")}]}, _d4)
    except ValueError:
        pass
    check("a refused save writes nothing at all",
          sum(len(fs) for _, _, fs in os.walk(_d4)) == 0)
    check("output parses as strict JSON",
          json.loads(_dump(gi))["interpretability"]["schema_version"] == "1.0.0")

    print("\nSELFTEST:", "GREEN" if ok else "RED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(cli())

