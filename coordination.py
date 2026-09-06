"""
Coordination detection, rebuilt around what the literature actually measures.

WHY THIS REPLACES THE OLD DETECTOR
----------------------------------
The previous implementation (app.detect_coordination) compared 4-word shingles
between documents on DIFFERENT platforms, looked only at the 100 most-engaged
documents, and cited at most five example pairs. The red-team harness measured
what that produces:

  * 0% recall on a 64-post identical-text campaign, because campaign posts are
    low-engagement by nature and never entered the top-100 comparison window —
    the detector was looking exactly where campaigns are not.
  * Recall FALLING as the campaign grew (75% at 8 posts, 9% at 64), because the
    number of cited documents was capped at five pairs regardless of scale.
  * 16-80 "coordination score" on corpora with NO campaign in them at all,
    spanning low, medium and high risk on pure organic traffic.

Three design choices drive this module:

1. TRAITS, NOT TEXT. Luceri et al. measured trace AUCs: co-URL 0.72, co-retweet
   0.69, text similarity 0.52. Text similarity — the only thing the old detector
   used — is the weakest available signal and the easiest to evade with one
   rewriting pass. Co-sharing behaviour is what survives paraphrase.

2. ACTORS, NOT DOCUMENTS. Coordination is a property of accounts acting
   together, not of posts resembling each other. The unit of analysis is the
   actor-trait bipartite graph (Pacheco et al.): actors -> traits, projected to
   actor-actor similarity, filtered, then clustered.

3. RELATIVE, NOT ABSOLUTE. A raw score means nothing without a matched organic
   baseline (Ferrara: confirmed operations sit 7-70x above baseline). `detect()`
   accepts a baseline and reports the multiple. Without one it says so, and
   declines to hand out a risk band it cannot justify.

No third-party dependencies: corpora here are hundreds of documents, not
millions, and adding numpy/sklearn to requirements.txt to save milliseconds on a
600-document graph would be a deployment risk bought with nothing.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from datetime import datetime, timezone
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

# ── URL canonicalisation ─────────────────────────────────────────────────────
# The old detector grouped on the raw URL string, so appending a tracking
# parameter defeated it without changing the destination. That is the cheapest
# evasion in existence and every marketing tool does it by accident.
_TRACKING = re.compile(
    r"^(utm_|ga_|mc_|pk_|hsa_|vero_|mkt_|trk|ref|referrer|source|fbclid|gclid|"
    r"dclid|msclkid|igshid|twclid|si|feature|spm|scm|share_id|__twitter_impression)",
    re.I)
_AMP = re.compile(r"^(amp|m|mobile)\.")


def canonical_url(u: str) -> str:
    """Strip a URL to its destination. Empty string for anything unusable."""
    if not u or not isinstance(u, str):
        return ""
    try:
        p = urlsplit(u.strip())
        if p.scheme not in ("http", "https", ""):
            return ""
        host = (p.netloc or "").lower().split("@")[-1]
        if host.startswith("www."):
            host = host[4:]
        host = _AMP.sub("", host)
        host = re.sub(r":(80|443)$", "", host)
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=False)
             if not _TRACKING.match(k)]
        q.sort()
        path = re.sub(r"/+$", "", p.path) or "/"
        # Fragments are never part of the destination for our purposes.
        return urlunsplit(("https", host, path, urlencode(q), ""))
    except Exception:
        return ""


def _actor(doc: dict) -> str:
    """Who posted. Falls back to the host, which is right for news wires."""
    a = (doc.get("author") or "").strip().lower()
    if a:
        return f"{doc.get('platform','?')}:{a}"
    host = urlsplit(doc.get("url") or "").netloc.lower()
    return f"{doc.get('platform','?')}:{host or 'unknown'}"


_WORD = re.compile(r"[a-z0-9']+")


def _shingles(text: str, k: int = 5) -> set[str]:
    w = _WORD.findall((text or "").lower())
    if len(w) < k:
        return {" ".join(w)} if w else set()
    return {" ".join(w[i:i + k]) for i in range(len(w) - k + 1)}


def _parse_dt(v):
    if not v:
        return None
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except Exception:
        return None


# ── Bipartite construction ───────────────────────────────────────────────────

def _traits(docs: list[dict], bucket_minutes: int = 5) -> dict[str, dict[str, set]]:
    """actor -> {trait_kind -> set(trait tokens)}.

    Four traits, each a different way of being the same operation:
      url     the canonical destination shared
      text    5-word shingles (kept, but no longer the only signal)
      hashtag the tag set used
      time    coarse time buckets posted into
    """
    out: dict[str, dict[str, set]] = defaultdict(
        lambda: {"url": set(), "text": set(), "hashtag": set(), "time": set()})
    for d in docs:
        a = _actor(d)
        cu = canonical_url(d.get("url") or "")
        if cu:
            out[a]["url"].add(cu)
        blob = f"{d.get('title') or ''} {d.get('excerpt') or ''}"
        out[a]["text"] |= _shingles(blob)
        for tag in re.findall(r"#(\w{3,40})", blob):
            out[a]["hashtag"].add(tag.lower())
        dt = _parse_dt(d.get("timestamp"))
        if dt:
            ep = int(dt.replace(tzinfo=dt.tzinfo or timezone.utc).timestamp())
            out[a]["time"].add(ep // (bucket_minutes * 60))
    return out


def _idf(actor_traits: dict, kind: str) -> dict:
    """Inverse document frequency over traits.

    Without it, the single most common trait dominates every similarity: every
    actor posts in *some* time bucket, and a link everyone shares (the story
    itself) carries no evidence of coordination. IDF is what makes a RARE shared
    trait count and a universal one not.
    """
    n = max(1, len(actor_traits))
    df: dict = defaultdict(int)
    for t in actor_traits.values():
        for tok in t[kind]:
            df[tok] += 1
    return {tok: math.log(n / c) for tok, c in df.items() if c >= 1}


def _cosine_pairs(actor_traits: dict, kind: str, min_shared: int = 1,
                  min_sim: float = 0.30) -> dict[tuple, float]:
    """TF-IDF cosine between actors over one trait, via an inverted index.

    The inverted index matters: comparing every actor to every other is O(n^2)
    and, worse, spends nearly all of it on pairs that share nothing. Only actors
    that co-occur on at least one trait token are ever scored.
    """
    idf = _idf(actor_traits, kind)
    norms: dict[str, float] = {}
    postings: dict = defaultdict(list)
    for a, t in actor_traits.items():
        toks = t[kind]
        if not toks:
            continue
        norms[a] = math.sqrt(sum(idf.get(x, 0.0) ** 2 for x in toks)) or 1e-9
        for tok in toks:
            postings[tok].append(a)

    dot: dict[tuple, float] = defaultdict(float)
    shared: dict[tuple, int] = defaultdict(int)
    for tok, actors in postings.items():
        # A trait shared by nearly everyone is the topic, not the operation.
        if len(actors) < 2 or len(actors) > max(2, len(actor_traits) * 0.5):
            continue
        w = idf.get(tok, 0.0) ** 2
        for i in range(len(actors)):
            for j in range(i + 1, len(actors)):
                key = (actors[i], actors[j]) if actors[i] < actors[j] else (actors[j], actors[i])
                dot[key] += w
                shared[key] += 1

    out = {}
    for key, num in dot.items():
        if shared[key] < min_shared:
            continue
        sim = num / (norms[key[0]] * norms[key[1]])
        if sim >= min_sim:
            out[key] = min(1.0, sim)
    return out


def disparity_filter(edges: dict[tuple, float], alpha: float = 0.30,
                     keep_above: float = 0.22) -> dict[tuple, float]:
    """Serrano et al. multiscale backbone, with an absolute-weight escape hatch.

    A global similarity cutoff is wrong on a graph with a heavy-tailed degree
    distribution: it erases the whole neighbourhood of a lightly-connected actor
    while leaving a hub's dense but unremarkable edges intact. The disparity
    filter asks, per node, whether an edge carries more weight than a uniform
    random split of that node's own strength would predict, so an edge is kept
    if it is significant to EITHER endpoint.

    THE ESCAPE HATCH IS NOT AN OPTIMISATION — it fixes a real inversion.

    The filter tests whether an edge STANDS OUT against its node's other edges.
    A coordinated cluster is the one structure where that test backfires: 16
    accounts posting identical text form a near-complete clique of uniform
    weight, so every edge is exactly average for its node and NOTHING is
    exceptional. Measured on the harness, this silently discarded 120 campaign
    pairs at cosine 1.0 and left the detector reporting only organic noise —
    0% recall on a campaign it had already found perfectly.

    So: an edge survives if it is locally exceptional OR if its absolute weight
    is high enough to be evidence on its own. Two accounts with near-identical
    trait vectors are a finding whether or not their neighbourhoods are uniform.
    """
    if not edges:
        return {}
    strength: dict = defaultdict(float)
    degree: dict = defaultdict(int)
    for (a, b), w in edges.items():
        strength[a] += w; strength[b] += w
        degree[a] += 1; degree[b] += 1

    keep: dict[tuple, float] = {}
    for (a, b), w in edges.items():
        if w >= keep_above:          # uniform cliques never pass the test below
            keep[(a, b)] = w
            continue
        for node, other in ((a, b), (b, a)):
            k = degree[node]
            if k <= 1:
                # A degree-1 node has no distribution to be exceptional against;
                # keeping it on weight alone is the honest reading.
                if w >= 0.5:
                    keep[(a, b)] = w
                continue
            p = w / strength[node] if strength[node] else 0.0
            # P(observing >= p under the null) = (1 - p)^(k - 1)
            if (1.0 - p) ** (k - 1) < alpha:
                keep[(a, b)] = w
                break
    return keep


def _components(edges: dict[tuple, float], min_size: int = 3) -> list[set]:
    """Connected components of the filtered graph, largest first.

    Leiden would resolve communities inside a large component better, but it is
    a dependency and a tuning surface. At this corpus size a filtered component
    IS the cluster; the disparity filter has already done the separating.
    """
    adj: dict = defaultdict(set)
    for a, b in edges:
        adj[a].add(b); adj[b].add(a)
    seen, comps = set(), []
    for start in adj:
        if start in seen:
            continue
        stack, comp = [start], set()
        while stack:
            n = stack.pop()
            if n in seen:
                continue
            seen.add(n); comp.add(n)
            stack.extend(adj[n] - seen)
        if len(comp) >= min_size:
            comps.append(comp)
    return sorted(comps, key=len, reverse=True)


# ── Public API ───────────────────────────────────────────────────────────────

TRAIT_WEIGHT = {          # Luceri et al. trace AUCs, normalised
    "url": 0.72,
    "hashtag": 0.60,
    "text": 0.52,
    "time": 0.45,
}


def detect(docs: list[dict], baseline: float | None = None,
           min_cluster: int = 3) -> dict:
    """Find coordinated actor clusters and the documents they produced.

    Returns `flagged_urls` — the actual documents — not only a score. A
    coordination number that points at nothing cannot be acted on, and the
    harness scores it as a miss, correctly.
    """
    docs = [d for d in docs if isinstance(d, dict)]
    if len(docs) < 6:
        return {"coordination_score": 0, "risk": "unknown", "signals": [],
                "clusters": [], "flagged_urls": [],
                "caveat": f"{len(docs)} documents — too few to assess coordination"}

    actor_traits = _traits(docs)
    by_actor: dict = defaultdict(list)
    for d in docs:
        by_actor[_actor(d)].append(d)

    per_trait, combined = {}, defaultdict(float)
    for kind, weight in TRAIT_WEIGHT.items():
        pairs = _cosine_pairs(actor_traits, kind)
        per_trait[kind] = pairs
        for key, sim in pairs.items():
            combined[key] += weight * sim

    if not combined:
        return {"coordination_score": 0, "risk": "low", "signals": [],
                "clusters": [], "flagged_urls": [],
                "caveat": "no actor pair shares a distinctive trait"}

    # Normalise by the MAXIMUM ACHIEVABLE weight (all traits at cosine 1), not by
    # the largest weight observed. Dividing by the observed max makes every
    # threshold below depend on the rest of the corpus: the same pair of
    # accounts, identical in every respect, scores 1.0 in one search and 0.5 in
    # another simply because some unrelated pair happened to be stronger. Fixed
    # denominator, comparable numbers, thresholds that mean the same thing on
    # every query.
    denom = sum(TRAIT_WEIGHT.values())
    normed = {k: min(1.0, v / denom) for k, v in combined.items()}
    backbone = disparity_filter(normed)
    clusters = _components(backbone, min_cluster)

    flagged, cluster_out = [], []
    for comp in clusters[:20]:
        urls = [d.get("url") for a in comp for d in by_actor.get(a, []) if d.get("url")]
        flagged.extend(urls)
        inner = [normed[k] for k in normed
                 if k[0] in comp and k[1] in comp]
        kinds = sorted({kind for kind, pairs in per_trait.items()
                        for k in pairs if k[0] in comp and k[1] in comp},
                       key=lambda x: -TRAIT_WEIGHT[x])
        cluster_out.append({
            "actors": sorted(comp)[:25],
            "actor_count": len(comp),
            "documents": len(urls),
            "cohesion": round(sum(inner) / len(inner), 3) if inner else 0.0,
            "traits": kinds,
            "examples": urls[:6],
        })

    # Score from the size and cohesion of what was actually found, so it moves
    # with the evidence rather than with an arbitrary pair count.
    raw = sum(c["actor_count"] * c["cohesion"] for c in cluster_out)
    score = int(min(100, round(100 * (1 - math.exp(-raw / 12.0)))))

    signals = []
    for c in cluster_out[:5]:
        signals.append({
            "type": "coordinated_actor_cluster",
            "severity": ("high" if c["actor_count"] >= 8 and c["cohesion"] > .5
                         else "medium" if c["actor_count"] >= 4 else "low"),
            "description": (f"{c['actor_count']} accounts posting {c['documents']} documents "
                            f"share {', '.join(c['traits'])} behaviour "
                            f"(cohesion {c['cohesion']})"),
            "examples": c["examples"],
        })

    out = {"coordination_score": score,
           "clusters": cluster_out,
           "signals": signals,
           "flagged_urls": sorted(set(flagged)),
           "actors": len(actor_traits),
           "trait_pairs": {k: len(v) for k, v in per_trait.items()},
           # M7. The block reported "5 shared hashtags, 12 near-identical text,
           # 39 synchronised timing" beside a single cluster of three Reddit
           # crossposters, and never said what happened to the other 53. A
           # reader could not tell whether those signals failed the filter or
           # failed the renderer, which are opposite conclusions about the same
           # screen. The funnel is now reported: candidate pairs in, backbone
           # edges out, clusters formed. A signal that did not survive is a
           # finding about the evidence, not something to hide.
           "pair_funnel": {
               "candidate_pairs": len(normed),
               "backbone_pairs": len(backbone),
               "clusters": len(cluster_out),
               "min_cluster": min_cluster,
               "note": ("The disparity filter keeps only edges that stand out "
                        "against each account's own behaviour. Pairs that were "
                        "measured but did not survive it are not shown as "
                        "clusters, and their absence is a filter decision "
                        "rather than an absence of signal."),
           }}

    # ── The band ─────────────────────────────────────────────────────────────
    # Without a matched baseline there is no defensible band, and inventing one
    # is exactly the failure the harness found: pure organic traffic scoring
    # "high". Say what is missing instead of guessing.
    if baseline is None:
        out["risk"] = "unbanded"
        out["baseline_ratio"] = None
        out["caveat"] = ("No matched organic baseline for this query, so this "
                         "score is a magnitude with nothing to compare it to. "
                         "Confirmed operations sit roughly 7-70x above baseline; "
                         "a raw number alone cannot tell you where this sits.")
    else:
        ratio = (score / baseline) if baseline > 0 else None
        out["baseline_ratio"] = round(ratio, 2) if ratio is not None else None
        out["risk"] = ("high" if ratio and ratio >= 7
                       else "medium" if ratio and ratio >= 3
                       else "low")
        if ratio is None:
            out["risk"] = "unbanded"
            out["caveat"] = ("Baseline is zero — any signal separates, but the "
                             "ratio is undefined and is not reported as one.")
    return out


# ── Captured vs manufactured reach (P4) ──────────────────────────────────────

def reach_split(docs: list[dict], clusters: list[dict]) -> dict:
    """How much of this narrative's reach the operation generated itself.

    The question an analyst actually has about a campaign is not "is it
    coordinated" but "did it work". Those are different, and conflating them is
    how a noisy, self-contained botnet gets escalated over a quiet operation
    that a real audience picked up.

    Ferrara's cross-campaign measurement is the reference point: across seven
    confirmed operations, INTERNAL amplification accounted for only 0.1-5.3% of
    top-percentile reach. Almost all the reach of a campaign that mattered came
    from accounts outside it. So:

      manufactured  engagement on documents posted BY cluster actors
      captured      engagement on everything else in the corpus

    A high manufactured share means the operation is talking to itself — real
    but contained. A low share with a large absolute volume is the one that
    should worry someone: the narrative escaped.

    This is a within-corpus ratio, not a population estimate. It says how reach
    is distributed across what XTag collected, and inherits whatever the
    collection missed.
    """
    cluster_actors: set = set()
    for c in clusters or []:
        cluster_actors |= set(c.get("actors") or [])

    manufactured = captured = 0
    m_docs = c_docs = 0
    for d in docs:
        if not isinstance(d, dict):
            continue
        eng = d.get("engagement")
        if eng is None:
            meta = d.get("meta") or {}
            eng = sum(v for v in meta.values() if isinstance(v, (int, float))) if isinstance(meta, dict) else 0
        try:
            eng = max(0, int(eng or 0))
        except Exception:
            eng = 0
        if _actor(d) in cluster_actors:
            manufactured += eng; m_docs += 1
        else:
            captured += eng; c_docs += 1

    total = manufactured + captured
    if not cluster_actors:
        return {"manufactured": 0, "captured": captured, "total": total,
                "manufactured_share": None, "verdict": "no coordinated cluster found",
                "reference": "Ferrara: confirmed operations show 0.1-5.3% internal amplification"}
    if total <= 0:
        # No engagement figures at all is a DATA gap, not a 0% finding. Several
        # sources (GDELT, RSS, Telegram scrapes) carry no engagement metric, and
        # reporting 0% manufactured off that would be an invented result.
        return {"manufactured": 0, "captured": 0, "total": 0,
                "manufactured_share": None,
                "verdict": "no engagement data on this corpus — reach cannot be split",
                "cluster_documents": m_docs}

    share = manufactured / total
    if share >= 0.50:
        verdict = ("self-contained — most measurable reach is the operation's own accounts; "
                   "it is real but has not been picked up")
    elif share >= 0.05:
        verdict = ("partly captured — above the 0.1-5.3% band seen in confirmed operations, "
                   "so a meaningful share of reach is still internal")
    else:
        verdict = ("captured — reach is overwhelmingly from accounts outside the cluster, "
                   "consistent with a narrative that escaped its originators")
    return {"manufactured": manufactured, "captured": captured, "total": total,
            "manufactured_share": round(share, 4),
            "cluster_documents": m_docs, "other_documents": c_docs,
            "verdict": verdict,
            "reference": "Ferrara: confirmed operations show 0.1-5.3% internal amplification"}
