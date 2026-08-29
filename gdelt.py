"""
GDELT backbone — the primary news-narrative source for XTag.

WHY THIS IS ITS OWN MODULE
GDELT monitors broadcast, print and web news from nearly every country in 100+
languages and machine-translates 65 of them into English in near real time. For
a platform whose whole purpose is cross-language narrative tracking, that is the
centre of gravity, not one source among fifteen. Using it properly means several
different API modes, a shared rate limiter, a circuit breaker and its own cache —
none of which belongs inline in app.py.

HARD-WON OPERATIONAL FACT — READ BEFORE CHANGING ANYTHING HERE
GDELT rate-limits by IP and does NOT tell you politely. It does not return 429.
It resets the TCP connection mid-handshake (requests raises ConnectionError /
SSLError; curl reports HTTP 000 "connection reset by peer"). During development
roughly 15 requests in a couple of minutes was enough to get an entire container
IP blocked for several minutes, including for endpoints that had just worked.

The consequences shape this whole module:
  * every outbound call goes through _throttle() — a process-wide minimum gap
  * consecutive connection failures trip a circuit breaker, because continuing
    to hammer a blocked endpoint extends the block instead of recovering from it
  * every public function degrades to an empty result and never raises, so a
    throttled GDELT slows the intelligence picture down but never 500s a search
  * analytical modes are cached hard — they are hourly-resolution data, so
    re-fetching them per search would burn the rate limit for no new information

Gunicorn runs --workers 1 --threads 8, so a module-level threading.Lock really
does serialise every GDELT call this deployment makes. If the worker count ever
goes above 1, this rate limiter silently becomes per-worker and the effective
request rate multiplies — see README "Known limitations".

API MODES USED
  doc?mode=artlist              article list (the documents themselves)
  doc?mode=timelinetone         average tone over time
  doc?mode=timelinevolraw       raw article volume over time
  doc?mode=timelinesourcecountry  volume per source country  -> geographic intel
  doc?mode=timelinelang         volume per language          -> audience intel
  geo/geo?mode=PointData        geolocated mentions          -> OSINT geography

NOTE ON artlist FIELDS: artlist does NOT return a tone field. Tone only comes
from the timeline/tonechart modes. Any code that reads article["tone"] is dead.
"""

from __future__ import annotations

import os
import re
import time
import threading
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

DOC_API = "https://api.gdeltproject.org/api/v2/doc/doc"
GEO_API = "https://api.gdeltproject.org/api/v2/geo/geo"

USER_AGENT = "web:xtag:2.0 (narrative-intelligence)"

