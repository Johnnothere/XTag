"""
Persistent narrative identity — the same story, recognised across observations.

THE PROBLEM THIS SOLVES
-----------------------
XTag currently re-derives narratives from scratch on every search. Run the same
query twice and "Vaccine data was falsified" comes back as a brand-new object
with a new position in the list, so nothing can be said about it over time: not
whether it is growing, not when it started, not that it absorbed another
narrative last Tuesday. A watchlist that checks hourly produces a pile of
unrelated snapshots rather than a history.

Greene et al. (2010) is the standard answer: match each observation's clusters
against the FRONT — the most recent appearance of each living narrative — with
an optimal assignment, and carry a stable identifier across the match. A
narrative then has a birth, a life, and a death, and events (merge, split,
growth, contraction) become things that happened to a thing rather than
differences between two lists.

DESIGN NOTES, INCLUDING A LIMITATION WORTH STATING
--------------------------------------------------
* Clustering is DP-means (lambda controls granularity; no k to guess in advance,
  which matters because the number of live narratives is exactly what we do not
  know).
* Similarity is PLUGGABLE and defaults to TF-IDF cosine over words. Sentence
  embeddings are better for claim identity — lexical overlap under-counts
  paraphrase, which is the whole game here — but they need a model that does not
  currently live anywhere in this deployment. The structure below is unchanged
  by that swap: pass a different `embed` and everything else holds. Until then,
  expect this to split a narrative that has been substantially reworded, and
  read the thresholds as lexical ones.
* Two thresholds, deliberately different: claim identity is strict (~0.90,
  "these are the same claim") and narrative matching is loose (0.30 on Jaccard
  of members, per Greene).
* No third-party dependencies, including the assignment step.
"""
from __future__ import annotations

import math
import re
import uuid

import embeddings as emb
from collections import defaultdict
from dataclasses import dataclass, field

WORD = re.compile(r"[a-z0-9']+")
STOP = {"the","a","an","and","or","but","of","to","in","on","for","with","is","are",
        "was","were","be","been","it","its","this","that","these","those","as","at",
        "by","from","has","have","had","not","no","they","their","he","she","we","you"}


def tokens(text: str) -> list[str]:
    return [w for w in WORD.findall((text or "").lower()) if w not in STOP and len(w) > 2]


def tfidf(texts: list[str]) -> list[dict]:
    """Sparse L2-normalised TF-IDF vectors. Empty dict for an empty document."""
    docs = [tokens(t) for t in texts]
    n = max(1, len(docs))
    df: dict = defaultdict(int)
    for d in docs:
        for w in set(d):
            df[w] += 1
    idf = {w: math.log((1 + n) / (1 + c)) + 1.0 for w, c in df.items()}
    out = []
    for d in docs:
        tf: dict = defaultdict(float)
        for w in d:
            tf[w] += 1.0
        v = {w: (1 + math.log(c)) * idf.get(w, 0.0) for w, c in tf.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1e-9
        out.append({w: x / norm for w, x in v.items()})
    return out


def cosine(a: dict, b: dict) -> float:
    if not a or not b:
        return 0.0
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(w, 0.0) for w, x in a.items())


def _centroid(vecs: list[dict]) -> dict:
    if not vecs:
        return {}
    acc: dict = defaultdict(float)
    for v in vecs:
        for w, x in v.items():
            acc[w] += x
    norm = math.sqrt(sum(x * x for x in acc.values())) or 1e-9
    return {w: x / norm for w, x in acc.items()}


# ── DP-means ─────────────────────────────────────────────────────────────────

#: Minimum cosine for a claim to join an existing cluster rather than start a
#: new one. This is a SIMILARITY, not a DP-means distance penalty — the earlier
#: `1 - lambda` formulation quietly demanded 0.90, which is the claim-IDENTITY
#: threshold, and split every narrative into singletons.
#:
#: Measured on TF-IDF over this module's own tokeniser:
#:      restatement sharing vocabulary      0.58 - 1.00
#:      genuine paraphrase, new vocabulary  0.11 - 0.13   <-- see the caveat
#:      unrelated documents                 0.00
#: 0.45 sits cleanly above the noise floor and below the lexical-overlap band.
JOIN_SIM = 0.45


