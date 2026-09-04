"""
XTag red-team harness — measure coordination detection instead of asserting it.

WHY THIS EXISTS
---------------
DARPA's SMISC programme did not evaluate detectors on found data. It injected
synthetic campaigns with known ground truth into a real corpus and scored what
came back, because a coordination score computed on a corpus where nobody knows
the answer cannot be right or wrong — it can only be plausible. XTag has been in
exactly that position: `detect_coordination` returns a number between 0 and 100
and no one can say what it should have been.

This module supplies ground truth. It builds organic traffic, builds campaigns
whose every document is labelled, mixes them, runs the detector, and scores the
result with the SMISC Bot Challenge formula:

    score = Hits − 0.25 · Misses + Speed

A campaign kind exists for each evasion the detector's implementation invites.
That is deliberate: the point of a harness is to find the breaking point, not to
produce a good number. `python harness.py` prints the table.

WHAT THE HARNESS IS NOT
-----------------------
Synthetic adversaries are a floor, never a ceiling. Passing here means the
detector survives the evasions we thought of. Ferrara's finding that confirmed
operations sit 7–70× above a matched organic baseline is the other half of this
work — see `baseline_ratio()` — because a detector that fires at 1.3× baseline
is describing a fandom.
"""
from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

PLATFORMS = ["x", "youtube", "reddit", "telegram", "tiktok", "instagram",
             "gnews", "state_media"]

# Organic vocabulary — deliberately overlapping so the harness cannot be passed
# by a detector that simply flags any repeated wording.
_TOPICS = [
    "vaccine mandate protest downtown", "new travel restrictions announced",
    "hospital capacity latest figures", "minister responds to criticism",
    "court blocks the emergency order", "study questions the official numbers",
    "rally draws thousands to the square", "police disperse the crowd",
    "opposition demands an inquiry", "border checks tightened overnight",
]
_HEDGES = ["reportedly", "apparently", "according to sources", "allegedly", ""]
_TAILS = ["— full story", "(thread)", "what we know so far", "live updates", ""]

# Real coverage of one event is lexically diverse: different outlets, different
# users, different angles, mostly different words. An organic generator built
# from ten fixed phrases produces a corpus that genuinely IS coordinated, and
# grading a detector against it measures nothing — the first version of this
# file did exactly that and made the new detector look like it false-alarmed on
# organic traffic when the traffic was the problem.
_SUBJ = ["the health ministry", "protesters", "the opposition leader", "police",
         "a hospital director", "the mayor", "local residents", "the court",
         "an independent panel", "the prime minister", "union representatives",
         "a whistleblower", "the regional governor", "emergency services",
         "the electoral commission", "a group of doctors", "the interior ministry"]
_VERB = ["questioned", "confirmed", "disputed", "released", "delayed", "reviewed",
         "criticised", "defended", "expanded", "suspended", "investigated",
         "welcomed", "rejected", "postponed", "clarified", "escalated"]
_OBJ = ["the new figures", "yesterday's decision", "the emergency measures",
        "the vaccination schedule", "the inquiry findings", "the border policy",
        "the curfew order", "the funding package", "the leaked documents",
        "the testing regime", "the compensation scheme", "the draft legislation",
        "the审查 timetable".replace("审查 ", "review "), "the closure notice"]
_PLACE = ["in the capital", "across three provinces", "at the northern border",
          "outside parliament", "in the eastern districts", "nationwide",
          "in two major cities", "at the main hospital", ""]


@dataclass
class Doc:
    platform: str
    title: str
    excerpt: str
    url: str
    timestamp: str
    engagement: int
    author: str = ""
    injected: bool = False
    campaign: str | None = None

    def as_dict(self) -> dict:
        return {"platform": self.platform, "title": self.title,
                "excerpt": self.excerpt, "url": self.url, "author": self.author,
                "timestamp": self.timestamp, "engagement": self.engagement,
                "meta": {"likes": self.engagement}}


@dataclass
class Corpus:
    docs: list[Doc]
    injected_urls: set[str] = field(default_factory=set)

    def platforms(self) -> dict:
        out: dict[str, dict] = {}
        for d in self.docs:
            g = out.setdefault(d.platform, {"platform": d.platform,
                                            "results": [], "error": None})
            g["results"].append(d.as_dict())
        return out


