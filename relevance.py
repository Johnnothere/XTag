"""
Relevance gate — decides whether a collected document is actually about the query.

WHY THIS MODULE EXISTS
Every upstream source is a third-party search engine with its own fuzzy ranker,
and none of them are obliged to respect the query. Measured against the live
deployment on 2026-09-01, the query "#covid1948" returned 530 documents of which
389 (73.4%) contained neither "covid1948" nor even "covid" or "1948" anywhere:
YouTube alone contributed 450 generic COVID-19 vaccine videos because its ranker
degrades a token it cannot match into the nearest thing it can.

That noise is not cosmetic. It propagated into every number the platform
produced. Sentiment was computed over it, narratives were extracted from it, and
`intel.score_threat` diluted every ratio-based factor with it — while
`intel.py`'s confidence formula, which reads raw document count, reported 94.5
("high") precisely BECAUSE there were 530 documents. Unfiltered noise does not
merely add error; it certifies it.

THE RULE THAT MATTERS: FUSED TOKENS DO NOT DECOMPOSE
"#covid1948" is one identifier, not "covid" AND "1948". A hashtag is atomic —
it is a name, and half a name is a different name. Everything else in this
module follows from that. `covid19` must never match `covid1948`, and
`covid1948` must never be satisfied by an article about 1948.

MATCHING IS DONE BY EXPANDING THE QUERY, NOT BY SQUASHING THE DOCUMENT
The tempting implementation — strip all separators from both sides and do a
substring test — is wrong, and wrong in the silent direction: with separators
gone, "covid19" is a prefix of "covid1948" and matches it. Instead the query is
expanded into its plausible written surface forms and each is matched against
the normalised document under word boundaries. `\bcovid19\b` cannot match inside
"covid1948", which is exactly the discrimination the gate exists to make.

DROPPING IS NOT DELETING
Callers are expected to retain what was dropped and why. A relevance rule that
silently erases documents is a worse failure than the noise it replaces, because
nobody can see it happen. `score_doc` always returns a human-readable basis.
"""

from __future__ import annotations

import re
import unicodedata

__all__ = ["QuerySpec", "plan_query", "score_doc", "fold", "normalise"]


# ── Normalisation ─────────────────────────────────────────────────────────────

# Arabic-Indic (U+0660-0669) and Extended/Persian (U+06F0-06F9) digits. Persian
# and Arabic sources write years in native digits, so "١٩٤٨" and "1948" are the
# same token and must fold together or every Arabic-script match is missed.
_DIGIT_MAP = {**{0x0660 + i: str(i) for i in range(10)},
              **{0x06F0 + i: str(i) for i in range(10)}}

# Zero-width joiners and marks. Persian text is full of ZWNJ (U+200C); leaving
# it in splits tokens that a reader sees as one word.
_ZERO_WIDTH = dict.fromkeys([0x200B, 0x200C, 0x200D, 0x2060, 0xFEFF], None)

_NON_ALNUM = re.compile(r"[^0-9a-z؀-ۿ֐-׿Ѐ-ӿ一-鿿]+")
_RUNS = re.compile(r"[a-z]+|[0-9]+|[؀-ۿ]+|[֐-׿]+|[Ѐ-ӿ]+|[一-鿿]+")


def fold(s) -> str:
    """
    Case-, accent- and digit-fold a string into a comparable form.

    NFKD decomposition plus combining-mark removal is what makes the Turkish
    dotted capital İ (U+0130) fold onto plain "i" — that form appears verbatim
    in the live corpus as "#COVİD1948" and would otherwise be missed by an
    ASCII-only matcher. Non-Latin scripts pass through unchanged; they carry no
    case and their letters are not decomposable in a way that loses meaning.
    """
    s = str(s or "").translate(_ZERO_WIDTH).translate(_DIGIT_MAP)
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    return s.casefold()


def normalise(s) -> str:
    """Folded text with every separator collapsed to a single space.

    Word boundaries survive, so `\\b` anchors in the matcher mean what they say.
    """
    return " " + _NON_ALNUM.sub(" ", fold(s)).strip() + " "


# lower→Upper transition, the camelCase word boundary. Hashtags are written this
# way by convention ("#QudsDay2020", "#FreePalestine"), and the capital is the
# only thing marking where one word ends — folding first destroys it.
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")


def _runs(s) -> list[str]:
    """Alphabetic / numeric / per-script runs, camelCase boundaries preserved.

    This is where "covid1948" becomes ["covid", "1948"] — the split the SOURCES
    perform implicitly and destructively. Doing it here, explicitly, is what
    lets the module generate "covid 1948" as a legitimate surface form while
    still refusing to accept "covid" on its own.
    """
    return _RUNS.findall(fold(_CAMEL.sub(" ", str(s or ""))))