def dp_means(vectors: list[dict], join_sim: float = JOIN_SIM, iters: int = 12,
             lam: float | None = None) -> list[list[int]]:
    """Cluster without choosing k, opening a new cluster when nothing is close.

    `join_sim` is the minimum cosine to a centroid for a point to join it.
    Higher = more, tighter narratives. Lower = fewer, broader ones.

    MEASURED LIMITATION, because it decides how to read the output: TF-IDF
    scores a true paraphrase ("the ministry falsified vaccine data" vs "ministry
    accused of falsifying vaccination figures") at 0.13, against 0.00 for
    unrelated text. There is no threshold that captures that pair without also
    capturing noise. So a narrative restated in genuinely different vocabulary
    WILL split into two here, and no amount of tuning fixes it — lexical overlap
    is the wrong measurement for semantic identity.

    This is precisely what sentence embeddings solve, and swapping them in
    changes nothing else in this file: pass embedding vectors instead of TF-IDF
    ones and the clustering, tracking and event logic are unchanged. Until then,
    read a split narrative as possible over-fragmentation rather than as two
    separate stories.

    Deterministic: points are processed in input order and centroids recomputed
    each pass, so the same corpus gives the same clusters. That matters more
    here than cluster quality — a narrative whose membership flickers between
    identical runs cannot be tracked across time, and unstable identity is worse
    than coarse identity.
    """
    if not vectors:
        return []
    if lam is not None:          # back-compat for the old distance-style argument
        join_sim = max(0.05, min(0.95, 1.0 - lam))
    thresh = max(0.05, min(0.95, join_sim))

    centroids: list[dict] = []
    assign = [-1] * len(vectors)
    for _ in range(max(1, iters)):
        changed = False
        for i, v in enumerate(vectors):
            best, best_sim = -1, -1.0
            for c, cen in enumerate(centroids):
                s = cosine(v, cen)
                if s > best_sim:
                    best, best_sim = c, s
            if best_sim < thresh:
                centroids.append(dict(v))
                best = len(centroids) - 1
            if assign[i] != best:
                assign[i] = best
                changed = True
        groups: dict = defaultdict(list)
        for i, c in enumerate(assign):
            groups[c].append(i)
        # Recompute and drop emptied centroids, renumbering assignments.
        new_cent, remap = [], {}
        for c in sorted(groups):
            remap[c] = len(new_cent)
            new_cent.append(_centroid([vectors[i] for i in groups[c]]))
        centroids = new_cent
        assign = [remap[c] for c in assign]
        if not changed:
            break
    groups = defaultdict(list)
    for i, c in enumerate(assign):
        groups[c].append(i)
    return [groups[c] for c in sorted(groups, key=lambda c: -len(groups[c]))]


# ── Optimal assignment (Hungarian) ───────────────────────────────────────────

def hungarian(cost: list[list[float]]) -> list[tuple[int, int]]:
    """Minimum-cost perfect assignment on a rectangular matrix (JV variant).

    Greedy matching is the tempting shortcut and it is wrong in a specific,
    damaging way: a narrative that is a decent match for two current clusters
    gets claimed by whichever is examined first, and the better pairing further
    down the matrix is then impossible. Identity would depend on iteration
    order. Optimal assignment costs nothing at these sizes (tens of clusters).
    """
    if not cost or not cost[0]:
        return []
    n, m = len(cost), len(cost[0])
    transposed = False
    if n > m:
        cost = [[cost[i][j] for i in range(n)] for j in range(m)]
        n, m, transposed = m, n, True
    INF = float("inf")
    u = [0.0] * (n + 1); v = [0.0] * (m + 1)
    p = [0] * (m + 1); way = [0] * (m + 1)
    for i in range(1, n + 1):
        p[0] = i; j0 = 0
        minv = [INF] * (m + 1); used = [False] * (m + 1)
        while True:
            used[j0] = True
            i0 = p[j0]; delta = INF; j1 = 0
            for j in range(1, m + 1):
                if used[j]:
                    continue
                cur = cost[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur; way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]; j1 = j
            for j in range(m + 1):
                if used[j]:
                    u[p[j]] += delta; v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while j0:
            j1 = way[j0]; p[j0] = p[j1]; j0 = j1
    pairs = [(p[j] - 1, j - 1) for j in range(1, m + 1) if p[j] > 0]
    return [(b, a) for a, b in pairs] if transposed else pairs


# ── Narrative tracking ───────────────────────────────────────────────────────