def _ts(base: datetime, minutes: float) -> str:
    return (base + timedelta(minutes=minutes)).isoformat().replace("+00:00", "Z")


def generate_organic(n: int, rng: random.Random, base: datetime) -> list[Doc]:
    """Organic traffic: heavy-tailed engagement, diurnal timing, reused phrasing.

    The phrasing overlap matters. Real coverage of one event repeats itself —
    wire copy, quote-tweets, aggregators — so an organic corpus with perfectly
    unique text would let a naive duplicate detector score 100% and learn
    nothing.
    """
    docs = []
    for i in range(n):
        # ~17 x 16 x 14 x 9 x 5 x 5 combinations, so two organic posts sharing a
        # 5-word shingle is uncommon but not impossible — which is the point.
        text = " ".join(x for x in (
            rng.choice(_HEDGES), rng.choice(_SUBJ), rng.choice(_VERB),
            rng.choice(_OBJ), rng.choice(_PLACE), rng.choice(_TAILS)) if x)
        # Diurnal: posts cluster in waking hours rather than spreading uniformly.
        day = rng.randrange(0, 7)
        hour = min(23, max(0, int(rng.gauss(14, 4))))
        minute = rng.randrange(0, 60)
        # Heavy tail — a handful of posts carry most of the engagement.
        eng = int(math.floor((rng.paretovariate(1.16) - 1) * 40))
        docs.append(Doc(
            platform=rng.choice(PLATFORMS),
            title=text,
            excerpt=text + " " + " ".join(x for x in (
                rng.choice(_SUBJ), rng.choice(_VERB), rng.choice(_OBJ)) if x),
            url=f"https://organic.example/{i}-{rng.randrange(10**6)}",
            timestamp=_ts(base, day * 1440 + hour * 60 + minute),
            # A long tail of mostly-distinct accounts, with a few prolific
            # posters — the shape real traffic has. If organic accounts were
            # perfectly unique the harness would flatter any detector that keys
            # on repeat posting.
            author=f"user_{int(abs(rng.gauss(0, n / 2.2))) % max(1, int(n * 0.8)):05d}",
            engagement=eng))
    return docs


# ── Campaign generators ──────────────────────────────────────────────────────
# Each kind isolates one evasion. The names say what is being tested, and the
# docstrings say what the current detector does about it.

def _campaign_base(rng, base, n, spread_min):
    start = rng.uniform(0, 5 * 1440)
    return [start + rng.uniform(0, spread_min) for _ in range(n)]


def _accounts(rng, n_posts: int, posts_per_account: float = 2.0,
              prefix: str = "sock") -> list[str]:
    """Sock-puppet accounts for a campaign.

    Coordination is a property of ACCOUNTS acting together, so a harness whose
    documents have no author cannot test for it — every document collapses into
    one actor and no actor pair exists to be similar. (This was a real bug in
    the first version of this file: recall went to zero at n=64 not because
    detection failed but because there was nothing to detect between.)

    posts_per_account is the operator's central trade-off: fewer accounts means
    a louder per-account signal and easier detection; more accounts costs money.
    """
    n = max(2, int(round(n_posts / max(1.0, posts_per_account))))
    return [f"{prefix}_{rng.randrange(16**6):06x}{i}" for i in range(n)]


def _salt(rng) -> str:
    """Per-run URL salt.

    Without this, campaign URLs were identical across seeds, so the "5 seeds"
    in baseline_ratio were only independent on the organic side and a detector
    could in principle score well by recognising the fixture rather than the
    behaviour. Ground truth must be re-drawn every run.
    """
    return f"{rng.randrange(16**8):08x}"


def campaign_copypasta(n, rng, base, spread_min=25) -> list[Doc]:
    """Identical text pushed across many platforms in a tight window.

    The textbook case, and the one the current detector is built for: 4-word
    shingles, Jaccard > 0.55, cross-platform only. This should be caught.
    """
    text = "The official figures are fabricated. Demand an independent audit now."
    accts = _accounts(rng, n)
    salt = _salt(rng)
    times = _campaign_base(rng, base, n, spread_min)
    return [Doc(platform=PLATFORMS[i % len(PLATFORMS)], title=text, excerpt=text,
                url=f"https://camp.example/cp/{salt}-{i}", timestamp=_ts(base, times[i]),
                author=accts[i % len(accts)], engagement=rng.randrange(0, 30), injected=True, campaign="copypasta")
            for i in range(n)]