def _join_variants(runs: list[str], limit: int = 5) -> set[str]:
    """
    Every way the same identifier could plausibly have been written.

    "#QudsDay2020" is written in the wild as "#QudsDay2020", "QudsDay 2020",
    "Quds Day 2020" and "#quds_day_2020". They normalise to different strings,
    so matching only the fully-spaced and fully-joined forms misses the middle
    ones — which are the common ones. Enumerate the 2^(n-1) adjacent join/space
    choices instead. Bounded at `limit` runs (16 forms); beyond that the two
    extremes are enough, because a 6-word tag is not written half-joined.
    """
    if not runs: return set()
    if len(runs) == 1: return {runs[0]}
    if len(runs) > limit: return {" ".join(runs), "".join(runs)}
    forms = set()
    for mask in range(1 << (len(runs) - 1)):
        buf = runs[0]
        for i in range(1, len(runs)):
            buf += ("" if mask >> (i - 1) & 1 else " ") + runs[i]
        forms.add(buf)
    return forms


# ── Query planning ────────────────────────────────────────────────────────────

class QuerySpec:
    """A query, decomposed into everything needed to judge a document against it."""

    __slots__ = ("raw", "kind", "canonical", "fused", "surface_forms",
                 "tokens", "expansions", "_patterns", "_expansion_patterns")

    def __init__(self, raw, kind, canonical, fused, surface_forms, tokens, expansions):
        self.raw = raw
        self.kind = kind                      # 'hashtag' | 'phrase' | 'term'
        self.canonical = canonical
        self.fused = fused                    # True → token-splitting is forbidden
        self.surface_forms = surface_forms    # forms that count as an exact hit
        self.tokens = tokens
        self.expansions = expansions          # {lang: [terms]} cross-lingual
        self._patterns = [_boundary_re(f) for f in surface_forms]
        flat = []
        for lang, terms in (expansions or {}).items():
            for t in terms:
                n = " ".join(_runs(t))
                if n and n != canonical:
                    flat.append((lang, n, _boundary_re(n)))
        self._expansion_patterns = flat

    def __repr__(self):
        return (f"QuerySpec({self.raw!r} kind={self.kind} canonical={self.canonical!r} "
                f"fused={self.fused} forms={sorted(self.surface_forms)})")


def _boundary_re(form: str):
    """
    Word-boundary-anchored matcher for one surface form.

    `\\b` is unreliable at the edge of non-Latin scripts (Python's word-character
    class covers them, but the surrounding punctuation may not create a
    boundary), so the anchors are written as explicit "not a word character"
    lookarounds against the normalised text, which is always space-delimited.
    """
    return re.compile(r"(?<![^\W_])" + re.escape(form) + r"(?![^\W_])", re.UNICODE)


def plan_query(q: str, expansions: dict | None = None) -> QuerySpec:
    """
    Turn a raw user query into a QuerySpec.

    `expansions` is the {lang: [terms]} map from app.expand_query. It is passed
    in rather than imported so this module stays free of app-level dependencies
    and can be unit-tested on its own.
    """
    raw = (q or "").strip()
    plain = raw.lstrip("#").strip()
    is_tag = raw.startswith("#")

    runs = _runs(plain)
    canonical = " ".join(runs)

    # A hashtag is an atomic identifier. So is any single unspaced word that
    # contains an alpha/digit transition — "covid1948", "gaza2024", "Q17" — the
    # user wrote it as one token because it IS one token.
    fused = bool(is_tag or (runs and " " not in plain.strip() and len(runs) > 1))

    # Every adjacent join/space arrangement: 'covid 1948' matches "COVID-1948",
    # 'covid1948' matches "#COVİD1948", and for "#QudsDay2020" this also yields
    # 'quds day2020' and 'qudsday 2020', which the two extremes alone would miss.
    forms = _join_variants(runs)

    kind = "hashtag" if is_tag else ("phrase" if len(runs) > 1 and not fused else "term")

    # Scattered all-token matching is only ever offered for genuine multi-word
    # queries. For a fused token the token list is the whole thing, so the
    # 'all terms present' tier degenerates into the exact tier and can never
    # loosen the match — which is the entire point.
    tokens = [] if fused else [r for r in runs if len(r) > 2]

    return QuerySpec(raw, kind, canonical, fused, forms, tokens, expansions or {})


# ── Document scoring ──────────────────────────────────────────────────────────

# Weakest evidence first so the strongest available basis is the one reported.
EXACT       = 1.00
EXPANSION   = 0.65
ALL_TOKENS  = 0.45
UNVERIFIED  = 0.40
NO_MATCH    = 0.00

DEFAULT_FLOOR = 0.35

# Sources whose result we hold only as a SNIPPET of a longer document, and which
# matched the query against the full text we never fetched. For these, absence of
# the term in the stored snippet is not evidence of absence in the document, so a
# non-matching result is kept at UNVERIFIED rather than dropped — clearly labelled,
# and capped by MAX_UNVERIFIED_PER_SOURCE so it can never dominate a corpus.
#
# YouTube and Hacker News are deliberately NOT on this list. Both were measured
# doing the opposite thing: given "#covid1948" their rankers silently substituted
# the nearest token they could match and returned 450 and 20 generic COVID-19
# results respectively. A source that degrades the query has not matched the full
# text either, and gets no benefit of the doubt.
LENIENT_SOURCES = frozenset({
    "google", "gnews", "gdelt", "academic", "state_media", "serpapi", "notebooklm",
})
MAX_UNVERIFIED_PER_SOURCE = 12