@dataclass
class Narrative:
    id: str
    label: str
    members: set              # document urls at the FRONT (latest observation)
    first_seen: str
    last_seen: str
    observations: int = 1
    misses: int = 0
    peak_size: int = 0
    history: list = field(default_factory=list)   # [{ts, size, corpus_size}]

    def to_dict(self) -> dict:
        h = self.history[-HISTORY_CAP:]
        return {"id": self.id, "label": self.label, "members": sorted(self.members),
                "first_seen": self.first_seen, "last_seen": self.last_seen,
                "observations": self.observations, "misses": self.misses,
                "peak_size": self.peak_size, "history": h,
                # A narrative that missed an observation keeps the members it had
                # last time. Its "size" is therefore last run's, and a consumer
                # plotting it as current would be plotting a stale number with
                # nothing marking it as stale.
                "stale": self.misses > 0,
                # observations counts forever; history is capped. Without this a
                # reader computes a floor and a peak over the last 60 points and
                # presents them as the narrative's lifetime range.
                "history_capped": len(self.history) > len(h),
                "history_covers": len(h)}

    @staticmethod
    def from_dict(d: dict) -> "Narrative":
        return Narrative(id=d["id"], label=d.get("label", ""), members=set(d.get("members") or []),
                         first_seen=d.get("first_seen", ""), last_seen=d.get("last_seen", ""),
                         observations=int(d.get("observations") or 1),
                         misses=int(d.get("misses") or 0),
                         peak_size=int(d.get("peak_size") or 0),
                         history=list(d.get("history") or []))


HISTORY_CAP = 60            # points kept per narrative; flagged when it bites
MATCH_THRESHOLD = 0.30      # Greene et al. front-matching theta
DEATH_AFTER = 3             # consecutive misses before a narrative is declared dead
DRIFT_SIM = 0.15            # below this label similarity, a match is flagged as drift
GROWTH_BAND = 0.10          # +/- 10% is "stable", outside it is growth or contraction