def campaign_same_platform(n, rng, base, spread_min=25) -> list[Doc]:
    """Identical text, ONE platform — a single-platform botnet.

    `detect_coordination` skips any pair where `da.platform == db.platform`, so
    this campaign is invisible to the duplicate signal by construction, no
    matter how blatant it is. Included to measure a known blind spot rather than
    to be surprised by it.
    """
    text = "The official figures are fabricated. Demand an independent audit now."
    accts = _accounts(rng, n)
    salt = _salt(rng)
    times = _campaign_base(rng, base, n, spread_min)
    return [Doc(platform="x", title=text, excerpt=text,
                url=f"https://camp.example/sp/{salt}-{i}", timestamp=_ts(base, times[i]),
                author=accts[i % len(accts)], engagement=rng.randrange(0, 30), injected=True, campaign="same_platform")
            for i in range(n)]


_PARA = [
    "Those official numbers are made up — we need an independent audit.",
    "Independent auditors must review these fabricated government figures.",
    "Nobody should trust the published data. An outside audit is overdue.",
    "The published statistics do not hold up. Call in independent auditors.",
    "An audit by outsiders is the only way to check these invented numbers.",
]


def campaign_paraphrase(n, rng, base, spread_min=25) -> list[Doc]:
    """One claim, many wordings — the same operation run through a rewriter.

    Word-level shingles cannot see this: two sentences carrying an identical
    claim share almost no 4-grams. This is the single cheapest evasion available
    to an operator and costs one LLM call per post.
    """
    accts = _accounts(rng, n)
    salt = _salt(rng)
    times = _campaign_base(rng, base, n, spread_min)
    return [Doc(platform=PLATFORMS[i % len(PLATFORMS)],
                title=_PARA[i % len(_PARA)], excerpt=_PARA[(i + 2) % len(_PARA)],
                url=f"https://camp.example/pp/{salt}-{i}", timestamp=_ts(base, times[i]),
                author=accts[i % len(accts)], engagement=rng.randrange(0, 30), injected=True, campaign="paraphrase")
            for i in range(n)]


def campaign_url_variants(n, rng, base, spread_min=40) -> list[Doc]:
    """The same destination behind per-post tracking parameters.

    `cross_platform_url_sharing` groups on the raw URL string, so appending
    `?utm_source=` defeats it without changing where the link goes. URL
    canonicalisation is the fix and is queued as P4.
    """
    accts = _accounts(rng, n)
    salt = _salt(rng)
    times = _campaign_base(rng, base, n, spread_min)
    return [Doc(platform=PLATFORMS[i % len(PLATFORMS)],
                title=f"Read this before you vote {i}", excerpt="Important context.",
                url=f"https://target.example/story?utm_source=s{salt}{i}&utm_campaign=c{i}",
                timestamp=_ts(base, times[i]), engagement=rng.randrange(0, 30),
                author=accts[i % len(accts)], injected=True, campaign="url_variants") for i in range(n)]


def campaign_slow_burn(n, rng, base, spread_min=6 * 24 * 60) -> list[Doc]:
    """Identical text, but spread over a week instead of half an hour.

    Tests whether detection depends on the burst signal. An operator who can
    wait is a different adversary from one who cannot.
    """
    text = "The official figures are fabricated. Demand an independent audit now."
    accts = _accounts(rng, n)
    salt = _salt(rng)
    times = _campaign_base(rng, base, n, spread_min)
    return [Doc(platform=PLATFORMS[i % len(PLATFORMS)], title=text, excerpt=text,
                url=f"https://camp.example/sb/{salt}-{i}", timestamp=_ts(base, times[i]),
                author=accts[i % len(accts)], engagement=rng.randrange(0, 30), injected=True, campaign="slow_burn")
            for i in range(n)]