def _doc_text(doc: dict) -> str:
    """
    Every field a match could legitimately live in.

    The URL is included deliberately: a TikTok or Instagram permalink often
    carries the tag when the caption we captured does not. The translated
    variants are included because `enrich_languages` may have run first, and a
    match in either the original or the translation is a match.
    """
    parts = [
        doc.get("title"), doc.get("title_en"),
        doc.get("excerpt"), doc.get("excerpt_en"),
        doc.get("author"), doc.get("url"),
        _meta_text(doc.get("meta")),
    ]
    return normalise(" ".join(p for p in parts if isinstance(p, str) and p))


def _meta_text(meta) -> str:
    """The searchable text inside a doc's `meta`, and nothing else.

    `meta` is a DICT on every document make_doc() builds — hashtags, channel
    name, handle, and also view/like/share COUNTS. Two rules follow:

      1. It cannot be dropped into " ".join() as-is. Doing so raised
         TypeError: sequence item 2: expected str instance, dict found
         on any document with a non-empty meta — i.e. effectively all of them.
         The gate would have taken down every search on first deploy.

      2. Only STRING values are taken. Numeric values are engagement counts,
         and folding them into the matchable text would let a video with
         1,948 views satisfy a query for `1948`. Relevance must come from what
         a document SAYS, never from how popular it is.
    """
    if isinstance(meta, str):
        return meta
    if not isinstance(meta, dict):
        return ""
    bits = []
    for v in meta.values():
        if isinstance(v, str):
            bits.append(v)
        elif isinstance(v, (list, tuple)):
            bits.extend(x for x in v if isinstance(x, str))
    return " ".join(bits)


def score_doc(doc: dict, spec: QuerySpec) -> tuple[float, str]:
    """
    Score one document against the query. Returns (score, basis).

    `basis` is not decoration — it is what makes a wrong rule visible. A caller
    that drops documents must be able to show an analyst why each one went.
    """
    if not spec.canonical:
        return EXACT, "no query to match against"

    text = _doc_text(doc)
    if not text.strip():
        return NO_MATCH, "document has no matchable text"

    for pat, form in zip(spec._patterns, spec.surface_forms):
        if pat.search(text):
            return EXACT, f"exact match: {form!r}"

    for lang, term, pat in spec._expansion_patterns:
        if pat.search(text):
            return EXPANSION, f"{lang} variant: {term!r}"

    # Only reachable for non-fused queries — `tokens` is empty otherwise.
    if spec.tokens and all(_boundary_re(t).search(text) for t in spec.tokens):
        return ALL_TOKENS, "all query terms present, not adjacent"

    return NO_MATCH, "no query term found in title, text, meta or URL"


def partition(docs: list, spec: QuerySpec, floor: float = DEFAULT_FLOOR,
              platform: str | None = None) -> tuple[list, list]:
    """
    Split documents into (kept, dropped), annotating both.

    Both halves are returned. The caller is expected to keep a sample of the
    dropped set in the payload so the gate can be audited from the UI rather
    than trusted on faith.

    `platform` selects the lenient policy described at LENIENT_SOURCES. Lenient
    documents are admitted at UNVERIFIED and always sort below every verified
    match, so stratified sampling downstream reaches real evidence first and
    falls back to unverified context only when it runs out.
    """
    lenient = (platform or "") in LENIENT_SOURCES
    kept, dropped, unverified = [], [], []
    for d in docs:
        score, basis = score_doc(d, spec)
        if score >= floor:
            d["relevance"], d["relevance_basis"] = round(score, 2), basis
            kept.append(d)
        elif lenient:
            d["relevance"] = UNVERIFIED
            d["relevance_basis"] = (
                f"kept unverified — {platform} matched this against the full page, "
                f"but the query term is not in the snippet we stored")
            unverified.append(d)
        else:
            d["relevance"], d["relevance_basis"] = round(score, 2), basis
            dropped.append(d)

    # Rank unverified by engagement so the cap keeps the most-read context.
    unverified.sort(key=lambda d: -(d.get("engagement") or 0))
    for d in unverified[MAX_UNVERIFIED_PER_SOURCE:]:
        d["relevance"], d["relevance_basis"] = 0.0, (
            f"no query term in snippet; beyond the {MAX_UNVERIFIED_PER_SOURCE}-document "
            f"unverified allowance for {platform}")
        dropped.append(d)
    kept.extend(unverified[:MAX_UNVERIFIED_PER_SOURCE])

    kept.sort(key=lambda d: (-d.get("relevance", 0), -(d.get("engagement") or 0)))
    return kept, dropped
