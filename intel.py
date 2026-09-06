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

import os
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


# U1: the audited "Hezbollah" corpus spanned 16.4 years and the confidence
# figure took no notice, so a 2006 opinion poll counted toward a present-tense
# assessment exactly as much as last week's strikes. These two constants define
# what "current" means for that judgement and how much dating evidence has to
# exist before the judgement is allowed at all.
#
# 90 days: long enough that a story with a month of build-up still reads as
# current, short enough that last year's coverage cannot pass as this week's.
# 20 documents: below that, a share is arithmetic on a handful of timestamps
# rather than a property of the corpus, and the gate is skipped rather than
# guessed (see the recency gate in score_threat).
_RECENCY_WINDOW_DAYS = 90
_RECENCY_MIN_DATED = 20

# Publisher clocks drift and some feeds stamp a scheduled publication time, so a
# few hours into the future is noise; anything beyond that is a broken timestamp
# and is discarded rather than counted as maximally recent. Same tolerance the
# velocity computation upstream already applies, for the same reason.
_FUTURE_SKEW_HOURS = 2.0


def _parse_ts(value):
    """
    Best-effort UTC datetime from whatever a source put in a document's
    `timestamp`, or None.

    Deliberately forgiving and deliberately silent: the callers here use dates
    to weight confidence, so an unparseable date must degrade to "undated" —
    which is excluded from the measurement — and never to a plausible-looking
    wrong date, which would be counted as evidence.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        ts = float(value)
        if ts > 1e12:          # milliseconds
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except Exception:
            return None
    s = str(value).strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_ts(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt:
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except Exception:
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S",
                "%Y-%m-%d"):
        try:
            dt = datetime.strptime(s, fmt)
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except Exception:
            continue
    return None


def _recency_counts(docs: list) -> tuple[int, int]:
    """
    (usably dated documents, of which published within _RECENCY_WINDOW_DAYS).

    Undated documents are counted in NEITHER figure rather than assumed old.
    Most feeds stamp a date; the ones that do not are a source quirk, and
    treating a missing timestamp as age would penalise a platform for its
    metadata rather than the corpus for being stale.
    """
    now = datetime.now(timezone.utc)
    dated = recent = 0
    for d in docs:
        dt = _parse_ts((d or {}).get("timestamp"))
        if not dt:
            continue
        age_h = (now - dt).total_seconds() / 3600.0
        if age_h < -_FUTURE_SKEW_HOURS:
            continue
        dated += 1
        if age_h <= _RECENCY_WINDOW_DAYS * 24:
            recent += 1
    return dated, recent


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


def _dur(hours) -> str:
    """Hours as something a reader can picture.

    A corpus spanning years reported "spread over 22394.4h", which is precise,
    unreadable, and reads like a bug in a panel whose whole job is to make the
    score believable.
    """
    try:
        h = float(hours or 0)
    except (TypeError, ValueError):
        return "unknown"
    if h < 1:    return f"{round(h * 60)}min"
    if h < 48:   return f"{h:.0f}h"
    d = h / 24
    if d < 60:   return f"{d:.0f}d"
    if d < 730:  return f"{d / 30.44:.0f}mo"
    return f"{d / 365.25:.1f}y"


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

    def _is_wire_syndication(group) -> bool:
        """Distinguish a syndicated news story from a scripted amplification net.

        Verified defect: GDELT documents set `excerpt == title` and `author ==
        the publishing domain`. A Reuters story carried by six outlets therefore
        arrives as six "identical texts from six distinct authors" — the exact
        signature this function treats as high-severity inauthenticity. Measured
        on a real corpus: 6 high-credibility news documents produced an
        inauthenticity score of 26, severity HIGH, feeding info-integrity at
        weight 45.

        Syndication is a normal, visible, attributable publishing practice. It
        is not an influence operation, and reporting it as one both wastes the
        analyst's attention and makes every genuine finding less believable.

        The test is deliberately conservative — it only excuses a group where
        EVERY member is a news-type document from a DIFFERENT domain, which is
        what syndication looks like and what a sock-puppet network does not.
        """
        if len(group) < 2:
            return False
        domains = set()
        for g in group:
            if (g.get("source_type") or "social") not in ("news", "state_media", "academic"):
                return False
            dom = (g.get("author") or "").strip().lower()
            if not dom or " " in dom:      # a real handle, not a domain
                return False
            domains.add(dom)
        return len(domains) == len(group)

    copypasta, syndicated = [], []
    for body, group in by_text.items():
        authors = {(g.get("author") or "").strip().lower() for g in group if g.get("author")}
        if len(group) >= 3 and len(authors) >= 3 and _is_wire_syndication(group):
            syndicated.append({"text": body[:160], "outlets": sorted(authors)[:8],
                               "copies": len(group)})
            continue
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

    # Reported, but as context rather than as a finding: an analyst should be
    # able to see that the corpus contains syndicated copy, and should not see
    # it counted as amplification.
    if syndicated:
        signals.append({
            "type": "wire_syndication",
            "severity": "info",
            "description": (f"{len(syndicated)} story/stories carried verbatim by multiple "
                            f"distinct outlets — normal news syndication, excluded from the "
                            f"inauthenticity score"),
            "examples": syndicated[:3],
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
                    f"spread over {_dur(prop.get('spread_hours'))}")))
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
    rel = payload.get("relevance") or {}
    # A8: the old formula was 0.55*coverage*100 + 0.45*vol_conf, so full signal
    # coverage alone scored 55 before a single document was read — "moderate"
    # confidence on zero evidence, and "high" on eight. Coverage answers "did
    # the signals exist", never "was there enough material to judge", so it must
    # not be able to carry the score by itself. A multiplicative volume gate
    # makes that structural: below ~100 documents the whole confidence figure is
    # scaled down, and no amount of coverage escapes it.
    vol_gate = 0.35 + 0.65 * min(1.0, n_docs / 100.0)

    # U1: the volume gate was the ONLY gate, and on the audited "Hezbollah" run
    # it did nothing at all (407 documents saturates it), leaving CONFIDENCE
    # 94.5 printed directly above two caveats this same function had just
    # written: GDELT partially dark, and 37 of 444 collected documents discarded
    # as off-topic. Both of those facts moved the caveat list and neither moved
    # the number. That is precisely the failure this module's opening principle
    # forbids — a caveat nobody is charged for is decoration, and a reader who
    # sees 94.5 in large type does not deduct for prose underneath it.
    #
    # Three further gates follow, each in [0,1], each multiplicative and each
    # named in `confidence_drivers`, so the reduction is ATTRIBUTABLE: a reader
    # can see which condition cost what rather than being handed one
    # unaccountable figure. They are multiplicative rather than subtractive for
    # the same reason vol_gate is: a subtractive penalty can be outrun by a high
    # coverage term, and these conditions are not things a good score should be
    # able to outrun.

    # ── Purity gate ──────────────────────────────────────────────────────────
    # 407 survivors filtered out of 444 is not the same evidence base as 407
    # clean hits. Heavy filtering means the query was ambiguous enough that the
    # gate had to work hard, and a gate working hard has an error rate in both
    # directions — some kept documents are noise and some dropped ones were not.
    # The floor is 0.60 rather than 0 because the survivors are still real,
    # read documents: noise removed is not evidence destroyed. The gate reaches
    # that floor at 50% noise, the point past which it is the collection, not
    # the corpus, that needs explaining.
    purity_gate = 1.0
    noise_ratio = rel.get("noise_ratio") if rel.get("enabled") else None
    if isinstance(noise_ratio, (int, float)) and not isinstance(noise_ratio, bool) \
            and noise_ratio > 0:
        purity_gate = max(0.60, 1.0 - 0.40 * min(1.0, float(noise_ratio) / 0.50))

    # ── Source gate ──────────────────────────────────────────────────────────
    # `snap["degraded"]` names sources that went dark or returned partial data
    # mid-run. Before U1 it produced a caveat string and touched nothing else,
    # so a run with GDELT missing scored identically to a run with GDELT whole.
    # Signal coverage does not cover this case: on the audited run coverage was
    # still 90% because the GDELT-derived factors DEGRADED rather than vanished,
    # and a tone average computed from a partial index is not a tone average.
    #
    # ENGINE ERRORS ARE COUNTED HERE AND NOWHERE ELSE. The old subtractive
    # `engine_penalty` — 12 points each, capped at 35 — has been folded into
    # this gate at the same calibration (12 points off a ~100-point scale is
    # ×0.88) and removed from the sum above, so an engine failure is charged
    # exactly once. The per-source cost is the same 0.12: a named source going
    # dark is at least as expensive as an analysis stage crashing, because both
    # mean a signal that was supposed to exist does not.
    degraded = snap.get("degraded")
    degraded_n = (len(degraded) if isinstance(degraded, (list, tuple, set))
                  else (1 if degraded else 0))
    source_loss = min(0.30, 0.12 * degraded_n) + min(0.35, 0.12 * len(engine_errors))
    source_gate = max(0.50, 1.0 - source_loss)

    # ── Recency gate ─────────────────────────────────────────────────────────
    # The audited corpus spanned 16.4 years. A 2006 opinion poll and a 2008 film
    # clip carried the same weight as last week's strikes, and the assessment
    # they fed is written in the present tense. Volume was standing in for
    # quality with no reference to time at all, which is how an archive of a
    # long-running story reads as high confidence about this week.
    #
    # If fewer than _RECENCY_MIN_DATED documents are usably dated the gate is
    # skipped entirely at 1.0 rather than estimated: a share computed from a
    # dozen timestamps is not a measurement of a 407-document corpus, and
    # guessing would reintroduce exactly the dishonesty this gate removes.
    recency_gate = 1.0
    dated_docs, recent_docs = _recency_counts(docs)
    recent_share = None
    if dated_docs >= _RECENCY_MIN_DATED:
        recent_share = recent_docs / dated_docs
        # Full marks at three-quarters current rather than at 100%: background
        # and context coverage is normal and desirable in any real corpus and
        # should not be charged for. Floor 0.55 — an archival corpus still
        # describes something that genuinely happened, it just does not
        # describe now, so it is discounted rather than dismissed.
        recency_gate = 0.55 + 0.45 * min(1.0, recent_share / 0.75)

    confidence = _clamp(
        (0.55 * coverage * 100 + 0.45 * vol_conf)
        * vol_gate * purity_gate * source_gate * recency_gate)
    # U1: the decimal place asserted a resolution this method does not have.
    # Every input above is a judgement call good to within several points, and
    # printing 94.5 invited a reader to believe the .5 carried information.
    # Whole numbers only.
    confidence = float(round(confidence))

    # `confidence_drivers` is what makes the number interrogable instead of
    # merely believable — the same principle as `factors`, applied to the
    # confidence figure, which until now was the one number in this module that
    # arrived without its workings. Only gates that actually moved the score are
    # listed, because a wall of "1.00 — no effect" rows teaches a reader to skip
    # the block; the volume gate is always listed because corpus size is the
    # first thing anyone asks of a confidence figure and its absence would read
    # as an omission rather than as a pass.
    confidence_drivers = [{
        "label": "Evidence volume",
        "value": round(vol_gate, 2),
        "detail": (f"{n_docs} documents is a full evidence base for this measure, so "
                   f"corpus size is not holding this figure down."
                   if vol_gate >= 0.99 else
                   f"Only {n_docs} documents were kept, below the roughly 100 at which "
                   f"this measure stops discounting for a thin evidence base."),
    }]
    if purity_gate < 0.99:
        _dropped = rel.get("dropped")
        _collected = rel.get("collected")
        _pct = round(float(noise_ratio) * 100)
        confidence_drivers.append({
            "label": "Corpus purity",
            "value": round(purity_gate, 2),
            "detail": ((f"{_dropped} of {_collected} collected documents were off-topic "
                        f"and excluded ({_pct}% noise). "
                        if _dropped and _collected else
                        f"{_pct}% of the documents collected were off-topic and excluded. ")
                       + "The survivors are real evidence, but a query that needed that "
                         "much filtering is a less certain one than a clean match."),
        })
    if source_gate < 0.99:
        if degraded_n and engine_errors:
            _src_detail = (
                f"A source went dark or returned partial data during this run, and "
                f"{len(engine_errors)} analysis stage(s) failed. Parts of this "
                f"assessment rest on less evidence than a complete run would give.")
        elif degraded_n:
            _src_detail = (
                "GDELT was unavailable or returned only partial data for this run, so "
                "the geographic and tone signals rest on less evidence than the "
                "coverage figure alone suggests.")
        else:
            _src_detail = (
                f"{len(engine_errors)} analysis stage(s) failed during this run, so "
                f"signals that should have been measured were never computed.")
        confidence_drivers.append({
            "label": "Source availability",
            "value": round(source_gate, 2),
            "detail": _src_detail,
        })
    if recency_gate < 0.99 and recent_share is not None:
        confidence_drivers.append({
            "label": "Corpus recency",
            "value": round(recency_gate, 2),
            "detail": (f"Only {round(recent_share * 100)}% of the {dated_docs} dated "
                       f"documents were published in the last {_RECENCY_WINDOW_DAYS} "
                       f"days. Much of this corpus describes the past, while the "
                       f"assessment above is written about the present."),
        })

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
    # U1: the 16.4-year span of the audited corpus was visible nowhere on the
    # page. The recency gate now charges for it, but a reader who does not open
    # the confidence breakdown still needs to be told, because it changes what
    # the whole assessment is ABOUT — a judgement on a long-running story rather
    # than on this week's turn in it.
    if recent_share is not None and recent_share < 0.5:
        caveats.append(
            f"Only {round(recent_share * 100)}% of the {dated_docs} dated documents are "
            f"from the last {_RECENCY_WINDOW_DAYS} days — this corpus is substantially "
            f"archival, and the assessment reflects the story's history as much as its "
            f"current state.")

    # Corpus purity. `n_docs` above counts documents that PASSED the relevance
    # gate, which is what makes the confidence figure defensible — before the
    # gate existed, "#covid1948" produced confidence 94.5 ("high") from 530
    # documents of which 389 contained no form of the query at all. A reader who
    # is told only the surviving number cannot tell a clean corpus from a heavily
    # filtered one, and those warrant different levels of trust, so say it.
    if rel.get("enabled") and rel.get("dropped"):
        caveats.append(
            f"{rel['dropped']} of {rel['collected']} collected documents were off-topic "
            f"and excluded ({round(rel.get('noise_ratio', 0) * 100)}% noise). Every score "
            f"here is computed from the {rel.get('kept')} that matched the query.")
    elif rel.get("enabled") is False:
        caveats.insert(0, (
            "Relevance filtering was DISABLED for this run. Scores include every "
            "document each source returned, on-topic or not, and should not be "
            "compared against a filtered assessment."))

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
            # Part of the reproducibility fingerprint: the same query over the
            # same window scores differently under a different relevance floor,
            # so the floor and the corpus it produced belong in the record.
            "relevance_threshold": (payload.get("relevance") or {}).get("threshold"),
            "docs_collected": (payload.get("relevance") or {}).get("collected"),
            "docs_dropped": (payload.get("relevance") or {}).get("dropped"),
            "timespan_hours": snap.get("timespan_hours"),
            "gdelt_cached": bool(snap.get("cached")),
        },
        "confidence": int(confidence),
        "confidence_band": ("high" if confidence >= 70 else
                            "moderate" if confidence >= 45 else "low"),
        # U1: each named gate and what it did to the number above, so the
        # confidence figure can be argued with rather than only accepted.
        "confidence_drivers": confidence_drivers,
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

# U7: where the operational dimension's referent comes from. Nothing in XTag
# supplied one before — the dimension was computed and printed regardless — so
# these are the places a referent CAN legitimately arrive: explicitly on the
# payload for a per-search subject, or from the deployment's environment for an
# installation that monitors one organisation and always means the same thing by
# "operational". Env is checked last so a per-search subject always wins.
#
# A referent is a name this system can look for in the corpus. It is deliberately
# not inferred from the query: "Hezbollah" is what the analyst searched for, not
# what they operate, and treating the two as the same thing is the exact
# substitution U7 exists to stop.
_SUBJECT_PAYLOAD_KEYS = ("watch_subject", "monitored_asset", "subject",
                         "organisation", "organization")
_SUBJECT_ENV_VAR = "XTAG_WATCH_SUBJECT"


def _watch_subject(payload: dict) -> str | None:
    """The configured operational referent, or None if nobody supplied one."""
    for key in _SUBJECT_PAYLOAD_KEYS:
        val = (payload or {}).get(key)
        if isinstance(val, str) and val.strip():
            return val.strip()
        if isinstance(val, dict):
            name = val.get("name") or val.get("label") or val.get("subject")
            if isinstance(name, str) and name.strip():
                return name.strip()
    return os.environ.get(_SUBJECT_ENV_VAR, "").strip() or None


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
        # U7 made `available` load-bearing for the operational dimension; it is
        # set on all four so a consumer can branch on one key instead of having
        # to know which dimensions are capable of withholding themselves.
        "available": True, "reason": None,
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
        "available": True, "reason": None,
        "score": round(s, 1), "band": _band_cov(s, cov), "coverage": round(cov * 100, 1),
        "factors": fd,
        "rationale": ("Whether this narrative is being manufactured and pushed rather "
                      "than emerging organically."),
    }

    # ── Operational ──────────────────────────────────────────────────────────
    # U7: this dimension used to score coverage volume, acceleration and platform
    # spread and render the result as "OPERATIONAL 73.8 HIGH" — on the audited
    # "Hezbollah" run, the largest number on the page. Operational risk to WHOM?
    # XTag does not know what the analyst operates. Those three signals measure
    # how big and how fast the STORY is, which the threat score and the
    # reputational dimension already report; filing them under "operational"
    # silently asserted a subject nobody had entered, and a reader supplies their
    # own employer by reflex. The other three dimensions are properties of the
    # information environment and need no referent. This one is a relation
    # between that environment and something outside it.
    #
    # So the dimension now requires a referent. Given one, the three magnitude
    # signals are kept but subordinated to the only thing that makes them
    # operational rather than ambient: how much of this corpus actually names the
    # subject. Given none, no number is produced, because there is no defensible
    # number to produce.
    subject = _watch_subject(payload)
    if not subject:
        _op_reason = (
            "Operational impact is exposure of a specific subject — an organisation, "
            "asset, site or person that you operate — and no subject was configured "
            "for this search. Coverage volume, acceleration and platform spread "
            "describe how large the narrative is, not what it does to you; presenting "
            "them under this heading would attribute a risk to a subject that was "
            "never supplied. Configure a watch subject to assess this dimension.")
        _op_factors = [
            Factor("subject_exposure", "Subject exposure", 40, None,
                   reason="no watch subject configured for this search"),
            Factor("volume", "Coverage volume", 25, None,
                   reason="withheld — no subject to relate coverage volume to"),
            Factor("velocity", "Acceleration", 20, None,
                   reason="withheld — no subject to relate acceleration to"),
            Factor("platforms", "Platform spread", 15, None,
                   reason="withheld — no subject to relate platform spread to"),
        ]
        _, _, fd = _composite(_op_factors)
        # NOTE THE ABSENT "score" KEY, which is the whole point of the fix.
        # It is not 0 and it is not None: 0 renders as a number and reads as
        # "operational risk: none", a finding this system did not make and the
        # inverse of the honest answer; None renders as a literal null in the
        # existing risk cell. The renderer's fallback for a score it cannot find
        # is an em-dash, which IS the honest answer, so the key is simply not
        # emitted. Every consumer that ranks or aggregates these dimensions must
        # therefore check `available` before reaching for `score` — see the
        # ranking at the end of this function.
        dims["operational"] = {
            "available": False,
            "reason": _op_reason,
            "band": "unknown",
            "coverage": 0.0,
            "factors": fd,
            "subject": None,
            "rationale": ("Exposure of a named subject to this narrative. Not assessed "
                          "for this search: no watch subject was configured, and this "
                          "dimension has no meaning without one."),
        }
    else:
        gd_total = (snap.get("volume") or {}).get("total")
        vol_score = _log_scale(gd_total, 3000) if gd_total else (
            _log_scale(n_docs, 600) if n_docs else None)
        _reached = (payload.get("propagation") or {}).get("platforms_reached")
        # Tokens rather than a substring so "acme" does not match "acmesoft".
        # The length filter drops articles and initials; if that empties the set
        # — a two-letter subject like "BP" — fall back to the raw tokens, since
        # matching nothing at all would score a genuine exposure as zero.
        _subj_norm = _norm_text(subject)
        subj_tokens = {t for t in _subj_norm.split() if len(t) > 2} or set(_subj_norm.split())
        subj_hits = 0
        if subj_tokens:
            for d in docs:
                body = _norm_text((d.get("title") or "") + " " + (d.get("excerpt") or ""))
                if body and subj_tokens.issubset(set(body.split())):
                    subj_hits += 1
        subj_share = (subj_hits / n_docs) if n_docs else 0.0
        # No multiplier and no curve. The score IS the percentage of the corpus
        # that names the subject, because there is no defensible basis for
        # steepening it and an invented one would put this dimension straight
        # back where U7 found it. A very large narrative that never mentions you
        # scores near zero here and should: that is the distinction the dimension
        # was missing, not a bug in it.
        subj_score = _clamp(subj_share * 100) if n_docs >= 5 else None
        f = [
            Factor("subject_exposure", "Subject exposure", 40, subj_score,
                   detail=(f"{subj_hits} of {n_docs} documents name {subject}"
                           if subj_score is not None else None),
                   reason=None if subj_score is not None
                   else "too few documents to measure subject exposure"),
            Factor("volume", "Coverage volume", 25, vol_score,
                   detail=(f"{gd_total or n_docs} items" if vol_score is not None else None),
                   reason=None if vol_score is not None else "no volume signal"),
            Factor("velocity", "Acceleration", 20, accel_score,
                   detail=accel if accel_score is not None else None,
                   reason=None if accel_score is not None else "no velocity signal"),
            Factor("platforms", "Platform spread", 15,
                   _log_scale(_reached, 10) if _reached else None,
                   detail=f"{_reached} platforms" if _reached else None,
                   reason=None if _reached else "no propagation trace"),
        ]
        # Exposure carries 40 of the 100 — more than any single magnitude signal,
        # less than all three together — because a narrative that names you is
        # the precondition for this dimension meaning anything, while how loud
        # and how fast it is determines what answering it would cost.
        s, cov, fd = _composite(f)
        dims["operational"] = {
            "available": True,
            "reason": None,
            "score": round(s, 1), "band": _band_cov(s, cov), "coverage": round(cov * 100, 1),
            "factors": fd,
            "subject": subject,
            "rationale": (f"How far this narrative has attached itself to {subject} — the "
                          f"share of the corpus that names it, weighted by the scale and "
                          f"speed of the coverage any response would have to be made in."),
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
        "available": True, "reason": None,
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

    # U7: this ranking was `sorted(dims.items(), key=lambda kv: kv[1]["score"])`,
    # which assumes every dimension carries a number. A withheld dimension no
    # longer does, and the two obvious repairs — defaulting it to 0, or scoring
    # it as 0 upstream — would both smuggle in the exact claim that withholding
    # exists to prevent ("operational risk: none") and would do it in the one
    # field the UI renders largest. Unassessed dimensions are excluded from the
    # ranking and named separately instead, so absent can never be read as low.
    _scored = [(k, v) for k, v in dims.items()
               if v.get("available") and v.get("score") is not None]
    ordered = sorted(_scored, key=lambda kv: kv[1]["score"], reverse=True)
    unassessed = sorted(k for k, v in dims.items() if not v.get("available"))
    return {
        "dimensions": dims,
        "highest": {"dimension": ordered[0][0], "score": ordered[0][1]["score"],
                    "band": ordered[0][1]["band"]} if ordered else None,
        # Named explicitly so a caller reading `highest` cannot conclude that the
        # dimensions it does not mention were measured and came back quiet.
        "unassessed_dimensions": unassessed,
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