def campaign_adaptive(n, rng, base, spread_min=4 * 24 * 60) -> list[Doc]:
    """An adversary that has read this file.

    Every evasion at once: paraphrase to beat shingles, per-post URL parameters
    to beat URL grouping, a slow spread to beat the burst threshold, and
    engagement kept low so `_top_docs` — which ranks by engagement — is unlikely
    to place these documents inside the detector's 100-document comparison
    window at all.

    This is the campaign that matters. A detector is only as good as its result
    against an adversary who knows how it works, and real operators do.
    """
    accts = _accounts(rng, n, posts_per_account=1.0)
    salt = _salt(rng)
    times = _campaign_base(rng, base, n, spread_min)
    docs = []
    for i in range(n):
        claim = _PARA[i % len(_PARA)]
        filler = rng.choice(_TOPICS)
        docs.append(Doc(platform=PLATFORMS[i % len(PLATFORMS)],
                        title=f"{filler} — {claim}",
                        excerpt=f"{claim} {rng.choice(_TAILS)}",
                        url=f"https://t{i%4}.example/p?ref={salt}{rng.randrange(10**6)}",
                        timestamp=_ts(base, times[i]),
                        engagement=rng.randrange(0, 4),   # stay below the fold
                        author=accts[i % len(accts)], injected=True, campaign="adaptive"))
    return docs


CAMPAIGNS = {
    "copypasta": campaign_copypasta,
    "same_platform": campaign_same_platform,
    "paraphrase": campaign_paraphrase,
    "url_variants": campaign_url_variants,
    "slow_burn": campaign_slow_burn,
    "adaptive": campaign_adaptive,
}


def build(kind: str, n_campaign: int, n_organic: int = 400, seed: int = 7) -> Corpus:
    rng = random.Random(seed)
    base = datetime(2026, 8, 1, tzinfo=timezone.utc)
    docs = generate_organic(n_organic, rng, base)
    camp = CAMPAIGNS[kind](n_campaign, rng, base) if n_campaign else []
    docs.extend(camp)
    rng.shuffle(docs)
    return Corpus(docs=docs, injected_urls={d.url for d in camp})


# ── Scoring ──────────────────────────────────────────────────────────────────

def flagged_urls(detection: dict) -> set[str]:
    """Which documents the detector actually pointed at.

    `detect_coordination` returns aggregate signals, so ground truth has to be
    recovered from the examples it cites. A detector that raises its score
    without naming documents scores nothing here — and that is correct, because
    an analyst cannot act on a number that points at nothing.
    """
    # A detector that publishes its full flagged set gets scored on it. The old
    # detector did not, so its recall was bounded by however many examples it
    # chose to cite — and an early version of THIS function scored the new
    # detector the same way, reporting 9% recall on a campaign it had actually
    # flagged 64 documents of, because it read only the 6 cited examples. The
    # cap belongs in the UI, never in the measurement.
    explicit = detection.get("flagged_urls")
    if isinstance(explicit, (list, set, tuple)):
        return {u for u in explicit if u}
    urls: set[str] = set()
    for sig in detection.get("signals") or []:
        for ex in sig.get("examples") or []:
            if isinstance(ex, dict):
                for k in ("url_a", "url_b"):
                    if ex.get(k): urls.add(ex[k])
            elif isinstance(ex, str):
                urls.add(ex)
    return urls


@dataclass
class Result:
    kind: str
    n_campaign: int
    hits: int
    misses: int
    false_positives: int
    recall: float
    precision: float
    smisc: float
    seconds: float
    coordination_score: int
    risk: str

    def row(self) -> str:
        return (f"{self.kind:<15} {self.n_campaign:>4}  {self.hits:>5} {self.misses:>7} "
                f"{self.false_positives:>4}  {self.recall:>6.0%} {self.precision:>10.0%} "
                f"{self.smisc:>8.1f}  {self.coordination_score:>4} {self.risk:>7}  "
                f"{self.seconds:>5.2f}s")


HEADER = (f"{'campaign':<15} {'n':>4}  {'hits':>5} {'misses':>7} {'fp':>4}  "
          f"{'recall':>6} {'precision':>10} {'SMISC':>8}  {'score':>4} {'risk':>7}  {'time':>6}")