def _jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def track(previous: list[dict] | None, clusters: list[dict], timestamp: str,
          threshold: float = MATCH_THRESHOLD, corpus_size: int | None = None) -> dict:
    """Match this observation's clusters to living narratives and emit events.

    `clusters` is [{"label": str, "members": [url, ...]}] from this run.
    `previous` is the serialised state from the last run (or None to start).
    `corpus_size` is how many documents this observation was drawn from. Pass it.
    Without it, "this narrative grew 40%" cannot be told apart from "we collected
    40% more documents this time" — the growth event is then a statement about
    our own collection, presented as a statement about the world.

    Returns {"narratives": [...], "events": [...]}, where every narrative keeps
    the SAME id it had before. That id is the whole point: it is what lets a
    chart plot one line, an alert say "this narrative doubled", and a case file
    refer to a story rather than to a row number.
    """
    live = [Narrative.from_dict(d) for d in (previous or [])]
    cur_members = [set(c.get("members") or []) for c in clusters]
    cur_labels = [str(c.get("label") or "")[:120] for c in clusters]

    events: list[dict] = []
    matched_live: dict[int, int] = {}      # live index -> cluster index

    if live and cur_members:
        # Cost = 1 - similarity, so minimum-cost assignment maximises similarity.
        sim = [[_jaccard(l.members, c) for c in cur_members] for l in live]
        cost = [[1.0 - s for s in row] for row in sim]
        for li, ci in hungarian(cost):
            if 0 <= li < len(live) and 0 <= ci < len(cur_members) and sim[li][ci] >= threshold:
                matched_live[li] = ci

        # Greene's merge and split are read off the similarity matrix, not off
        # the assignment: assignment is one-to-one by construction, so a split
        # is only visible as a SECOND current cluster that also resembles this
        # narrative, and a merge as a second narrative resembling this cluster.
        for li, ci in matched_live.items():
            others = [j for j in range(len(cur_members))
                      if j != ci and sim[li][j] >= threshold]
            if others:
                # `into` used to be an integer here and a label string on
                # merge — two meanings on one field name. Named explicitly.
                events.append({"kind": "split", "narrative": live[li].id,
                               "label": live[li].label,
                               "into_count": len(others) + 1, "at": timestamp,
                               "detail": f"'{live[li].label}' now matches "
                                         f"{len(others)+1} distinct clusters"})
        for ci in set(matched_live.values()):
            srcs = [i for i in range(len(live)) if sim[i][ci] >= threshold]
            if len(srcs) > 1:
                # A merge has no single subject, so `narrative` is null by
                # design rather than absent — a consumer indexing by it must be
                # able to tell "not applicable" from "field missing".
                events.append({"kind": "merge", "narrative": None,
                               "label": cur_labels[ci],
                               "into_label": cur_labels[ci],
                               "from_ids": [live[i].id for i in srcs],
                               "from_labels": [live[i].label for i in srcs],
                               "at": timestamp,
                               "detail": f"{len(srcs)} narratives converged on "
                                         f"'{cur_labels[ci]}': "
                                         + ", ".join(f"'{live[i].label}'" for i in srcs)})

    out: list[Narrative] = []
    used_clusters = set(matched_live.values())

    for li, n in enumerate(live):
        if li in matched_live:
            ci = matched_live[li]
            before = len(n.members)
            prev_label = n.label
            prev_corpus = (n.history[-1].get("corpus_size") if n.history else None)
            n.members = cur_members[ci]
            n.label = cur_labels[ci] or n.label
            n.last_seen = timestamp
            n.observations += 1
            n.misses = 0
            n.peak_size = max(n.peak_size, len(n.members))
            n.history.append({"ts": timestamp, "size": len(n.members),
                              "corpus_size": corpus_size})
            # SEMANTIC DRIFT. Greene defines identity by MEMBERSHIP, so a
            # narrative that keeps its documents keeps its id even if the
            # wording moves — which is usually right, because stories do evolve.
            # But it means identity can walk onto a different subject one
            # observation at a time, and an analyst reading "this narrative has
            # grown steadily for three weeks" deserves to know if what it is
            # about changed underneath. Not blocked, because blocking would
            # break legitimate evolution; reported, so it is visible.
            if prev_label and n.label and prev_label != n.label:
                lv = tfidf([prev_label, n.label])
                if cosine(lv[0], lv[1]) < DRIFT_SIM:
                    events.append({"kind": "drift", "narrative": n.id,
                                   "label": n.label, "at": timestamp,
                                   "from_label": prev_label, "to_label": n.label,
                                   "detail": (f"kept its documents but its subject changed: "
                                              f"'{prev_label[:60]}' -> '{n.label[:60]}'")})
            if before:
                now_n = len(n.members)
                delta = (now_n - before) / before

                # SHARE, not just count. A narrative that grew 40% in a corpus
                # that grew 40% did not grow — we collected more. Reporting the
                # raw count as growth turns a fact about our own collection into
                # a claim about the world, which is the same defect class as an
                # uncalibrated coordination score.
                share_delta = None
                corpus_delta = None
                if corpus_size and prev_corpus and prev_corpus > 0 and corpus_size > 0:
                    corpus_delta = (corpus_size - prev_corpus) / prev_corpus
                    share_before = before / prev_corpus
                    share_now = now_n / corpus_size
                    if share_before > 0:
                        share_delta = (share_now - share_before) / share_before

                # When the corpus explains the move, the honest verdict is
                # "held its share", not growth.
                explained = (share_delta is not None
                             and abs(share_delta) <= GROWTH_BAND
                             and abs(delta) > GROWTH_BAND)

                def _evt(kind, verb):
                    d = {"kind": kind, "narrative": n.id, "label": n.label,
                         "at": timestamp,
                         "from_size": before, "to_size": now_n,
                         "delta": round(delta, 4),
                         "share_delta": (round(share_delta, 4)
                                         if share_delta is not None else None),
                         "corpus_delta": (round(corpus_delta, 4)
                                          if corpus_delta is not None else None),
                         "normalised": share_delta is not None}
                    if share_delta is None:
                        d["detail"] = (f"'{n.label}' {verb} {delta:+.0%} "
                                       f"({before} -> {now_n} documents). No corpus size "
                                       f"recorded, so this is a raw count change and may "
                                       f"reflect how much was collected.")
                    else:
                        d["detail"] = (f"'{n.label}' {verb} {delta:+.0%} "
                                       f"({before} -> {now_n} documents); its share of the "
                                       f"corpus moved {share_delta:+.0%} while the corpus "
                                       f"itself moved {corpus_delta:+.0%}.")
                    events.append(d)

                if explained:
                    events.append({
                        "kind": "held_share", "narrative": n.id, "label": n.label,
                        "at": timestamp, "from_size": before, "to_size": now_n,
                        "delta": round(delta, 4), "share_delta": round(share_delta, 4),
                        "corpus_delta": round(corpus_delta, 4), "normalised": True,
                        "detail": (f"'{n.label}' moved {delta:+.0%} in raw count, but the "
                                   f"corpus moved {corpus_delta:+.0%} — its share is "
                                   f"effectively unchanged. This is collection volume, "
                                   f"not the narrative.")})
                elif delta > GROWTH_BAND:
                    _evt("growth", "grew")
                elif delta < -GROWTH_BAND:
                    _evt("contraction", "shrank")
            out.append(n)
        else:
            n.misses += 1
            if n.misses >= DEATH_AFTER:
                # The narrative is dropped from the returned list on this same
                # pass, so a consumer can never look its label up. Carry it on
                # the event or the death is unreadable — an id and a date.
                events.append({"kind": "death", "narrative": n.id, "label": n.label,
                               "at": timestamp, "misses": n.misses,
                               "first_seen": n.first_seen, "last_seen": n.last_seen,
                               "peak_size": n.peak_size,
                               "detail": f"'{n.label}' absent from {n.misses} consecutive "
                                         f"observations. Peak size {n.peak_size}; first seen "
                                         f"{n.first_seen}."})
                # Dropped from the returned state — but the death event is the
                # record that it existed and stopped, which is itself a finding.
                continue
            # A narrative that misses once is not dead. Real collection is lossy:
            # a source times out, a rate limit hits, an API returns nothing. Killing
            # identity on the first miss would manufacture a "new" narrative on the
            # next run and destroy exactly the continuity this module exists for.
            out.append(n)

    for ci, members in enumerate(cur_members):
        if ci in used_clusters:
            continue
        out.append(Narrative(
            id=f"n_{uuid.uuid4().hex[:12]}", label=cur_labels[ci], members=members,
            first_seen=timestamp, last_seen=timestamp, peak_size=len(members),
            history=[{"ts": timestamp, "size": len(members),
                      "corpus_size": corpus_size}]))
        events.append({"kind": "birth", "narrative": out[-1].id,
                       "label": cur_labels[ci], "at": timestamp,
                       "to_size": len(members),
                       "detail": f"'{cur_labels[ci]}' first observed "
                                 f"({len(members)} documents)"})

    out.sort(key=lambda n: (-len(n.members), n.first_seen))
    # EVERY event carries: kind, at, narrative (id or null), label, detail.
    # Kind-specific extras are named for what they hold — into_count, from_ids,
    # from_label/to_label — never reusing one name for two meanings.
    return {"narratives": [n.to_dict() for n in out], "events": events,
            "corpus_size": corpus_size,
            "normalised": corpus_size is not None}


