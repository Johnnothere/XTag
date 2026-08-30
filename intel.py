"""
Assessment layer — turns collected signals into a judgement.

Everything upstream of this module answers "what is being said?". This module
answers "how much should you care, and why?" — the step that separates an
intelligence platform from a search aggregator.

DESIGN PRINCIPLE: NO BLACK-BOX SCORES
Every score here ships with the factors that produced it, each carrying its own
raw value, weight and contribution. An analyst who cannot see why a number is 78
cannot defend it, act on it, or notice when it is wrong. `factors` is not
decoration — it is the actual output; the number is a summary of it.

DESIGN PRINCIPLE: CONFIDENCE IS SEPARATE FROM SEVERITY
A high threat score computed from three documents is not a high threat, it is a
guess. Severity and confidence are reported independently and never folded
together, because a confident low score and an unreliable high score demand
opposite responses from the reader.

DESIGN PRINCIPLE: SAY WHAT IS MISSING
When a signal is unavailable (GDELT rate-limited, sentiment disabled, no social
sources configured) the factor is marked unavailable and excluded from the
weighted total rather than scored as zero. Absent evidence is not evidence of
absence, and scoring it as zero silently drags every total toward "nothing to
see here" — the single most dangerous failure mode a threat score can have.
"""

from __future__ import annotations

import re
import math
import logging
from collections import defaultdict, Counter
from datetime import datetime, timezone

log = logging.getLogger(__name__)


# ── Small helpers ─────────────────────────────────────────────────────────────

def _clamp(v: float, lo: float = 0.0, hi: float = 100.0) -> float:
    return max(lo, min(hi, v))


def _all_docs(platforms: dict) -> list:
    out = []
    for group in (platforms or {}).values():
        out.extend((group or {}).get("results") or [])
    return out


def _log_scale(value: float, full_at: float) -> float:
    """
    Map a count to 0-100 on a logarithmic curve that reaches ~100 at `full_at`.

    Linear scaling is wrong for volume: the difference between 10 and 60 articles
    is a real change in posture, while 5000 vs 5050 is noise. Log scaling keeps
    the low end sensitive, which is where narrative emergence actually shows up.
    """
    if value <= 0:
        return 0.0
    if full_at <= 1:
        return 100.0
    return _clamp(math.log1p(value) / math.log1p(full_at) * 100.0)


class Factor:
    """One contributing signal in a composite score."""

    __slots__ = ("key", "label", "weight", "score", "available", "detail", "reason")

    def __init__(self, key, label, weight, score=None, detail=None, reason=None):
        self.key = key
        self.label = label
        self.weight = weight
        self.available = score is not None
        self.score = float(score) if score is not None else None
        self.detail = detail
        self.reason = reason        # why unavailable, when it is

    def as_dict(self, total_weight: float) -> dict:
        # Contribution is expressed against the weight that was ACTUALLY used,
        # so the visible contributions always sum to the headline score even
        # when some factors dropped out.
        contrib = None
        # E4: `weight` is the CONFIGURED weight, but the composite renormalises
        # over available factors only — so a factor configured at 20 can actually
        # be carrying 50% of the score once others drop out, and displaying "20"
        # understates it by more than a factor of two. Both numbers are emitted:
        # `weight` for the design intent, `contribution_pct` for what this factor
        # actually contributed to THIS assessment.
        contribution_pct = None
        if self.available and total_weight > 0:
            contrib = round(self.score * self.weight / total_weight, 1)
            contribution_pct = round(self.weight / total_weight * 100, 1)
        return {
            "key": self.key, "label": self.label,
            "weight": self.weight, "contribution_pct": contribution_pct,
            "available": self.available,
            "score": round(self.score, 1) if self.available else None,
            "contribution": contrib,
            "detail": self.detail, "reason": self.reason,
        }


def _composite(factors: list[Factor]) -> tuple[float, float, list[dict]]:
    """
    Weighted mean over AVAILABLE factors only, plus a coverage ratio.

    Renormalising over available weight is what stops a missing signal from
    silently suppressing the score. Coverage then tells the reader how much of
    the intended evidence base actually existed for this assessment.
    """
    usable = [f for f in factors if f.available]
    total_w = sum(f.weight for f in usable)
    if total_w <= 0:
        return 0.0, 0.0, [f.as_dict(0) for f in factors]
    score = sum(f.score * f.weight for f in usable) / total_w
    coverage = total_w / sum(f.weight for f in factors)
    return _clamp(score), coverage, [f.as_dict(total_w) for f in factors]


# The minimum corpus size at which any document-derived judgement is worth
# making. detect_inauthenticity already used 8 as its floor; A7 promotes it to a
# module constant so the threat band can apply the same standard.
MIN_ASSESSABLE_DOCS = 8


def _band(score: float) -> str:
    if score >= 75: return "critical"
    if score >= 55: return "high"
    if score >= 35: return "elevated"
    if score >= 18: return "moderate"
    return "low"


def _band_cov(score: float, coverage: float) -> str:
    """
    Band a score, but refuse to band it at all when the evidence base was too
    thin to support one. Used everywhere a score is presented, so that "low"
    always means "we looked and it is low" and never "we could not look".
    """
    return "unknown" if coverage < 0.25 else _band(score)