def evaluate(detect_fn, kind: str, n_campaign: int, n_organic: int = 400,
             seed: int = 7) -> Result:
    """Run one detector against one labelled corpus.

    `Speed` in the SMISC formula is worth at most one point here and is scaled
    against a 10-second reference. It is included for fidelity to the original
    scoring, but it is deliberately small: a fast wrong answer is still wrong,
    and weighting speed heavily is how a harness ends up rewarding a detector
    that returns early.
    """
    corpus = build(kind, n_campaign, n_organic, seed)
    t0 = time.monotonic()
    detection = detect_fn(corpus.platforms())
    seconds = time.monotonic() - t0

    flagged = flagged_urls(detection)
    truth = corpus.injected_urls
    hits = len(flagged & truth)
    misses = len(truth - flagged)
    fp = len(flagged - truth)
    speed = max(0.0, 1.0 - seconds / 10.0)
    return Result(
        kind=kind, n_campaign=n_campaign, hits=hits, misses=misses,
        false_positives=fp,
        recall=(hits / len(truth)) if truth else 0.0,
        precision=(hits / len(flagged)) if flagged else 0.0,
        smisc=hits - 0.25 * misses + speed,
        seconds=seconds,
        coordination_score=detection.get("coordination_score", 0),
        risk=detection.get("risk", "?"))


def sweep(detect_fn, kind: str, sizes=(4, 8, 16, 32, 64), **kw) -> list[Result]:
    """Vary campaign size to find where detection begins and where it saturates."""
    return [evaluate(detect_fn, kind, n, **kw) for n in sizes]


def breaking_point(results: list[Result], min_recall: float = 0.5) -> int | None:
    """The smallest campaign this detector still finds half of. None = never."""
    for r in sorted(results, key=lambda r: r.n_campaign):
        if r.recall >= min_recall:
            return r.n_campaign
    return None


# ── P3-2: the matched organic baseline ───────────────────────────────────────

def baseline_ratio(detect_fn, kind: str, n_campaign: int, n_organic: int = 400,
                   trials: int = 5) -> dict:
    """Express a coordination score as a MULTIPLE of matched organic traffic.

    An absolute score of 40 is not a finding. Ferrara's cross-campaign work put
    confirmed operations at 7–70× a matched organic baseline; a corpus scoring
    1.3× baseline is a fandom, a breaking news event, or a wire story — all of
    which look coordinated to an absolute threshold because, in the ordinary
    sense of the word, they are.

    The baseline here is the SAME organic generator with the campaign removed,
    over several seeds, so the comparison holds topic, volume and platform mix
    fixed and varies only the thing being measured.
    """
    organic, injected = [], []
    for s in range(trials):
        organic.append(detect_fn(build(kind, 0, n_organic, seed=100 + s)
                                 .platforms()).get("coordination_score", 0))
        injected.append(detect_fn(build(kind, n_campaign, n_organic, seed=100 + s)
                                  .platforms()).get("coordination_score", 0))
    b = sum(organic) / len(organic)
    c = sum(injected) / len(injected)
    return {"baseline_mean": round(b, 1),
            "baseline_max": max(organic),
            "with_campaign_mean": round(c, 1),
            # A baseline of zero cannot produce a ratio. Say so rather than
            # dividing by a fudge factor and reporting a confident infinity.
            "ratio": (round(c / b, 2) if b > 0 else None),
            "separated": c > max(organic),
            "note": ("baseline is 0 — any positive score separates, but the "
                     "ratio is undefined and must not be reported as one"
                     if b <= 0 else "")}


def main() -> None:
    import sys, os
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import app

    detect = app.detect_coordination
    print("\nXTag coordination detector — red-team harness")
    print("SMISC scoring: Hits - 0.25*Misses + Speed;  400 organic documents\n")
    print(HEADER); print("-" * len(HEADER))
    all_results: dict[str, list[Result]] = {}
    for kind in CAMPAIGNS:
        rs = sweep(detect, kind)
        all_results[kind] = rs
        for r in rs: print(r.row())
        print()

    print("BREAKING POINT — smallest campaign detected at >=50% recall")
    print("-" * 58)
    for kind, rs in all_results.items():
        bp = breaking_point(rs)
        print(f"  {kind:<15} {('%d documents' % bp) if bp else 'NEVER DETECTED'}")

    print("\nMATCHED ORGANIC BASELINE (P3-2) — 5 seeds each")
    print("-" * 58)
    for kind in CAMPAIGNS:
        b = baseline_ratio(detect, kind, 32)
        ratio = f"{b['ratio']}x" if b["ratio"] is not None else "undefined"
        print(f"  {kind:<15} organic {b['baseline_mean']:>5} (max {b['baseline_max']:>3})  "
              f"+campaign {b['with_campaign_mean']:>5}  ratio {ratio:>9}  "
              f"{'separates' if b['separated'] else 'INDISTINGUISHABLE'}")
    print()


if __name__ == "__main__":
    main()
