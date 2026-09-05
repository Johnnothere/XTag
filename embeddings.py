"""
Semantic embeddings — the missing measurement under claim clustering.

WHY THIS EXISTS
---------------
`narratives.py` clusters claims on TF-IDF cosine, and that was measured against
its own test corpus:

    restatement sharing vocabulary      0.58 - 1.00
    genuine paraphrase, new vocabulary  0.11 - 0.14   <-- the problem
    unrelated documents                 0.00

"The ministry falsified vaccine data" and "ministry accused of falsifying
vaccination figures" score 0.14, against 0.00 for text about something else.
There is no threshold that separates those two numbers, so a narrative restated
in different words splits into two — and no amount of tuning fixes it, because
lexical overlap is simply the wrong measurement for semantic identity.

WHY HOSTED, NOT LOCAL
---------------------
The obvious answer is sentence-transformers. It was rejected deliberately:

  * XTag's corpus is Persian, Arabic, Hebrew and English. A monolingual model is
    useless here, and a multilingual one that is any good is large.
  * The deployment image already carries Playwright and Chromium. Adding torch
    puts it into multi-gigabyte territory, on a platform where build time and
    memory are both constrained.
  * A model that loads per worker competes for RAM with the request it is
    serving.

A hosted embedding call costs one HTTP request, no image weight, and is
multilingual by default. If the deployment ever gets somewhere sensible to run a
model, `_embed_local` below is the seam — nothing else changes.

DEGRADATION IS THE POINT
------------------------
With no key configured this module returns None and the caller falls back to
TF-IDF, exactly as before. What it must never do is fall back SILENTLY: the
method actually used is reported alongside the result, because "these two
narratives are separate" means something different depending on whether a
machine that understands paraphrase said it.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import threading
import time

import requests

# ── Configuration ────────────────────────────────────────────────────────────
VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY", "").strip()
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()

# voyage-3-lite: multilingual, 512 dimensions, and roughly $0.02 per million
# tokens — a search's worth of claims is a few thousand tokens, so the marginal
# cost of a search is a rounding error against the model calls already made.
VOYAGE_MODEL = os.environ.get("VOYAGE_MODEL", "voyage-3-lite")
OPENAI_MODEL = os.environ.get("OPENAI_EMBED_MODEL", "text-embedding-3-small")
LOCAL_MODEL  = os.environ.get("LOCAL_EMBED_MODEL",
                              "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2")

EMBED_TIMEOUT   = float(os.environ.get("EMBED_TIMEOUT", "10"))
EMBED_BATCH     = int(os.environ.get("EMBED_BATCH", "96"))
EMBED_MAX_CHARS = int(os.environ.get("EMBED_MAX_CHARS", "800"))
EMBED_CACHE_MAX = int(os.environ.get("EMBED_CACHE_MAX", "4000"))

_cache: dict = {}
_cache_lock = threading.Lock()
_last_error: dict = {"at": None, "backend": None, "message": None}
_err_lock = threading.Lock()


def _key(backend: str, model: str, text: str) -> str:
    return hashlib.sha256(f"{backend}|{model}|{text}".encode("utf-8")).hexdigest()[:32]


def _cache_get(k: str):
    with _cache_lock:
        return _cache.get(k)


def _cache_put(k: str, v) -> None:
    with _cache_lock:
        _cache[k] = v
        # Insertion-ordered dict: the oldest keys are first.
        while len(_cache) > EMBED_CACHE_MAX:
            _cache.pop(next(iter(_cache)), None)


def _note_error(backend: str, msg: str) -> None:
    with _err_lock:
        _last_error.update({"at": time.time(), "backend": backend,
                            "message": str(msg)[:200]})


def last_error() -> dict:
    with _err_lock:
        return dict(_last_error)


# ── Backends ─────────────────────────────────────────────────────────────────

def _embed_voyage(texts: list[str]) -> list[list[float]] | None:
    r = requests.post(
        "https://api.voyageai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {VOYAGE_API_KEY}",
                 "Content-Type": "application/json"},
        json={"input": texts, "model": VOYAGE_MODEL, "input_type": "document"},
        timeout=EMBED_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"voyage HTTP {r.status_code}: {r.text[:160]}")
    data = r.json().get("data") or []
    # The API returns items with an `index`; do not assume input order.
    out: list = [None] * len(texts)
    for item in data:
        i = int(item.get("index", -1))
        if 0 <= i < len(out):
            out[i] = item.get("embedding")
    if any(v is None for v in out):
        raise RuntimeError("voyage returned an incomplete batch")
    return out


def _embed_openai(texts: list[str]) -> list[list[float]] | None:
    r = requests.post(
        "https://api.openai.com/v1/embeddings",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={"input": texts, "model": OPENAI_MODEL},
        timeout=EMBED_TIMEOUT)
    if r.status_code != 200:
        raise RuntimeError(f"openai HTTP {r.status_code}: {r.text[:160]}")
    data = sorted((r.json().get("data") or []), key=lambda d: d.get("index", 0))
    if len(data) != len(texts):
        raise RuntimeError("openai returned an incomplete batch")
    return [d.get("embedding") for d in data]


_local_model = None
_local_lock = threading.Lock()


def _embed_local(texts: list[str]) -> list[list[float]] | None:
    """The seam for a locally-hosted model. Not installed by default — see the
    module docstring for why. If sentence-transformers is ever present, this
    lights up with no other change."""
    global _local_model
    with _local_lock:
        if _local_model is None:
            from sentence_transformers import SentenceTransformer   # noqa
            _local_model = SentenceTransformer(LOCAL_MODEL)
    return [list(map(float, v)) for v in
            _local_model.encode(texts, normalize_embeddings=False)]


def _available() -> list[tuple[str, object]]:
    """Backends in preference order, best first."""
    out: list = []
    if VOYAGE_API_KEY:
        out.append(("voyage", _embed_voyage))
    if OPENAI_API_KEY:
        out.append(("openai", _embed_openai))
    try:
        import sentence_transformers  # noqa: F401
        out.append(("local", _embed_local))
    except Exception:
        pass
    return out


def backend() -> str | None:
    """Which backend would be used, without calling it."""
    a = _available()
    return a[0][0] if a else None


def available() -> bool:
    return bool(_available())


# ── Public API ───────────────────────────────────────────────────────────────

def embed(texts: list[str]) -> tuple[list[list[float]] | None, str]:
    """Embed texts. Returns (vectors, backend_name).

    `(None, "unavailable")` when no backend is configured — that is a normal
    state, not an error, and the caller falls back to lexical similarity.

    Never raises. A failure mid-way returns None rather than a partially
    embedded set: half-semantic, half-lexical clustering would be worse than
    either, and impossible to describe honestly to the analyst.
    """
    texts = [(t or "")[:EMBED_MAX_CHARS] for t in texts]
    if not texts:
        return [], "empty"
    backends = _available()
    if not backends:
        return None, "unavailable"
    name, fn = backends[0]
    model = {"voyage": VOYAGE_MODEL, "openai": OPENAI_MODEL, "local": LOCAL_MODEL}[name]

    out: list = [None] * len(texts)
    pending: list[tuple[int, str]] = []
    for i, t in enumerate(texts):
        hit = _cache_get(_key(name, model, t))
        if hit is not None:
            out[i] = hit
        else:
            pending.append((i, t))

    try:
        for start in range(0, len(pending), EMBED_BATCH):
            chunk = pending[start:start + EMBED_BATCH]
            vecs = fn([t for _, t in chunk])
            if not vecs or len(vecs) != len(chunk):
                raise RuntimeError("backend returned the wrong number of vectors")
            for (i, t), v in zip(chunk, vecs):
                out[i] = v
                _cache_put(_key(name, model, t), v)
    except Exception as e:
        _note_error(name, e)
        return None, f"{name}:failed"

    if any(v is None for v in out):
        _note_error(name, "incomplete result")
        return None, f"{name}:incomplete"
    return out, name


def cosine(a, b) -> float:
    """Cosine similarity between two dense vectors."""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return max(-1.0, min(1.0, dot / (math.sqrt(na) * math.sqrt(nb))))


def status() -> dict:
    """For /api/status — say which method the analysis is actually using."""
    b = backend()
    with _cache_lock:
        cached = len(_cache)
    return {
        "backend": b or "none",
        "semantic": bool(b),
        "model": ({"voyage": VOYAGE_MODEL, "openai": OPENAI_MODEL,
                   "local": LOCAL_MODEL}.get(b) if b else None),
        "cached_vectors": cached,
        "last_error": last_error() if last_error().get("message") else None,
        "note": ("Claims are clustered on meaning." if b else
                 "No embedding backend configured — claims are clustered on shared "
                 "wording, which does not recognise paraphrase. Set VOYAGE_API_KEY "
                 "to enable semantic clustering."),
    }


# ── Anchor sentiment ─────────────────────────────────────────────────────────
# WHAT THIS IS, AND WHAT IT IS NOT
#
# XTag scores sentiment with Claude and Babel Street. Both are better than what
# follows, and neither is replaced by it. This exists for one specific gap:
# documents past the per-language batch cap are currently returned UNSCORED, and
# on a large corpus that is most of them.
#
# The method is nearest-anchor classification. A handful of short phrases with
# known polarity are embedded once; a document takes the polarity of whichever
# anchor set it sits closest to. It works multilingually because the embedding
# model does — anchors are supplied in the languages XTag actually collects, and
# the model places a Persian sentence near its English equivalent without being
# told they are related.
#
# It is a WEAK signal and is reported as its own engine so it can never be
# mistaken for the other two. It never overwrites a document the stronger
# engines scored, and it abstains rather than guessing when the nearest positive
# and negative anchors are too close together to separate.
#
# Cost: zero extra network calls when the document vectors were already embedded
# for claim clustering — the same vectors serve both.

_POS = [
    "this is good news and a welcome development",
    "praise, support and celebration for what happened",
    "relief that the situation has improved",
    "خبر خوب و مثبت است",                       # fa
    "هذا خبر جيد ومشجع",                        # ar
    "אלו חדשות טובות",                          # he
]
_NEG = [
    "this is a disaster and an outrage",
    "condemnation, anger and accusation of wrongdoing",
    "fear that the situation is getting worse",
    "این فاجعه است و باعث خشم شده",              # fa
    "هذه كارثة وتثير الغضب",                     # ar
    "זו קטסטרופה ומעוררת זעם",                   # he
]
_NEU = [
    "a factual report of what was announced",
    "procedural update with no evaluative language",
    "گزارشی واقعی از آنچه اعلام شد",             # fa
    "تقرير وقائعي لما تم الإعلان عنه",           # ar
    "דיווח עובדתי על מה שהוכרז",                 # he
]

#: How much closer the winning polarity must be than the runner-up before a
#: label is assigned. Below it the document is left UNSCORED — an abstention is
#: a truthful output and a coin-flip label is not.
ANCHOR_MARGIN = float(os.environ.get("ANCHOR_MARGIN", "0.02"))

_anchors: dict | None = None
_anchor_lock = threading.Lock()


def _anchor_vectors():
    """Embed the anchor sets once per process."""
    global _anchors
    with _anchor_lock:
        if _anchors is not None:
            return _anchors
    allp = _POS + _NEG + _NEU
    vecs, backend_name = embed(allp)
    if not vecs:
        return None
    a = {"positive": vecs[:len(_POS)],
         "negative": vecs[len(_POS):len(_POS) + len(_NEG)],
         "neutral":  vecs[len(_POS) + len(_NEG):],
         "backend": backend_name}
    with _anchor_lock:
        _anchors = a
    return a


def _best(vec, anchors: list) -> float:
    return max((cosine(vec, a) for a in anchors), default=-1.0)


def score_sentiment(texts: list[str], vectors: list | None = None) -> dict:
    """Label texts by nearest anchor. Returns {index: label} plus diagnostics.

    `vectors` lets a caller reuse embeddings it already computed — pass them and
    this costs no network at all.

    Returns only the documents it is willing to label. An index absent from
    `labels` was ABSTAINED ON, which is different from being labelled neutral,
    and callers must keep that difference.
    """
    if not texts:
        return {"labels": {}, "engine": "anchor", "available": False,
                "reason": "no texts"}
    anchors = _anchor_vectors()
    if not anchors:
        return {"labels": {}, "engine": "anchor", "available": False,
                "reason": "no embedding backend configured"}

    if vectors is None or len(vectors) != len(texts):
        vectors, _ = embed(texts)
    if not vectors:
        return {"labels": {}, "engine": "anchor", "available": False,
                "reason": "embedding call failed"}

    labels: dict = {}
    abstained = 0
    for i, v in enumerate(vectors):
        sims = {k: _best(v, anchors[k]) for k in ("positive", "negative", "neutral")}
        ranked = sorted(sims.items(), key=lambda kv: -kv[1])
        top, second = ranked[0], ranked[1]
        if (top[1] - second[1]) < ANCHOR_MARGIN:
            abstained += 1
            continue
        labels[i] = top[0]
    return {
        "labels": labels,
        "engine": "anchor",
        "available": True,
        "backend": anchors.get("backend"),
        "scored": len(labels),
        "abstained": abstained,
        "margin": ANCHOR_MARGIN,
        "caveat": ("Nearest-anchor classification on multilingual embeddings. "
                   "Weaker than the Claude and Babel Street engines and never "
                   "overrides them — it exists to cover documents those engines "
                   "did not reach. Abstains when the polarities are too close "
                   "to separate."),
    }