# ── Tunables ──────────────────────────────────────────────────────────────────
# MIN_INTERVAL is the single most important number in this file. GDELT publishes
# no formal rate limit and the observed tolerance is low: during development this
# container was blocked for >15 minutes after roughly 20 requests, some of them
# issued back-to-back with no gap. 2.0s with 3 article windows means a fresh
# search costs ~7 GDELT calls / ~14s of throttle, which stayed safe in testing.
#
# Both are env-tunable. Raising ARTICLE_WINDOWS buys depth (250 more articles per
# window) at a directly proportional increase in block risk — if GDELT starts
# reporting rate-limited in /healthz, lower this first.
MIN_INTERVAL      = float(os.environ.get("GDELT_MIN_INTERVAL", "2.0"))
TIMESPAN_HOURS    = int(os.environ.get("GDELT_TIMESPAN_HOURS", "168"))   # 7d (was hardcoded 72h)
ARTICLE_WINDOWS   = int(os.environ.get("GDELT_ARTICLE_WINDOWS", "3"))    # time-slices for depth
MAX_RECORDS       = 250          # hard API cap per call — not configurable upstream
REQ_TIMEOUT       = int(os.environ.get("GDELT_TIMEOUT", "20"))
ANALYTICS_TTL     = int(os.environ.get("GDELT_ANALYTICS_TTL", "3600"))   # 1h — hourly data
BREAKER_THRESHOLD = int(os.environ.get("GDELT_BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN  = int(os.environ.get("GDELT_BREAKER_COOLDOWN", "180"))
ANALYTICS_BUDGET  = int(os.environ.get("GDELT_ANALYTICS_BUDGET", "45"))  # wall-clock seconds

# ── Rate limiter + circuit breaker state ──────────────────────────────────────
_lock = threading.Lock()
_last_call_at = 0.0
_consecutive_failures = 0
_breaker_open_until = 0.0

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


def _throttle() -> None:
    """Serialise GDELT calls process-wide with a minimum gap between them."""
    global _last_call_at
    with _lock:
        gap = time.time() - _last_call_at
        if gap < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - gap)
        _last_call_at = time.time()


def _breaker_is_open() -> bool:
    return time.time() < _breaker_open_until


def _record_failure(is_connection_error: bool) -> None:
    """
    Only connection-level failures trip the breaker. A 204 "no results" or a
    malformed-JSON response means GDELT is answering us fine and the query is
    simply empty — tripping the breaker on those would disable GDELT for every
    other query because one search found nothing.
    """
    global _consecutive_failures, _breaker_open_until
    if not is_connection_error:
        return
    with _lock:
        _consecutive_failures += 1
        if _consecutive_failures >= BREAKER_THRESHOLD:
            _breaker_open_until = time.time() + BREAKER_COOLDOWN
            log.warning(
                "GDELT circuit breaker OPEN for %ss after %d consecutive connection "
                "failures — almost certainly IP rate-limiting, backing off",
                BREAKER_COOLDOWN, _consecutive_failures)


def _record_success() -> None:
    global _consecutive_failures
    with _lock:
        _consecutive_failures = 0


def _cache_get(key: str, ttl: int):
    with _cache_lock:
        hit = _cache.get(key)
    if not hit:
        return None
    ts, val = hit
    if time.time() - ts > ttl:
        return None
    return val


def _cache_set(key: str, val) -> None:
    with _cache_lock:
        _cache[key] = (time.time(), val)
        if len(_cache) > 400:
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:120]:
                _cache.pop(k, None)