#: Semantic backends and lexical ones do not live on the same scale. TF-IDF
#: puts a genuine paraphrase at 0.14; an embedding model puts the same pair
#: around 0.8. One threshold cannot serve both, so each carries its own.
SEMANTIC_JOIN_SIM = float(__import__("os").environ.get("SEMANTIC_JOIN_SIM", "0.72"))


def cluster_claims(claims: list[dict], join_sim: float = JOIN_SIM) -> list[dict]:
    """Group claim strings into narratives.

    `claims` is [{"text": str, "url": str}]. Returns
    [{"label", "members", "size", "cohesion"}] ready to hand to `track()`.
    The label is the claim closest to the centroid — the most representative
    thing actually said, rather than a summary nobody wrote.
    """
    claims = [c for c in claims if isinstance(c, dict) and (c.get("text") or "").strip()]
    if not claims:
        return []
    texts = [c["text"] for c in claims]

    # Semantic first, lexical as the fallback — and the method is RECORDED on
    # every cluster, because "these are two separate narratives" means something
    # different depending on whether the thing that said so understands
    # paraphrase. A silent downgrade would let an analyst read lexical
    # fragmentation as two genuinely distinct stories.
    dense, method = emb.embed(texts)
    if dense:
        vecs = [dict(enumerate(v)) for v in dense]
        thresh = SEMANTIC_JOIN_SIM
    else:
        vecs = tfidf(texts)
        thresh = join_sim
        method = "lexical" if method in ("unavailable", "empty") else f"lexical ({method})"

    out = []
    for idxs in dp_means(vecs, join_sim=thresh):
        cen = _centroid([vecs[i] for i in idxs])
        best = max(idxs, key=lambda i: cosine(vecs[i], cen))
        sims = [cosine(vecs[i], cen) for i in idxs]
        out.append({
            "method": method,
            "label": claims[best]["text"][:120],
            "members": sorted({claims[i].get("url") for i in idxs if claims[i].get("url")}),
            "size": len(idxs),
            "cohesion": round(sum(sims) / len(sims), 3) if sims else 0.0,
        })
    return out