# ══════════════════════════════════════════════════════════════════════════════
# INAUTHENTICITY / INFLUENCE-OPERATION DETECTION
# ══════════════════════════════════════════════════════════════════════════════

# Handles like "PatriotVoice8837" or "news_bot_2291" — a display name followed by
# a run of digits with no separator is the classic bulk-registration signature.
_HANDLE_NUMERIC_TAIL = re.compile(r"^[A-Za-z][A-Za-z_]{2,}\d{4,}$")
_HANDLE_RANDOM = re.compile(r"^[a-z]{6,}\d{2,}$")


def _norm_text(s: str) -> str:
    s = re.sub(r"https?://\S+", " ", str(s or "").lower())
    s = re.sub(r"[^\w\s]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def detect_inauthenticity(platforms: dict, coordination: dict | None = None) -> dict:
    """
    Content- and account-level markers of inauthentic amplification.

    `coordination` is accepted for call-site compatibility and deliberately NOT
    used in the score (see A9 below): coordination is already a separately
    weighted factor everywhere this score is consumed.

    This is deliberately NOT a bot classifier. Real bot detection needs account
    age, follower graphs and posting history that none of XTag's sources expose.
    What is observable here is the shape of an amplification campaign, so that is
    what gets measured — and the output says so, rather than implying a
    confidence about individual accounts that the evidence cannot support.
    """
    docs = _all_docs(platforms)

    # Below a floor of documents this analysis cannot say anything. Returning
    # score 0 here would be actively misleading: "no inauthenticity signals
    # found in 2 documents" is not evidence of authenticity, but a 0 feeds the
    # threat composite as a real, confident low reading and drags the headline
    # score down. Unavailable is the honest answer, and the composite excludes
    # it rather than counting it as clean.
    if len(docs) < MIN_ASSESSABLE_DOCS:
        return {
            "score": None, "band": None, "signals": [], "signal_count": 0,
            "copypasta_clusters": 0, "suspicious_handle_ratio": None,
            "laundering_chains": 0, "assessed_docs": len(docs),
            "unavailable_reason": (f"only {len(docs)} document(s) collected — below the "
                                   f"minimum needed to assess amplification patterns"),
            "method_note": ("Campaign-shape analysis, not per-account bot classification."),
        }

    signals: list[dict] = []

    # ── 1. Verbatim reposting (copypasta) ────────────────────────────────────
    # Distinct from the near-duplicate check in detect_coordination(): that one
    # looks for similar text across DIFFERENT platforms. This one looks for
    # exactly identical text from DIFFERENT authors, which is the signature of a
    # supplied script rather than independent people reaching the same wording.
    by_text: dict[str, list] = defaultdict(list)
    for d in docs:
        body = _norm_text((d.get("title") or "") + " " + (d.get("excerpt") or ""))
        if len(body) < 40:
            continue
        by_text[body].append(d)

    copypasta = []
    for body, group in by_text.items():
        authors = {(g.get("author") or "").strip().lower() for g in group if g.get("author")}
        if len(group) >= 3 and len(authors) >= 3:
            copypasta.append({
                "text": body[:160],
                "copies": len(group),
                "distinct_authors": len(authors),
                "platforms": sorted({g.get("platform") for g in group if g.get("platform")}),
                "examples": [g.get("url") for g in group[:4] if g.get("url")],
            })
    copypasta.sort(key=lambda c: c["copies"], reverse=True)
    if copypasta:
        worst = copypasta[0]["copies"]
        signals.append({
            "type": "verbatim_repost_network",
            "severity": "high" if worst >= 6 else "medium",
            "description": (f"{len(copypasta)} message(s) reposted verbatim by 3+ distinct "
                            f"accounts (worst: {worst} copies)"),
            "examples": copypasta[:3],
        })

    # ── 2. Account handle patterns ───────────────────────────────────────────
    handles = [(d.get("author") or "").strip().lstrip("@") for d in docs]
    handles = [h for h in handles if h]
    suspicious = [h for h in handles
                  if _HANDLE_NUMERIC_TAIL.match(h) or _HANDLE_RANDOM.match(h)]
    handle_ratio = (len(suspicious) / len(handles)) if handles else 0.0
    if handles and handle_ratio > 0.15 and len(suspicious) >= 4:
        signals.append({
            "type": "bulk_registration_handles",
            "severity": "high" if handle_ratio > 0.35 else "medium",
            "description": (f"{len(suspicious)} of {len(handles)} author handles "
                            f"({handle_ratio*100:.0f}%) match bulk-registration patterns "
                            f"(name followed by a long digit run)"),
            "examples": suspicious[:8],
        })

    # ── 3. Single-author flooding ────────────────────────────────────────────
    # Established outlets and news wires are excluded. A newsroom byline on 30
    # articles is a newsroom doing its job, not an account flooding a hashtag —
    # counting it as flooding made a plain Reuters news story score as
    # inauthentic in testing, which is exactly the false positive that would
    # destroy an analyst's trust in this whole module.
    social_handles = [
        (d.get("author") or "").strip().lstrip("@")
        for d in docs
        if (d.get("source_type") or "social") not in ("news", "academic")
        and (d.get("credibility") or "unknown") not in ("high", "medium")
    ]
    social_handles = [h for h in social_handles if h]
    author_counts = Counter(h.lower() for h in social_handles)
    floods = [{"author": a, "posts": n} for a, n in author_counts.most_common(10)
              if n >= 8]
    if floods:
        signals.append({
            "type": "single_author_flooding",
            "severity": "medium",
            "description": (f"{len(floods)} non-institutional account(s) posting 8+ times "
                            f"on this query"),
            "examples": floods[:5],
        })

    # ── 4. Narrative laundering: state media -> unattributed pickup ──────────
    # The classic influence-op pipeline is state outlet -> fringe aggregator ->
    # mainstream, with attribution stripped at each hop. Observable proxy: the
    # same claim appearing in a state-credibility source AND in sources that
    # carry no credibility rating at all.
    state_docs = [d for d in docs if d.get("credibility") == "state"]
    unknown_docs = [d for d in docs if d.get("credibility") == "unknown"]
    laundering = []
    if state_docs and unknown_docs:
        def shingles(d, k=5):
            w = _norm_text((d.get("title") or "") + " " + (d.get("excerpt") or "")).split()
            return {" ".join(w[i:i+k]) for i in range(max(0, len(w) - k + 1))}
        state_sh = [(d, shingles(d)) for d in state_docs[:40]]
        unk_sh = [(d, shingles(d)) for d in unknown_docs[:80]]
        for sd, ss in state_sh:
            if not ss:
                continue
            for ud, us in unk_sh:
                if not us:
                    continue
                sim = len(ss & us) / max(len(ss | us), 1)
                if sim > 0.35:
                    laundering.append({
                        "state_source": sd.get("author") or sd.get("platform"),
                        "state_url": sd.get("url"),
                        "pickup_url": ud.get("url"),
                        "pickup_platform": ud.get("platform"),
                        "similarity": round(sim, 2),
                    })
                    break
    if laundering:
        signals.append({
            "type": "narrative_laundering",
            "severity": "high" if len(laundering) >= 4 else "medium",
            "description": (f"{len(laundering)} claim(s) traceable from state-controlled "
                            f"media into unattributed secondary sources"),
            "examples": laundering[:4],
        })

    # ── 5. Impersonation heuristics ──────────────────────────────────────────
    # Handles that closely mimic a known outlet name without being it — e.g.
    # "BBCBreaking_" or "reuters_live". Low precision by nature, so severity is
    # capped at low and it is reported as "review these", never as a finding.
    OUTLETS = ["bbc", "reuters", "aljazeera", "cnn", "nytimes", "guardian", "afp",
               "apnews", "skynews", "france24", "dw", "npr", "bloomberg"]
    impersonators = []
    for h in set(handles):
        hl = h.lower()
        for o in OUTLETS:
            if o in hl and hl != o and not hl.endswith(".com"):
                if re.search(rf"{o}[\W_]*(news|breaking|live|official|update|24|tv)?\d*$", hl):
                    impersonators.append(h)
                    break
    if impersonators:
        signals.append({
            "type": "possible_outlet_impersonation",
            "severity": "low",
            "description": (f"{len(impersonators)} handle(s) resemble major-outlet names "
                            f"without matching them — verify before citing"),
            "examples": impersonators[:8],
        })

    # ── Composite inauthenticity score ───────────────────────────────────────
    # A9: the coordination score used to be folded in here at 0.33 weight. That
    # double-counted it, because score_threat and assess_risk BOTH already score
    # coordination as an independent weighted factor alongside this one. The
    # visible symptom was a factor row reading "score 26.4 / 0 signals; 0
    # clusters" — a non-zero score whose own evidence string said there was no
    # evidence. This score is now purely its own signals; coordination is
    # scored where it belongs, once.
    sev_weight = {"high": 26, "medium": 14, "low": 5}
    score = _clamp(sum(sev_weight.get(s["severity"], 5) for s in signals))

    return {
        "score": round(score, 1),
        "band": _band(score),
        "signals": signals,
        "signal_count": len(signals),
        "copypasta_clusters": len(copypasta),
        "suspicious_handle_ratio": round(handle_ratio, 3),
        "laundering_chains": len(laundering),
        "assessed_docs": len(docs),
        "method_note": ("Campaign-shape analysis, not per-account bot classification. "
                        "Account age, follower graphs and posting history are not "
                        "available from these sources, so no claim is made about any "
                        "individual account being automated."),
    }


# ══════════════════════════════════════════════════════════════════════════════
# AUDIENCE + GEOGRAPHIC INTELLIGENCE
# ══════════════════════════════════════════════════════════════════════════════

def build_audience(platforms: dict, languages: dict | None = None,
                   gdelt_snapshot: dict | None = None) -> dict:
    """
    Who is being reached, in what language, and where.

    Two independent evidence bases are combined: GDELT's country/language
    breakdown (broad, global, news-only) and the document-level language
    distribution from the collected corpus (narrower, but spans social). They
    are reported separately as well as merged, because when they disagree that
    disagreement is itself the signal — a narrative loud in Arabic social but
    absent from Arabic news media is a different phenomenon from one in both.
    """
    docs = _all_docs(platforms)
    snap = gdelt_snapshot or {}
    geo = snap.get("geography") or {}
    aud = snap.get("audience") or {}

    # Corpus-side language distribution
    corpus_langs = Counter()
    for d in docs:
        corpus_langs[(d.get("language") or "en")] += 1
    corpus_total = sum(corpus_langs.values()) or 1
    corpus_dist = [{"code": c, "docs": n, "share": round(n / corpus_total * 100, 1)}
                   for c, n in corpus_langs.most_common(15)]

    countries = geo.get("countries") or []
    # A10: geo["countries"] is truncated to 40 entries for DISPLAY, so len() on
    # it saturated breadth at 40 and made the full_at=45 curve unreachable —
    # a narrative in 40 countries scored identically to one in 120. GDELT
    # already returns the real count; use it, and fall back to the list length
    # only when the field is absent.
    reach_countries = geo.get("total_countries")
    if not reach_countries:
        reach_countries = len(countries)

    # Reach breadth: how many national medias carry it at all. A narrative in
    # 40 countries is categorically different from one in 3, independent of volume.
    breadth = _log_scale(reach_countries, 45) if reach_countries else None

    non_en_share = aud.get("non_english_share")
    if non_en_share is None and corpus_dist:
        non_en_share = round(100 - sum(x["share"] for x in corpus_dist if x["code"] == "en"), 1)

    # Amplifier vs originator: countries whose share of coverage far exceeds
    # what their media volume would normally produce are amplifying, not reporting.
    top = countries[:12]
    amplifiers = [c for c in top if c.get("share", 0) >= 8]

    return {
        "countries": countries[:25],
        "country_count": reach_countries,
        "geographic_concentration": geo.get("concentration"),
        "top3_country_share": geo.get("top3_share"),
        "geographic_breadth_score": round(breadth, 1) if breadth is not None else None,
        "gdelt_languages": (aud.get("languages") or [])[:15],
        "corpus_languages": corpus_dist,
        "non_english_share": non_en_share,
        "primary_amplifiers": amplifiers,
        "language_count": aud.get("total_languages") or len(corpus_langs),
        "available": bool(countries or corpus_dist),
        "degraded": snap.get("degraded") or [],
    }


# ══════════════════════════════════════════════════════════════════════════════
# NARRATIVE THREAT SCORE
# ══════════════════════════════════════════════════════════════════════════════

def score_threat(payload: dict, gdelt_snapshot: dict | None = None,
                 inauth: dict | None = None, audience: dict | None = None) -> dict:
    """
    Composite 0-100 narrative threat score.

    The inputs are signals XTag was already collecting and discarding. Nothing
    here needs new plumbing — the gap this closes is interpretive, not technical.

    Weights reflect what actually distinguishes a dangerous narrative from a
    merely loud one. Coordination and inauthenticity carry the most weight
    because organic anger and a manufactured campaign can look identical on
    volume alone, and the response to each is completely different.
    """
    snap = gdelt_snapshot or {}
    inauth = inauth or {}
    audience = audience or {}
    platforms = payload.get("platforms") or {}
    docs = _all_docs(platforms)
    n_docs = len(docs)

    coordination = payload.get("coordination") or {}
    velocity = payload.get("velocity") or {}
    sentiment = payload.get("sentiment") or {}
    narratives = payload.get("narratives") or []

    factors: list[Factor] = []

    # ── Coordination (20) ────────────────────────────────────────────────────
    cs = coordination.get("coordination_score")
    factors.append(Factor(
        "coordination", "Coordinated distribution", 20,
        score=cs if cs is not None else None,
        detail=(f"{coordination.get('near_duplicate_pairs', 0)} cross-platform near-duplicates, "
                f"peak burst {coordination.get('peak_burst_30min', 0)}/30min")
        if cs is not None else None,
        reason=None if cs is not None else "coordination analysis unavailable"))

    # ── Inauthenticity (18) ──────────────────────────────────────────────────
    isc = inauth.get("score")
    factors.append(Factor(
        "inauthenticity", "Inauthentic amplification", 18,
        score=isc if isc is not None else None,
        detail=(f"{inauth.get('signal_count', 0)} signal(s); "
                f"{inauth.get('copypasta_clusters', 0)} verbatim clusters, "
                f"{inauth.get('laundering_chains', 0)} laundering chains")
        if isc is not None else None,
        reason=None if isc is not None
        else (inauth.get("unavailable_reason") or "inauthenticity analysis unavailable")))

    # ── Velocity / acceleration (14) ─────────────────────────────────────────
    accel = velocity.get("acceleration")
    windows = velocity.get("windows") or {}
    if accel:
        base = {"accelerating": 80, "stable": 35, "declining": 12}.get(accel, 35)
        # A spike detected by GDELT's own hourly volume series corroborates the
        # corpus-level acceleration and pushes the factor up.
        sigma = (snap.get("volume") or {}).get("spike_sigma")
        if sigma and sigma > 3:
            base = min(100, base + 15)
        factors.append(Factor(
            "velocity", "Spread velocity", 14, score=base,
            detail=(f"{accel}; {windows.get('6h', 0)} docs in 6h vs "
                    f"{windows.get('24h', 0)} in 24h"
                    + (f"; GDELT peak {sigma}σ above baseline" if sigma else ""))))
    else:
        factors.append(Factor("velocity", "Spread velocity", 14,
                              reason="no timestamped documents to measure velocity"))

    # ── Reach / volume (12) ──────────────────────────────────────────────────
    gd_total = (snap.get("volume") or {}).get("total")
    if gd_total:
        # GDELT's global article count is a far better reach proxy than the
        # corpus size, which is capped by our own fetch limits.
        factors.append(Factor(
            "reach", "Coverage volume", 12, score=_log_scale(gd_total, 3000),
            detail=f"{gd_total} articles in GDELT's global news index"))
    elif n_docs:
        factors.append(Factor(
            "reach", "Coverage volume", 12, score=_log_scale(n_docs, 600),
            detail=f"{n_docs} documents collected (GDELT volume unavailable)"))
    else:
        factors.append(Factor("reach", "Coverage volume", 12,
                              reason="no documents collected"))

    # ── Geographic breadth (10) ──────────────────────────────────────────────
    gb = audience.get("geographic_breadth_score")
    factors.append(Factor(
        "geography", "Geographic breadth", 10,
        score=gb if gb is not None else None,
        detail=(f"{audience.get('country_count')} countries' media, "
                f"{audience.get('geographic_concentration')} spread")
        if gb is not None else None,
        reason=None if gb is not None else "GDELT geographic breakdown unavailable"))

    # ── Hostile tone (10) ────────────────────────────────────────────────────
    tone_avg = (snap.get("tone") or {}).get("average")
    if tone_avg is not None:
        # GDELT tone: roughly -10..+10, most news -5..+2. Map negativity to 0-100,
        # treating -8 as the practical floor.
        tone_score = _clamp((-tone_avg / 8.0) * 100)
        trend = (snap.get("tone") or {}).get("trend")
        if trend == "worsening":
            tone_score = min(100, tone_score + 12)
        factors.append(Factor(
            "tone", "Hostile framing (news tone)", 10, score=tone_score,
            detail=f"GDELT avg tone {tone_avg}, {trend}"))
    else:
        net = sentiment.get("net")
        scored = sentiment.get("scored") or 0
        if net is not None and scored >= 5:
            factors.append(Factor(
                "tone", "Hostile framing (sentiment)", 10,
                score=_clamp((-net) * 100),
                detail=f"net sentiment {net} across {scored} scored documents"))
        else:
            factors.append(Factor("tone", "Hostile framing", 10,
                                  reason="no tone or sentiment signal available"))

    # ── Source credibility mix (8) ───────────────────────────────────────────
    cred = Counter(d.get("credibility") or "unknown" for d in docs)
    if n_docs >= 5:
        state_share = cred.get("state", 0) / n_docs
        unknown_share = cred.get("unknown", 0) / n_docs
        # State-controlled and unattributable sources both raise concern; high-
        # credibility coverage lowers it, because a story mainstream outlets are
        # reporting straight is a different object from one only fringe carries.
        high_share = (cred.get("high", 0) + cred.get("medium", 0)) / n_docs
        cscore = _clamp(state_share * 110 + unknown_share * 45 - high_share * 25)
        factors.append(Factor(
            "credibility", "Source credibility mix", 8, score=cscore,
            detail=(f"{cred.get('state',0)} state-controlled, {cred.get('unknown',0)} "
                    f"unattributed, {cred.get('high',0)+cred.get('medium',0)} established "
                    f"of {n_docs}")))
    else:
        factors.append(Factor("credibility", "Source credibility mix", 8,
                              reason="too few documents to assess source mix"))

    # ── Cross-platform propagation (8) ───────────────────────────────────────
    prop = payload.get("propagation") or {}
    reached = prop.get("platforms_reached")
    if reached:
        factors.append(Factor(
            "propagation", "Cross-platform propagation", 8,
            score=_log_scale(reached, 10),
            detail=(f"{reached} platforms; origin {prop.get('origin')}, "
                    f"spread over {prop.get('spread_hours')}h")))
    else:
        factors.append(Factor("propagation", "Cross-platform propagation", 8,
                              reason="propagation trace unavailable"))

    score, coverage, factor_dicts = _composite(factors)

    # ── Confidence, reported separately from severity ────────────────────────
    # Three independent things degrade confidence: thin evidence, missing
    # factors, and analysis-engine failures. A score built on 4 documents with
    # half the factors dark should never present as authoritative.
    vol_conf = _clamp(_log_scale(n_docs, 250))
    engine_errors = payload.get("engine_errors") or {}
    engine_penalty = min(35, len(engine_errors) * 12)
    # A8: the old formula was 0.55*coverage*100 + 0.45*vol_conf, so full signal
    # coverage alone scored 55 before a single document was read — "moderate"
    # confidence on zero evidence, and "high" on eight. Coverage answers "did
    # the signals exist", never "was there enough material to judge", so it must
    # not be able to carry the score by itself. A multiplicative volume gate
    # makes that structural: below ~100 documents the whole confidence figure is
    # scaled down, and no amount of coverage escapes it.
    vol_gate = 0.35 + 0.65 * min(1.0, n_docs / 100.0)
    confidence = _clamp(
        (0.55 * coverage * 100 + 0.45 * vol_conf - engine_penalty) * vol_gate)

    caveats = []
    if n_docs < 25:
        caveats.append(f"Only {n_docs} documents collected — treat as indicative, not conclusive.")
    if coverage < 0.6:
        missing = [f["label"] for f in factor_dicts if not f["available"]]
        caveats.append("Signals unavailable: " + ", ".join(missing) + ".")
    if engine_errors:
        caveats.append("Analysis engine reported errors: " + ", ".join(sorted(engine_errors)) + ".")
    if snap.get("degraded"):
        caveats.append("GDELT partially unavailable — geographic and tone signals may be thin.")

    drivers = sorted([f for f in factor_dicts if f["available"]],
                     key=lambda f: (f["contribution"] or 0), reverse=True)[:3]

    # THE MOST IMPORTANT THREE LINES IN THIS MODULE.
    # Below a quarter of the intended signal base, a computed score is an
    # artefact of what happened to be measurable, not an assessment. Reporting
    # "0 / 100 — LOW" when nothing was collected would present total ignorance
    # as an all-clear, which is the worst thing a threat system can do: a reader
    # acts on green. "Unknown" forces the reader to look at why instead.
    #
    # A7: coverage alone was not a sufficient guard. GDELT-only factors (reach +
    # geography + tone = 32 of 100 weight) clear the 25% bar without a single
    # document being collected, so a real-looking band could be produced from an
    # empty corpus. The document floor is now part of the same guard, using the
    # module's existing MIN_ASSESSABLE_DOCS.
    band = _band(score)
    if coverage < 0.25:
        band = "unknown"
        caveats.insert(0, (
            f"Insufficient signal to assess — only {round(coverage*100)}% of the "
            f"intended evidence base was available. This is NOT an all-clear; it "
            f"means the assessment could not be made."))
    elif n_docs < MIN_ASSESSABLE_DOCS:
        band = "unknown"
        caveats.insert(0, (
            f"Insufficient documents to assess — {n_docs} collected, minimum "
            f"{MIN_ASSESSABLE_DOCS}. The numeric score below is derived almost "
            f"entirely from GDELT's aggregate index rather than from any corpus "
            f"this system read. This is NOT an all-clear."))

    return {
        "score": round(score, 1),
        "band": band,
        # E5: the fingerprint of what this score was computed FROM. Without it a
        # stored score cannot be reproduced or audited later — two runs of the
        # same query over different corpora are indistinguishable in the record.
        "inputs": {
            "query": payload.get("query"),
            "n_docs": n_docs,
            "timespan_hours": snap.get("timespan_hours"),
            "gdelt_cached": bool(snap.get("cached")),
        },
        "confidence": round(confidence, 1),
        "confidence_band": ("high" if confidence >= 70 else
                            "moderate" if confidence >= 45 else "low"),
        "signal_coverage": round(coverage * 100, 1),
        "factors": factor_dicts,
        "primary_drivers": [{"label": d["label"], "contribution": d["contribution"]}
                            for d in drivers],
        "caveats": caveats,
        "narrative_count": len(narratives),
        "assessed_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# RISK ASSESSMENT (dimensional)
# ══════════════════════════════════════════════════════════════════════════════

def assess_risk(payload: dict, threat: dict, inauth: dict | None = None,
                audience: dict | None = None, gdelt_snapshot: dict | None = None) -> dict:
    """
    Decompose the single threat number into the dimensions an organisation
    actually assigns to different owners.

    One aggregate score cannot be acted on, because the person who handles a
    reputational problem is not the person who handles a physical-security one.
    Each dimension reuses the same underlying signals with different weightings
    and reports its own rationale.

    A NOTE ON PHYSICAL SECURITY: this dimension is intentionally conservative
    and lexical. It flags that violent or threatening language is present in the
    corpus and that a human should look; it does not attempt to predict violence,
    and the output says so. Over-claiming here would be worse than useless.
    """
    inauth = inauth or {}
    audience = audience or {}
    snap = gdelt_snapshot or {}
    docs = _all_docs(payload.get("platforms") or {})
    n_docs = len(docs)

    coordination = payload.get("coordination") or {}
    velocity = payload.get("velocity") or {}
    tone_avg = (snap.get("tone") or {}).get("average")
    sentiment = payload.get("sentiment") or {}

    cred = Counter(d.get("credibility") or "unknown" for d in docs)
    state_share = (cred.get("state", 0) / n_docs) if n_docs else 0.0

    # Negativity 0-100 from whichever tone signal exists
    if tone_avg is not None:
        negativity = _clamp((-tone_avg / 8.0) * 100)
    elif sentiment.get("net") is not None and (sentiment.get("scored") or 0) >= 5:
        negativity = _clamp(-sentiment["net"] * 100)
    else:
        negativity = None

    coord_score = coordination.get("coordination_score")
    inauth_score = inauth.get("score")
    breadth = audience.get("geographic_breadth_score")
    accel = velocity.get("acceleration")
    accel_score = {"accelerating": 80, "stable": 35, "declining": 12}.get(accel) if accel else None

    dims = {}

    # ── Reputational ─────────────────────────────────────────────────────────
    f = [
        Factor("negativity", "Hostile framing", 40, negativity,
               detail=f"tone/sentiment negativity {round(negativity)}" if negativity is not None else None,
               reason=None if negativity is not None else "no tone signal"),
        Factor("breadth", "Geographic breadth", 30, breadth,
               detail=f"{audience.get('country_count')} countries" if breadth is not None else None,
               reason=None if breadth is not None else "GDELT geography unavailable"),
        Factor("velocity", "Acceleration", 30, accel_score,
               detail=accel if accel_score is not None else None,
               reason=None if accel_score is not None else "no velocity signal"),
    ]
    s, cov, fd = _composite(f)
    dims["reputational"] = {
        "score": round(s, 1), "band": _band_cov(s, cov), "coverage": round(cov * 100, 1),
        "factors": fd,
        "rationale": ("Driven by how hostile the framing is, how widely it has spread "
                      "geographically, and whether it is still accelerating."),
    }

    # ── Information integrity ────────────────────────────────────────────────
    f = [
        # E3: detail was passed unconditionally, so an UNAVAILABLE factor still
        # rendered "0 signals" next to "reason: not assessed" — a reader sees a
        # measurement where none was taken. Detail is now None whenever the
        # factor is unavailable.
        Factor("inauthenticity", "Inauthentic amplification", 45, inauth_score,
               detail=(f"{inauth.get('signal_count', 0)} signals"
                       if inauth_score is not None else None),
               reason=None if inauth_score is not None else "not assessed"),
        Factor("coordination", "Coordination", 35, coord_score,
               detail=(f"{coordination.get('near_duplicate_pairs', 0)} near-duplicate pairs"
                       if coord_score is not None else None),
               reason=None if coord_score is not None else "not assessed"),
        Factor("state_media", "State-media share", 20,
               _clamp(state_share * 130) if n_docs >= 5 else None,
               detail=f"{cred.get('state', 0)}/{n_docs} documents from state-controlled outlets"
               if n_docs >= 5 else None,
               reason=None if n_docs >= 5 else "too few documents"),
    ]
    s, cov, fd = _composite(f)
    dims["information_integrity"] = {
        "score": round(s, 1), "band": _band_cov(s, cov), "coverage": round(cov * 100, 1),
        "factors": fd,
        "rationale": ("Whether this narrative is being manufactured and pushed rather "
                      "than emerging organically."),
    }

    # ── Operational ──────────────────────────────────────────────────────────
    gd_total = (snap.get("volume") or {}).get("total")
    vol_score = _log_scale(gd_total, 3000) if gd_total else (
        _log_scale(n_docs, 600) if n_docs else None)
    _reached = (payload.get("propagation") or {}).get("platforms_reached")
    f = [
        Factor("volume", "Coverage volume", 40, vol_score,
               detail=(f"{gd_total or n_docs} items" if vol_score is not None else None),
               reason=None if vol_score is not None else "no volume signal"),
        Factor("velocity", "Acceleration", 35, accel_score,
               detail=accel if accel_score is not None else None,
               reason=None if accel_score is not None else "no velocity signal"),
        Factor("platforms", "Platform spread", 25,
               _log_scale(_reached, 10) if _reached else None,
               detail=f"{_reached} platforms" if _reached else None,
               reason=None if _reached else "no propagation trace"),
    ]
    s, cov, fd = _composite(f)
    dims["operational"] = {
        "score": round(s, 1), "band": _band_cov(s, cov), "coverage": round(cov * 100, 1),
        "factors": fd,
        "rationale": ("Scale and speed of the response this would require if it "
                      "continues on its current trajectory."),
    }

    # ── Physical security (lexical, deliberately conservative) ───────────────
    THREAT_TERMS = [
        "assassinate", "assassination", "kill", "killing", "murder", "execute",
        "bomb", "bombing", "explosive", "detonate", "attack", "strike",
        "retaliate", "retaliation", "revenge", "martyr", "jihad", "raid",
        "kidnap", "hostage", "shooting", "gunmen", "massacre", "ambush",
        "threat", "threaten", "target", "eliminate", "destroy", "burn",
    ]
    hits = Counter()
    flagged_docs = []
    # A1: `flagged_docs` stops growing at 12 because it is a DISPLAY list, but
    # the share used to divide len(flagged_docs) by the full corpus size. The
    # numerator was capped and the denominator was not, so past 12 hits the
    # physical-threat dimension FELL as more violent documents were found —
    # exactly inverted. The share now counts every match; the list stays capped.
    FLAGGED_DISPLAY_CAP = 12
    flagged_total = 0
    for d in docs:
        body = _norm_text((d.get("title") or "") + " " + (d.get("excerpt") or ""))
        if not body:
            continue
        found = [t for t in THREAT_TERMS if re.search(rf"\b{t}\b", body)]
        if found:
            for t in found:
                hits[t] += 1
            flagged_total += 1
            if len(flagged_docs) < FLAGGED_DISPLAY_CAP:
                flagged_docs.append({
                    "url": d.get("url"), "platform": d.get("platform"),
                    "terms": found[:5],
                    "excerpt": (d.get("title") or d.get("excerpt") or "")[:160],
                })
    threat_doc_share = (flagged_total / n_docs) if n_docs else 0.0
    if n_docs >= 10:
        # Density of violent language, amplified when it coincides with a
        # coordinated push — organised hostile messaging warrants a closer look
        # than the same words appearing in scattered news reporting.
        #
        # The multiplier is deliberately not steep. For conflict and security
        # topics — which is most of what this platform is pointed at — violent
        # vocabulary is the BASELINE of ordinary reporting, not an anomaly. At
        # 260x, a routine war-coverage query pinned to 100 and the dimension
        # stopped carrying information. 170x keeps headroom so that genuinely
        # saturated corpora still separate from normal conflict reporting.
        base = _clamp(threat_doc_share * 170)
        if (coord_score or 0) > 50:
            base = min(100, base + 15)
        phys_score = base
        phys_reason = None
    else:
        phys_score = None
        phys_reason = "too few documents for lexical assessment"

    _detail = None
    if phys_score is not None:
        _detail = (f"{flagged_total} of {n_docs} documents contain threat vocabulary")
        if flagged_total > len(flagged_docs):
            _detail += f" (showing {len(flagged_docs)})"
    f = [Factor("violent_language", "Violent/threatening language density", 100,
                phys_score, detail=_detail, reason=phys_reason)]
    s, cov, fd = _composite(f)
    dims["physical_security"] = {
        "score": round(s, 1), "band": _band_cov(s, cov), "coverage": round(cov * 100, 1),
        "factors": fd,
        "top_terms": [{"term": t, "count": c} for t, c in hits.most_common(10)],
        "flagged_documents": flagged_docs,
        "flagged_document_count": flagged_total,
        "rationale": ("Presence and density of violent or threatening vocabulary in the "
                      "corpus, weighted up when it coincides with coordinated distribution."),
        "method_note": ("Lexical screening only. This flags language for human review; "
                        "it does not assess intent, capability or credibility of any "
                        "threat, and must not be read as a prediction of violence."),
    }

    ordered = sorted(dims.items(), key=lambda kv: kv[1]["score"], reverse=True)
    return {
        "dimensions": dims,
        "highest": {"dimension": ordered[0][0], "score": ordered[0][1]["score"],
                    "band": ordered[0][1]["band"]} if ordered else None,
        "overall_band": threat.get("band"),
        "confidence": threat.get("confidence"),
        "assessed_at": datetime.now(timezone.utc).isoformat(),
    }


# ══════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

def assess(payload: dict, gdelt_snapshot: dict | None = None) -> dict:
    """
    Run the full assessment layer over a completed search payload.
    Never raises — a failure in any stage degrades that stage only.
    """
    out = {"inauthenticity": {}, "audience": {}, "threat": {}, "risk": {}, "errors": {}}

    try:
        out["inauthenticity"] = detect_inauthenticity(
            payload.get("platforms") or {}, payload.get("coordination"))
    except Exception as e:
        log.warning("inauthenticity assessment failed: %s", e)
        out["errors"]["inauthenticity"] = str(e)[:160]

    try:
        out["audience"] = build_audience(
            payload.get("platforms") or {}, payload.get("languages"), gdelt_snapshot)
    except Exception as e:
        log.warning("audience assessment failed: %s", e)
        out["errors"]["audience"] = str(e)[:160]

    # E2: assess() recorded its own stage crashes in out["errors"], but
    # score_threat reads payload["engine_errors"] — a key assess() never wrote —
    # so a crashed inauthenticity or audience stage cost the confidence score
    # nothing. Merge them in (on a copy; the caller's payload is not ours to
    # mutate) so an engine failure is actually paid for.
    scoring_payload = payload
    if out["errors"]:
        scoring_payload = {**payload, "engine_errors": {
            **(payload.get("engine_errors") or {}),
            **{f"assess_{k}": v for k, v in out["errors"].items()},
        }}

    try:
        out["threat"] = score_threat(scoring_payload, gdelt_snapshot,
                                     out.get("inauthenticity"), out.get("audience"))
    except Exception as e:
        log.warning("threat scoring failed: %s", e)
        out["errors"]["threat"] = str(e)[:160]

    try:
        out["risk"] = assess_risk(payload, out.get("threat") or {},
                                  out.get("inauthenticity"), out.get("audience"),
                                  gdelt_snapshot)
    except Exception as e:
        log.warning("risk assessment failed: %s", e)
        out["errors"]["risk"] = str(e)[:160]

    # E5: the fingerprint of the inputs this assessment was computed from, so a
    # stored score can be reproduced and audited rather than merely believed.
    snap = gdelt_snapshot or {}
    out["inputs"] = {
        "query": payload.get("query"),
        "n_docs": len(_all_docs(payload.get("platforms") or {})),
        "timespan_hours": snap.get("timespan_hours"),
        "gdelt_cached": bool(snap.get("cached")),
    }
    return out