def _get(url: str, params: dict) -> tuple[object | None, str | None]:
    """
    Single choke point for every GDELT request. Returns (data, error).
    Never raises. A blocked or throttled GDELT yields (None, reason).
    """
    if _breaker_is_open():
        return None, "rate-limited (backing off)"
    _throttle()
    try:
        r = requests.get(url, params=params,
                         headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
    except (requests.exceptions.ConnectionError, requests.exceptions.SSLError) as e:
        # This is what GDELT rate-limiting actually looks like from the client.
        _record_failure(True)
        return None, f"connection reset (rate limit?): {str(e)[:80]}"
    except requests.exceptions.Timeout:
        _record_failure(True)
        return None, "timed out"
    except Exception as e:
        _record_failure(False)
        return None, str(e)[:110]

    if r.status_code == 204:
        _record_success()
        return None, None                      # legitimately zero results
    if r.status_code == 429:
        _record_failure(True)
        return None, "rate limited (429)"
    if r.status_code >= 400:
        _record_failure(False)
        return None, f"http {r.status_code}"

    _record_success()
    body = (r.text or "").strip()
    if not body:
        return None, None
    try:
        return r.json(), None
    except Exception:
        # GDELT occasionally returns an HTML error page with a 200. Treat as empty,
        # not as a connection failure — the endpoint is up, this query just failed.
        snippet = body[:60].replace("\n", " ")
        return None, f"non-JSON response: {snippet}"


# ── Query handling ────────────────────────────────────────────────────────────

def _build_query(q: str, lang: str | None = None, country: str | None = None) -> str:
    """
    GDELT treats bare multi-word input as AND across the words, which for an
    entity like "Islamic Resistance" pulls in every article containing both
    words anywhere. Quoting makes it a phrase match, which is what narrative
    tracking actually wants.
    """
    q = (q or "").strip().lstrip("#").strip()
    if not q:
        return ""
    if " " in q and not (q.startswith('"') and q.endswith('"')):
        q = f'"{q}"'
    parts = [q]
    if lang:
        parts.append(f"sourcelang:{lang}")
    if country:
        parts.append(f"sourcecountry:{country}")
    return " ".join(parts)


def _stamp(dt: datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S")


def _timespan_str(hours: int) -> str:
    """
    GDELT's timespan parameter takes a value plus a unit from {min,h,d,w,m}.
    Large hour counts are not reliably accepted — "168h" is not obviously valid
    where "7d" is — and an unaccepted timespan does not error usefully, it just
    returns nothing. Express the span in the largest clean unit that fits so the
    analytical modes do not silently come back empty.

    Days are preferred over weeks even where a week divides cleanly: "7d" was
    verified working against the live API, "1w" was not. Given the two are
    equivalent, ship the one that was actually observed to work.
    """
    hours = max(1, int(hours))
    if hours % 24 == 0:
        return f"{hours // 24}d"
    return f"{hours}h"


def _parse_ts(s: str | None) -> datetime | None:
    """GDELT timestamps look like 20260822T170000Z."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s).strip(), "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except Exception:
        pass
    try:
        return datetime.strptime(str(s).strip(), "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
    except Exception:
        return None


# ── Language + country normalisation ──────────────────────────────────────────
# GDELT returns human-readable names ("Russian", "Israel"), not codes. Previously
# the language name was passed straight through as if it were an ISO code, so a
# Turkish article was tagged language="Turkish" and every downstream lookup missed.

LANG_NAME_TO_CODE = {
    "english": "en", "arabic": "ar", "persian": "fa", "farsi": "fa",
    "hebrew": "he", "russian": "ru", "chinese": "zh", "urdu": "ur",
    "turkish": "tr", "spanish": "es", "french": "fr", "german": "de",
    "portuguese": "pt", "italian": "it", "dutch": "nl", "japanese": "ja",
    "korean": "ko", "hindi": "hi", "bengali": "bn", "indonesian": "id",
    "malay": "ms", "thai": "th", "vietnamese": "vi", "polish": "pl",
    "ukrainian": "uk", "greek": "el", "swedish": "sv", "norwegian": "no",
    "danish": "da", "finnish": "fi", "czech": "cs", "hungarian": "hu",
    "romanian": "ro", "bulgarian": "bg", "serbian": "sr", "croatian": "hr",
    "slovak": "sk", "slovenian": "sl", "albanian": "sq", "macedonian": "mk",
    "azerbaijani": "az", "armenian": "hy", "georgian": "ka", "kazakh": "kk",
    "uzbek": "uz", "pashto": "ps", "kurdish": "ku", "somali": "so",
    "swahili": "sw", "amharic": "am", "tamil": "ta", "telugu": "te",
    "marathi": "mr", "gujarati": "gu", "punjabi": "pa", "nepali": "ne",
    "sinhala": "si", "burmese": "my", "khmer": "km", "lao": "lo",
    "tagalog": "tl", "filipino": "tl", "catalan": "ca", "basque": "eu",
    "estonian": "et", "latvian": "lv", "lithuanian": "lt", "icelandic": "is",
    "irish": "ga", "welsh": "cy", "maltese": "mt", "afrikaans": "af",
    "hausa": "ha", "yoruba": "yo", "igbo": "ig", "zulu": "zu",
    "belarusian": "be", "bosnian": "bs", "mongolian": "mn", "tibetan": "bo",
    "kyrgyz": "ky", "tajik": "tg", "turkmen": "tk", "malayalam": "ml",
    "kannada": "kn", "odia": "or", "assamese": "as",
}

# Name -> ISO-3166-1 alpha-2, used to derive flag emoji programmatically rather
# than hand-maintaining 200 flag characters.
COUNTRY_NAME_TO_ISO = {
    "united states": "US", "united kingdom": "GB", "israel": "IL", "lebanon": "LB",
    "iran": "IR", "syria": "SY", "iraq": "IQ", "russia": "RU", "china": "CN",
    "turkey": "TR", "saudi arabia": "SA", "united arab emirates": "AE", "qatar": "QA",
    "egypt": "EG", "jordan": "JO", "yemen": "YE", "kuwait": "KW", "bahrain": "BH",
    "oman": "OM", "france": "FR", "germany": "DE", "italy": "IT", "spain": "ES",
    "netherlands": "NL", "belgium": "BE", "switzerland": "CH", "austria": "AT",
    "sweden": "SE", "norway": "NO", "denmark": "DK", "finland": "FI", "poland": "PL",
    "ukraine": "UA", "belarus": "BY", "czech republic": "CZ", "greece": "GR",
    "portugal": "PT", "ireland": "IE", "hungary": "HU", "romania": "RO",
    "bulgaria": "BG", "serbia": "RS", "croatia": "HR", "slovakia": "SK",
    "slovenia": "SI", "albania": "AL", "north macedonia": "MK", "bosnia and herzegovina": "BA",
    "cyprus": "CY", "malta": "MT", "iceland": "IS", "estonia": "EE", "latvia": "LV",
    "lithuania": "LT", "india": "IN", "pakistan": "PK", "afghanistan": "AF",
    "bangladesh": "BD", "sri lanka": "LK", "nepal": "NP", "japan": "JP",
    "south korea": "KR", "north korea": "KP", "taiwan": "TW", "hong kong": "HK",
    "singapore": "SG", "malaysia": "MY", "indonesia": "ID", "thailand": "TH",
    "vietnam": "VN", "philippines": "PH", "myanmar": "MM", "cambodia": "KH",
    "australia": "AU", "new zealand": "NZ", "canada": "CA", "mexico": "MX",
    "brazil": "BR", "argentina": "AR", "chile": "CL", "colombia": "CO",
    "peru": "PE", "venezuela": "VE", "cuba": "CU", "south africa": "ZA",
    "nigeria": "NG", "kenya": "KE", "ethiopia": "ET", "somalia": "SO",
    "sudan": "SD", "libya": "LY", "tunisia": "TN", "algeria": "DZ",
    "morocco": "MA", "ghana": "GH", "tanzania": "TZ", "uganda": "UG",
    "azerbaijan": "AZ", "armenia": "AM", "georgia": "GE", "kazakhstan": "KZ",
    "uzbekistan": "UZ", "turkmenistan": "TM", "kyrgyzstan": "KG", "tajikistan": "TJ",
    "mongolia": "MN", "palestine": "PS", "west bank": "PS", "gaza": "PS",
}


def lang_code(name: str | None) -> str:
    """'Russian' -> 'ru'. Unknown/blank falls back to 'en' as before."""
    if not name:
        return "en"
    n = str(name).strip().lower()
    if len(n) == 2 and n.isalpha():
        return n
    return LANG_NAME_TO_CODE.get(n, "en")


def country_flag(name: str | None) -> str:
    """Derive a flag emoji from a country name via its ISO-2 code."""
    if not name:
        return ""
    iso = COUNTRY_NAME_TO_ISO.get(str(name).strip().lower())
    if not iso or len(iso) != 2:
        return ""
    try:
        return chr(0x1F1E6 + ord(iso[0]) - 65) + chr(0x1F1E6 + ord(iso[1]) - 65)
    except Exception:
        return ""


# ── Articles (with time-window slicing for real depth) ────────────────────────

def articles(q: str, timespan_hours: int | None = None,
             windows: int | None = None, lang: str | None = None,
             country: str | None = None) -> tuple[list, str | None]:
    """
    Fetch articles, going deeper than the API's 250-record ceiling.

    The DOC 2.0 API caps maxrecords at 250 and — importantly — has NO offset or
    cursor parameter, so there is no conventional pagination. The only way to
    get more than 250 results is to slice the time range into consecutive
    windows and query each one separately. That is what `windows` does.

    Each extra window is another rate-limited request, so this trades directly
    against the block risk documented at the top of this file. Four windows over
    seven days yields up to 1000 articles for four requests, which is the
    balance point that held up in testing.
    """
    query = _build_query(q, lang=lang, country=country)
    if not query:
        return [], "empty query"

    hours = timespan_hours or TIMESPAN_HOURS
    n_windows = max(1, windows if windows is not None else ARTICLE_WINDOWS)

    now = datetime.now(timezone.utc)
    start_all = now - timedelta(hours=hours)
    step = (now - start_all) / n_windows

    seen_urls: set[str] = set()
    out: list = []
    errors: list[str] = []

    for i in range(n_windows):
        w_start = start_all + step * i
        w_end = start_all + step * (i + 1)
        params = {
            "query": query, "mode": "artlist", "maxrecords": MAX_RECORDS,
            "format": "json", "sort": "datedesc",
            "startdatetime": _stamp(w_start), "enddatetime": _stamp(w_end),
        }
        data, err = _get(DOC_API, params)
        if err:
            errors.append(err)
            # A tripped breaker means every later window will fail too — stop
            # rather than burning the remaining windows against a blocked IP.
            if "rate" in err.lower() or "reset" in err.lower():
                break
            continue
        if not isinstance(data, dict):
            continue
        for art in (data.get("articles") or []):
            url = (art.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(art)

    err = None
    if not out and errors:
        err = errors[0]
    return out, err


# ── Timeline / breakdown modes ────────────────────────────────────────────────

def _timeline(mode: str, q: str, timespan_hours: int | None = None) -> tuple[list, str | None]:
    """Return the raw `timeline` series list for a timeline-family mode."""
    query = _build_query(q)
    if not query:
        return [], "empty query"
    hours = timespan_hours or TIMESPAN_HOURS
    params = {"query": query, "mode": mode, "format": "json",
              "timespan": _timespan_str(hours)}
    data, err = _get(DOC_API, params)
    if err or not isinstance(data, dict):
        return [], err
    return (data.get("timeline") or []), None


def tone_timeline(q: str, timespan_hours: int | None = None) -> dict:
    """
    Average tone over time. GDELT tone runs roughly -10 (extremely negative) to
    +10 (extremely positive); most news sits between -5 and +2.

    Zero values are dropped rather than averaged in: GDELT emits 0 for hours with
    no coverage, and treating "no articles" as "perfectly neutral tone" would
    pull every average toward zero and wash out exactly the swings we care about.
    """
    series, err = _timeline("timelinetone", q, timespan_hours)
    if err or not series:
        return {"points": [], "average": None, "trend": None,
                "min": None, "max": None, "error": err}
    data = series[0].get("data") or []
    points = []
    for pt in data:
        ts = _parse_ts(pt.get("date"))
        try:
            val = float(pt.get("value"))
        except Exception:
            continue
        if ts is None:
            continue
        points.append({"ts": ts.isoformat(), "value": round(val, 3)})

    nz = [p["value"] for p in points if p["value"] != 0]
    if not nz:
        return {"points": points, "average": None, "trend": None,
                "min": None, "max": None, "error": None}

    avg = sum(nz) / len(nz)
    # Trend = second half vs first half of the non-empty observations.
    half = max(1, len(nz) // 2)
    early = sum(nz[:half]) / half
    late = sum(nz[-half:]) / half
    delta = late - early
    trend = "worsening" if delta < -0.5 else "improving" if delta > 0.5 else "stable"

    return {"points": points, "average": round(avg, 3), "trend": trend,
            "delta": round(delta, 3), "min": round(min(nz), 3),
            "max": round(max(nz), 3), "observations": len(nz), "error": None}


def volume_timeline(q: str, timespan_hours: int | None = None) -> dict:
    """Raw article counts per hour, plus peak detection."""
    series, err = _timeline("timelinevolraw", q, timespan_hours)
    if err or not series:
        return {"points": [], "total": 0, "peak": None, "error": err}
    data = series[0].get("data") or []
    points = []
    for pt in data:
        ts = _parse_ts(pt.get("date"))
        try:
            val = float(pt.get("value"))
        except Exception:
            continue
        if ts is None:
            continue
        points.append({"ts": ts.isoformat(), "value": val})

    if not points:
        return {"points": [], "total": 0, "peak": None, "error": None}

    total = sum(p["value"] for p in points)
    peak = max(points, key=lambda p: p["value"])
    vals = [p["value"] for p in points]
    mean = total / len(vals)
    # Sample standard deviation, used to express the peak in sigma above mean —
    # "the busiest hour was 4.2 sigma above baseline" is a far more useful
    # surge signal than a bare article count that means nothing without context.
    if len(vals) > 1:
        var = sum((v - mean) ** 2 for v in vals) / (len(vals) - 1)
        sd = var ** 0.5
    else:
        sd = 0.0
    spike_sigma = round((peak["value"] - mean) / sd, 2) if sd > 0 else None

    return {"points": points, "total": int(total), "mean_per_hour": round(mean, 2),
            "peak": {"ts": peak["ts"], "value": int(peak["value"])},
            "spike_sigma": spike_sigma, "error": None}


def _breakdown(mode: str, q: str, suffix: str,
               timespan_hours: int | None = None) -> tuple[list, str | None]:
    """
    Shared parser for the per-category timeline modes. These return one series
    per category ("India Volume Intensity", "Arabic Volume Intensity", ...),
    each a full time series. Summing each series gives the totals we want.
    """
    series, err = _timeline(mode, q, timespan_hours)
    if err or not series:
        return [], err
    totals: list[tuple[str, float]] = []
    for s in series:
        name = str(s.get("series") or "").strip()
        if suffix and name.lower().endswith(suffix.lower()):
            name = name[: -len(suffix)].strip()
        total = 0.0
        for pt in (s.get("data") or []):
            try:
                total += float(pt.get("value") or 0)
            except Exception:
                continue
        if total > 0 and name:
            totals.append((name, total))
    totals.sort(key=lambda kv: kv[1], reverse=True)
    return totals, None


def country_breakdown(q: str, timespan_hours: int | None = None) -> dict:
    """Which countries' media are carrying this narrative — geographic intelligence."""
    totals, err = _breakdown("timelinesourcecountry", q, "Volume Intensity", timespan_hours)
    if err or not totals:
        return {"countries": [], "total_countries": 0, "concentration": None, "error": err}
    grand = sum(v for _, v in totals) or 1.0
    countries = [{
        "country": name,
        "flag": country_flag(name),
        "volume": round(v, 4),
        "share": round(v / grand * 100, 2),
    } for name, v in totals[:40]]

    # Concentration: share held by the top 3 carriers. A narrative confined to
    # three national medias is a domestic story; one spread across thirty is an
    # international information campaign. This single number separates them.
    top3 = sum(c["share"] for c in countries[:3])
    concentration = ("concentrated" if top3 > 70 else
                     "regional" if top3 > 45 else "diffuse")
    return {"countries": countries, "total_countries": len(totals),
            "top3_share": round(top3, 1), "concentration": concentration, "error": None}


def language_breakdown(q: str, timespan_hours: int | None = None) -> dict:
    """
    Which languages the narrative is running in — the best available proxy for
    which audience it is aimed at.
    """
    totals, err = _breakdown("timelinelang", q, "Volume Intensity", timespan_hours)
    if err or not totals:
        return {"languages": [], "total_languages": 0, "error": err}
    grand = sum(v for _, v in totals) or 1.0
    langs = [{
        "language": name,
        "code": lang_code(name),
        "volume": round(v, 4),
        "share": round(v / grand * 100, 2),
    } for name, v in totals[:30]]
    non_en = round(sum(l["share"] for l in langs if l["code"] != "en"), 1)
    return {"languages": langs, "total_languages": len(totals),
            "non_english_share": non_en, "error": None}


def geo_points(q: str, timespan_hours: int | None = None, limit: int = 300) -> dict:
    """
    Geolocated mentions from the GEO 2.0 API — where in the world this narrative
    is physically being talked about, as mappable coordinates.
    """
    query = _build_query(q)
    if not query:
        return {"points": [], "error": "empty query"}
    hours = timespan_hours or TIMESPAN_HOURS
    params = {"query": query, "mode": "PointData", "format": "geojson",
              "timespan": _timespan_str(hours)}
    data, err = _get(GEO_API, params)
    if err or not isinstance(data, dict):
        return {"points": [], "error": err}
    pts = []
    for feat in (data.get("features") or [])[:limit]:
        geom = feat.get("geometry") or {}
        props = feat.get("properties") or {}
        coords = geom.get("coordinates") or []
        if len(coords) < 2:
            continue
        try:
            lon, lat = float(coords[0]), float(coords[1])
        except Exception:
            continue
        count = props.get("count")
        try:
            count = int(count)
        except Exception:
            count = 1
        pts.append({"lat": lat, "lon": lon,
                    "name": str(props.get("name") or "").strip(),
                    "count": count})
    pts.sort(key=lambda p: p["count"], reverse=True)
    return {"points": pts, "total": len(pts), "error": None}


# ── Combined analytical snapshot ──────────────────────────────────────────────

def snapshot(q: str, timespan_hours: int | None = None,
             budget: int | None = None) -> dict:
    """
    Run the analytical modes for a query and return one combined intelligence
    block. Cached hard (ANALYTICS_TTL) because these are hourly-resolution
    series — refetching per search would spend the rate limit on identical data.

    Runs SEQUENTIALLY on purpose. Firing four GDELT calls in parallel is the
    fastest possible way to trip the rate limiter, which then costs far more
    than the few seconds saved. A wall-clock budget stops the whole block from
    eating the search timeout when GDELT is slow.
    """
    hours = timespan_hours or TIMESPAN_HOURS
    key = f"snap:{(q or '').lower()}:{hours}"
    cached = _cache_get(key, ANALYTICS_TTL)
    if cached is not None:
        return {**cached, "cached": True}

    deadline = time.time() + (budget or ANALYTICS_BUDGET)
    out: dict = {"tone": {}, "volume": {}, "geography": {},
                 "audience": {}, "cached": False, "degraded": []}

    def _stage(name: str, fn):
        if time.time() > deadline:
            out["degraded"].append(f"{name}: skipped (budget exhausted)")
            return {}
        if _breaker_is_open():
            out["degraded"].append(f"{name}: skipped (rate-limit backoff)")
            return {}
        try:
            res = fn()
            if isinstance(res, dict) and res.get("error"):
                out["degraded"].append(f"{name}: {res['error']}")
            return res
        except Exception as e:
            log.warning("GDELT %s stage failed: %s", name, e)
            out["degraded"].append(f"{name}: {str(e)[:80]}")
            return {}

    out["tone"] = _stage("tone", lambda: tone_timeline(q, hours))
    out["volume"] = _stage("volume", lambda: volume_timeline(q, hours))
    out["geography"] = _stage("geography", lambda: country_breakdown(q, hours))
    out["audience"] = _stage("audience", lambda: language_breakdown(q, hours))

    # Only cache a result that actually carries signal. Caching a fully degraded
    # snapshot would pin the "GDELT is blocked" state in place for an hour after
    # the block had already lifted.
    if out["tone"].get("points") or out["volume"].get("points") or \
       out["geography"].get("countries"):
        _cache_set(key, out)
    return out


# ── Health ────────────────────────────────────────────────────────────────────

def health() -> dict:
    """
    Real capability probe, not a config check. GDELT needs no API key, so
    "configured" is meaningless — the only question that matters is whether this
    IP can currently reach it, which the circuit breaker state answers directly.
    """
    if _breaker_is_open():
        return {"reachable": False, "working": False,
                "reason": (f"rate-limited; backing off for "
                           f"{int(_breaker_open_until - time.time())}s more"),
                "breaker_open": True,
                "consecutive_failures": _consecutive_failures}

    cached = _cache_get("health", 120)
    if cached is not None:
        return cached

    data, err = _get(DOC_API, {"query": "news", "mode": "artlist",
                               "maxrecords": 1, "format": "json", "timespan": "24h"})
    if err:
        res = {"reachable": False, "working": False, "reason": err,
               "breaker_open": _breaker_is_open(),
               "consecutive_failures": _consecutive_failures}
    else:
        got = isinstance(data, dict) and bool(data.get("articles"))
        res = {"reachable": True, "working": True,
               "reason": None if got else "reachable but probe returned no articles",
               "breaker_open": False, "consecutive_failures": 0}
    _cache_set("health", res)
    return res


def stats() -> dict:
    """Introspection for /healthz — what the limiter is currently doing."""
    return {
        "min_interval_s": MIN_INTERVAL,
        "timespan_hours": TIMESPAN_HOURS,
        "article_windows": ARTICLE_WINDOWS,
        "max_records_per_window": MAX_RECORDS,
        "theoretical_max_articles": ARTICLE_WINDOWS * MAX_RECORDS,
        "breaker_open": _breaker_is_open(),
        "consecutive_failures": _consecutive_failures,
        "cache_entries": len(_cache),
    }
