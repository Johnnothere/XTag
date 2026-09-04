"""
GDELT backbone — the primary news-narrative source for XTag.

WHY THIS IS ITS OWN MODULE
GDELT monitors broadcast, print and web news from nearly every country in 100+
languages and machine-translates 65 of them into English in near real time. For
a platform whose whole purpose is cross-language narrative tracking, that is the
centre of gravity, not one source among fifteen. Using it properly means several
different API modes, a shared rate limiter, a circuit breaker and its own cache —
none of which belongs inline in app.py.

HARD-WON OPERATIONAL FACTS — READ BEFORE CHANGING ANYTHING HERE
These were established empirically against the live API. An earlier version of
this file asserted that GDELT "rate-limits by IP and does not return 429". That
was WRONG, and the wrong diagnosis produced a wrong design. What is actually true:

1. GDELT DOES return HTTP 429, and those 429s are STOCHASTIC AND RETRYABLE.
   The identical request, same User-Agent, seconds apart, returns 429 then 200.
   Measured: 4/4 analytical modes failed intermittently without retry and
   succeeded 4/4 with at most one retry. Retrying a 429 is therefore the single
   most important behaviour in this module — far more important than throttling.
   A circuit breaker that gives up for minutes on a 429 turns a 3-second hiccup
   into a total outage, which is exactly what the previous version did.

2. "Connection reset" is NOT GDELT rate-limiting. It is a TRANSPORT problem.
   In a sandboxed environment whose egress proxy intercepts TLS, HTTPS to
   api.gdeltproject.org is reset at handshake time while the very same request
   over HTTP returns 200 with correct data. Verified: openssl showed a
   MITM-issued certificate for *.gdeltproject.org, http:// worked, https:// did
   not, and data.gdeltproject.org over HTTPS worked fine. Treating that as a
   rate limit is a misdiagnosis that makes the module back off from a problem
   backing off cannot fix. Hence the HTTP fallback below.

3. The GEO 2.0 API is dead upstream. GDELT's own documented example
   (api/v2/geo/geo?query=trump) returns 404, as does every documented parameter
   combination. It is not called from here any more; it was removed rather than
   left in to fail on every search.

The consequences shape this whole module:
  * 429 is retried with backoff and NEVER trips the breaker — it is transient
  * connection-level failure falls back from HTTPS to HTTP, then trips the
    breaker only if both transports fail repeatedly
  * every public function degrades to an empty result and never raises, so a
    struggling GDELT thins the intelligence picture but never 500s a search
  * analytical modes are cached hard — they are hourly-resolution data, so
    re-fetching them per search would spend request budget for no new information

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

NOTE ON artlist FIELDS: artlist does NOT return a tone field. Tone only comes
from the timeline/tonechart modes. Any code that reads article["tone"] is dead.
"""

from __future__ import annotations

import os
import re
import time
import threading
import copy
import logging
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger(__name__)

# Paths are scheme-less on purpose: the working transport is chosen at runtime
# by _request(), which falls back HTTPS -> HTTP when TLS is intercepted.
DOC_PATH = "api.gdeltproject.org/api/v2/doc/doc"

USER_AGENT = "web:xtag:2.0 (narrative-intelligence)"

# ── Tunables ──────────────────────────────────────────────────────────────────
# RETRY_ATTEMPTS is the single most important number in this file. GDELT's 429s
# are stochastic: measured against the live API, 2 of 4 analytical modes returned
# 429 on the first try and 200 on the second, with no change to the request. One
# retry recovered 4/4. Throttling alone does NOT avoid these — the same request
# spaced 8s apart still drew a 429 — so retry is the mechanism that matters and
# MIN_INTERVAL is only politeness.
# 4, not 3: the timelinesourcecountry response is by far the heaviest (~400KB)
# and is throttled the most often. At 3 attempts it still lost the geography
# stage on a warm run, and geography is the costliest stage to lose — it feeds
# both the audience panel and the geographic_breadth threat factor.
RETRY_ATTEMPTS    = int(os.environ.get("GDELT_RETRY_ATTEMPTS", "4"))
RETRY_BACKOFF     = float(os.environ.get("GDELT_RETRY_BACKOFF", "3.0"))  # seconds, linear

# HTTPS is always tried first. Some environments (notably sandboxes whose egress
# proxy intercepts TLS) get the connection reset at handshake time for this host
# while plain HTTP works and returns identical data. Set GDELT_ALLOW_HTTP=0 to
# forbid the downgrade if your threat model requires it — GDELT is public,
# unauthenticated, read-only data and carries no credentials, but responses over
# HTTP are tamperable in principle, which for an intelligence platform is a real
# (if remote) concern. Default is on, because no GDELT at all is the worse
# failure for this application.
ALLOW_HTTP        = os.environ.get("GDELT_ALLOW_HTTP", "1").lower() not in ("0", "false", "no")

MIN_INTERVAL      = float(os.environ.get("GDELT_MIN_INTERVAL", "2.0"))
TIMESPAN_HOURS    = int(os.environ.get("GDELT_TIMESPAN_HOURS", "168"))   # 7d (was hardcoded 72h)
ARTICLE_WINDOWS   = int(os.environ.get("GDELT_ARTICLE_WINDOWS", "3"))    # time-slices for depth
MAX_RECORDS       = 250          # hard API cap per call — not configurable upstream
# 20s was far too generous and turned a flaky transport into a budget killer:
# a slow HTTPS attempt that eventually 429s costs 20s, and three of those exceed
# the whole analytics budget before a single usable stage completes. Healthy
# GDELT answers in ~5s over HTTP, so 12s is generous while capping the damage.
REQ_TIMEOUT       = int(os.environ.get("GDELT_TIMEOUT", "12"))
ANALYTICS_TTL     = int(os.environ.get("GDELT_ANALYTICS_TTL", "3600"))   # 1h — hourly data
BREAKER_THRESHOLD = int(os.environ.get("GDELT_BREAKER_THRESHOLD", "3"))
BREAKER_COOLDOWN  = int(os.environ.get("GDELT_BREAKER_COOLDOWN", "180"))
# The FIRST call in a process may pay one-off transport discovery (a slow HTTPS
# attempt, a reset, then the HTTP retry — measured at ~39s in a TLS-intercepting
# sandbox). At 45s that single discovery starved the remaining three stages and
# geography/audience never ran at all. 110s absorbs discovery and still leaves
# room for four ~5s stages; once the transport is known-good a full snapshot is
# ~20s. Search pool allows 240s and gunicorn 300s, so this stays well inside.
ANALYTICS_BUDGET  = int(os.environ.get("GDELT_ANALYTICS_BUDGET", "110"))  # wall-clock seconds

# ── Rate limiter + circuit breaker state ──────────────────────────────────────
_lock = threading.Lock()
_last_call_at = 0.0
_consecutive_failures = 0
_breaker_open_until = 0.0
# Learned at runtime: "https" until a connection-level failure proves otherwise,
# then "http" for the rest of the process. Sticky so we pay the failed-TLS
# penalty once rather than on every single call.
_scheme = "https"

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


class GdeltError(str):
    """An error message that also carries a machine-readable `kind`.

    A13: articles() decided whether to abort its remaining windows by substring-
    matching "rate" or "reset" in the message. The breaker's own message is
    "backing off after repeated connection failures", which contains neither, so
    a tripped breaker never actually stopped the loop and every remaining window
    was spent hammering a known-dead endpoint. Subclassing str keeps every
    existing consumer — logging, f-strings, JSON serialisation, truthiness —
    working unchanged, while giving callers something reliable to branch on.

    Kinds: breaker | rate_limit | transport | timeout | http | bad_response |
           partial | other
    """

    def __new__(cls, message: str, kind: str = "other"):
        obj = super().__new__(cls, message)
        obj.kind = kind
        return obj


def error_kind(err) -> str:
    """Kind of an error returned by this module; "other" for a plain string."""
    return getattr(err, "kind", "other") if err else ""


class ArticleSet(list):
    """A list of articles that also says how much of the corpus it actually is.

    A14: completeness was only ever expressed as an ERROR. A caller therefore
    learned "this corpus is whole" by the ABSENCE of something — and every
    caller that reasonably wrote `arts, err = articles(...)` then only looked at
    `err` when `arts` was empty (app.py did exactly that until A11) scored a
    one-window-of-three corpus as the whole picture. Absence of an error is a
    terrible carrier for a fact this load-bearing: it survives no assignment, no
    log line and no serialisation, and there is no way to ask a bare list
    whether it is everything.

    So the answer travels with the data. Subclassing list is the same trick
    GdeltError plays on str: every existing consumer — iteration, len(),
    truthiness, json.dumps, slicing — behaves identically, while a caller that
    wants the truth can read `.coverage` (or call gdelt.coverage(arts)) and get
    an explicit `complete: True/False` instead of inferring it.
    """

    def __init__(self, items=(), coverage: dict | None = None):
        super().__init__(items)
        self.coverage = coverage or _coverage()


def _coverage(windows_total: int = 0, windows_ok: int = 0, windows_failed: int = 0,
              windows_abandoned: int = 0, windows_truncated: int = 0,
              articles: int = 0, reason: str | None = None) -> dict:
    """Build the coverage record shared by ArticleSet and its error.

    `complete` is the only field most callers need: True means every window was
    fetched, none was abandoned, and none came back against the API's record
    cap — i.e. this really is everything GDELT has for the query and range.
    """
    complete = not (windows_failed or windows_abandoned or windows_truncated)
    return {
        "complete": bool(complete and windows_total > 0),
        "partial": bool(articles and not complete),
        "windows_total": windows_total,
        "windows_ok": windows_ok,
        "windows_failed": windows_failed,
        "windows_abandoned": windows_abandoned,
        "windows_truncated": windows_truncated,
        "articles": articles,
        "reason": reason,
    }


def coverage(result) -> dict:
    """Coverage of an articles() result — pass either the list or the error.

    Mirrors error_kind(): tolerates anything, so a caller never has to guard the
    lookup. A plain list from some older path reports complete=False with
    unknown window counts rather than claiming completeness it cannot vouch for.
    """
    cov = getattr(result, "coverage", None)
    return dict(cov) if isinstance(cov, dict) else _coverage()


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
    Only connection-level failures trip the breaker. A 204 "no results", a
    malformed-JSON response or an HTTP 429 all mean GDELT is answering us fine —
    tripping the breaker on those would disable GDELT for every other query
    because one query found nothing or hit a transient throttle. 429 in
    particular is handled by retry in _get() and must never reach here.
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
                "failures on both transports — GDELT unreachable, backing off",
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


def _record_transport_failure() -> bool:
    """
    Flip the process-wide scheme from HTTPS to HTTP, once. Returns True if this
    call is the one that switched it.

    A connection-level failure on HTTPS is very often TLS interception rather
    than anything wrong at GDELT. Flip to HTTP once and stay there — verified
    that the identical request succeeds over HTTP in exactly that situation.

    A12: this must only be called on EVIDENCE of interception — a connection
    reset or an SSL error. It used to be called on a plain read timeout too,
    which meant one slow GDELT response permanently downgraded every request for
    the life of the process, sending all subsequent traffic in clear text for no
    reason. A timeout says the server is slow; it says nothing about the
    transport. The return value used to be annotated `-> None` while the body
    returned bool, and no caller looked at it.
    """
    global _scheme
    if _scheme == "https" and ALLOW_HTTP:
        with _lock:
            if _scheme == "https":
                _scheme = "http"
                log.warning(
                    "GDELT: HTTPS failed at connection level; falling back to HTTP "
                    "for the rest of this process. This usually means an egress "
                    "proxy is intercepting TLS to api.gdeltproject.org.")
        return True
    return False


def _get(path: str, params: dict) -> tuple[object | None, str | None]:
    """
    Single choke point for every GDELT request. Returns (data, error).
    Never raises.

    Two failure modes are handled, and telling them apart is the whole point:

      429  -> TRANSIENT AND RETRYABLE. Retried up to RETRY_ATTEMPTS with a
              linear backoff. Never trips the breaker. Measured against the live
              API, one retry took the analytical modes from 2/4 to 4/4.

      connection reset / SSL error -> TRANSPORT problem, not a rate limit.
              Falls back HTTPS -> HTTP once (see _record_transport_failure) and
              retries immediately on the new transport. Only if that also fails
              does it count toward the breaker.
    """
    if _breaker_is_open():
        return None, GdeltError("backing off after repeated connection failures",
                                "breaker")

    last_err = None
    conn_failed = False
    for attempt in range(max(1, RETRY_ATTEMPTS)):
        _throttle()
        url = f"{_scheme}://{path}"
        try:
            r = requests.get(url, params=params,
                             headers={"User-Agent": USER_AGENT}, timeout=REQ_TIMEOUT)
        except (requests.exceptions.ConnectionError, requests.exceptions.SSLError) as e:
            last_err = GdeltError(f"connection failed ({_scheme}): {str(e)[:70]}",
                                  "transport")
            conn_failed = True
            # Reset / SSL error IS evidence of interception — flip transport and
            # retry immediately on the new one.
            if _record_transport_failure():
                log.info("GDELT: retrying immediately over %s", _scheme)
            continue
        except requests.exceptions.Timeout:
            # A12: deliberately NOT a transport downgrade. A read timeout means
            # GDELT was slow, not that TLS is being intercepted; downgrading on
            # it sent the whole process to plaintext for the rest of its life
            # after a single slow response. Retry on the transport we have.
            last_err = GdeltError(f"timed out after {REQ_TIMEOUT}s", "timeout")
            conn_failed = True
            continue
        except Exception as e:
            _record_failure(False)
            return None, GdeltError(str(e)[:110], "other")

        if r.status_code == 429:
            # Stochastic and retryable — explicitly NOT a breaker condition.
            last_err = GdeltError("rate limited (429)", "rate_limit")
            retry_after = r.headers.get("Retry-After")
            try:
                wait = min(float(retry_after), 30.0) if retry_after else RETRY_BACKOFF * (attempt + 1)
            except (TypeError, ValueError):
                wait = RETRY_BACKOFF * (attempt + 1)
            if attempt < RETRY_ATTEMPTS - 1:
                time.sleep(wait)
            continue

        if r.status_code == 204:
            _record_success()
            return None, None                  # legitimately zero results
        if r.status_code >= 400:
            _record_failure(False)
            return None, GdeltError(f"http {r.status_code}", "http")

        _record_success()
        body = (r.text or "").strip()
        if not body:
            return None, None
        try:
            return r.json(), None
        except Exception:
            # GDELT occasionally returns an HTML error page with a 200. Treat as
            # empty, not a connection failure — the endpoint is up, this query
            # just failed.
            snippet = body[:60].replace("\n", " ")
            return None, GdeltError(f"non-JSON response: {snippet}", "bad_response")

    # Breaker accounting happens ONCE per call, not once per attempt. Counting
    # per attempt let a single _get with 4 retries trip a 3-strike breaker by
    # itself, which then cascaded and skipped every later query — a self-inflicted
    # outage far worse than the transient fault it was reacting to.
    if conn_failed:
        _record_failure(True)
    return None, last_err or GdeltError("request failed", "other")


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
    # Quoting was applied only to multi-word input, so a fused token like
    # "covid1948" went through as bare full-text input and GDELT matched it
    # loosely — which is how a hashtag search returned articles about 1948.
    # A single token with an alpha/digit transition is a phrase too: it is one
    # name, and the API must be asked for it exactly.
    already_quoted = q.startswith('"') and q.endswith('"')
    fused = bool(re.search(r"[A-Za-z][0-9]|[0-9][A-Za-z]", q)) and " " not in q
    if (" " in q or fused) and not already_quoted:
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

    RETURNS (ArticleSet, error). The list carries `.coverage` — read it, or call
    gdelt.coverage(result), to tell "this is everything" from "this is what we
    could get". `coverage["complete"]` is True only when every window was
    fetched and none hit the 250-record cap; the error is non-None whenever it
    is False and something still came back (kind "partial").
    """
    query = _build_query(q, lang=lang, country=country)
    if not query:
        return ArticleSet([], _coverage(reason="empty query")), "empty query"

    hours = timespan_hours or TIMESPAN_HOURS
    n_windows = max(1, windows if windows is not None else ARTICLE_WINDOWS)

    now = datetime.now(timezone.utc)
    start_all = now - timedelta(hours=hours)
    step = (now - start_all) / n_windows

    seen_urls: set[str] = set()
    out: list = []
    errors: list = []
    truncated_windows = 0
    ok_windows = 0
    attempted = 0
    abandoned_windows = 0

    for i in range(n_windows):
        w_start = start_all + step * i
        w_end = start_all + step * (i + 1)
        params = {
            "query": query, "mode": "artlist", "maxrecords": MAX_RECORDS,
            "format": "json", "sort": "datedesc",
            "startdatetime": _stamp(w_start), "enddatetime": _stamp(w_end),
        }
        attempted += 1
        data, err = _get(DOC_PATH, params)
        if err:
            errors.append(err)
            # A13: a tripped breaker or an exhausted rate limit means every later
            # window will fail too — stop rather than burning the remaining
            # windows against a blocked IP. This used to substring-match the
            # message and silently never fire; it now tests the error's kind.
            if error_kind(err) in ("breaker", "rate_limit"):
                # A14: the abandoning was right, the accounting was not. The
                # windows we never tried were counted neither as fetched nor as
                # failed, so a breaker trip on window 1 of 3 reported "1 of 3
                # window(s) failed" — a caller reading windows_failed/
                # windows_total saw 67% coverage of a corpus it had 33% of, and
                # the two thirds of the time range that were never queried at
                # all looked like time ranges with no news in them.
                abandoned_windows = n_windows - attempted
                break
            continue
        if data is not None and not isinstance(data, dict):
            # A14: a response of an unexpected shape used to `continue`
            # silently — no article, no error, nothing counted. The window
            # simply evaporated and the run was reported as whole. (data is
            # None on a legitimate 204/empty body, which really is an empty
            # window and is counted as fetched below.)
            errors.append(GdeltError(
                f"unexpected response shape ({type(data).__name__}) for window "
                f"{i + 1}/{n_windows}", "bad_response"))
            continue
        ok_windows += 1
        window_arts = (data or {}).get("articles") or []
        # A11: MAX_RECORDS is the API's hard per-call ceiling with no cursor to
        # page past it. A window that comes back exactly full almost certainly
        # had more behind it, so the corpus for that slice is truncated and the
        # caller needs to know before treating counts as complete.
        if len(window_arts) >= MAX_RECORDS:
            truncated_windows += 1
        for art in window_arts:
            url = (art.get("url") or "").strip()
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            out.append(art)

    # A11: this used to report err=None whenever ANY articles came back, so a
    # run where 2 of 3 windows failed was indistinguishable from a complete one
    # and downstream code scored a partial corpus as the whole picture. Partial
    # failure and API-cap truncation are now always reported; the caller decides
    # what to do with a partial result, but it is never told the corpus is whole
    # when it is not.
    notes = []
    if errors:
        notes.append(f"{len(errors)} of {n_windows} time window(s) failed "
                     f"({errors[0]})")
    if abandoned_windows:
        notes.append(f"{abandoned_windows} window(s) never attempted — aborted "
                     f"after a {error_kind(errors[-1])} failure")
    if truncated_windows:
        notes.append(f"{truncated_windows} window(s) hit the {MAX_RECORDS}-record "
                     f"API cap — those slices are truncated")

    cov = _coverage(windows_total=n_windows, windows_ok=ok_windows,
                    windows_failed=len(errors), windows_abandoned=abandoned_windows,
                    windows_truncated=truncated_windows, articles=len(out),
                    reason="; ".join(notes) or None)
    result = ArticleSet(out, cov)

    err = None
    if notes:
        err = GdeltError("; ".join(notes), "partial" if out else "failed")
        # Flat attributes are the shape app.py already reads (getattr with a
        # None default), so they stay. `coverage` is the same record the list
        # carries, so a caller can branch on whichever of the two it happens to
        # be holding.
        err.windows_total = n_windows
        err.windows_failed = len(errors)
        err.windows_truncated = truncated_windows
        err.windows_abandoned = abandoned_windows
        err.coverage = cov
    return result, err


# ── Timeline / breakdown modes ────────────────────────────────────────────────

def _timeline(mode: str, q: str, timespan_hours: int | None = None) -> tuple[list, str | None]:
    """Return the raw `timeline` series list for a timeline-family mode."""
    query = _build_query(q)
    if not query:
        return [], "empty query"
    hours = timespan_hours or TIMESPAN_HOURS
    params = {"query": query, "mode": mode, "format": "json",
              "timespan": _timespan_str(hours)}
    data, err = _get(DOC_PATH, params)
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
        # E6: {**cached} is a SHALLOW copy — every nested list and dict is still
        # the cache's own object, so a caller that sorts, truncates or annotates
        # snapshot["geography"]["countries"] corrupts the entry for the next hour
        # of requests. Hand out a private copy.
        out = copy.deepcopy(cached)
        out["cached"] = True
        return out

    deadline = time.time() + (budget or ANALYTICS_BUDGET)
    # timespan_hours is echoed so consumers can fingerprint what a score was
    # computed over (intel.E5) without having to guess this module's defaults.
    out: dict = {"tone": {}, "volume": {}, "geography": {},
                 "audience": {}, "cached": False, "degraded": [],
                 "timespan_hours": hours}

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

    # Order is by value-if-lost, not by cost. Geography feeds both the audience
    # layer and the geographic_breadth threat factor, so it runs before volume:
    # if the budget runs out, losing volume costs one factor, losing geography
    # costs a factor AND the whole geographic intelligence panel.
    out["tone"] = _stage("tone", lambda: tone_timeline(q, hours))
    out["geography"] = _stage("geography", lambda: country_breakdown(q, hours))
    out["audience"] = _stage("audience", lambda: language_breakdown(q, hours))
    out["volume"] = _stage("volume", lambda: volume_timeline(q, hours))

    # A14, same defect as articles(): `degraded` was the only tell that this
    # block was assembled from fewer than four stages, and it is a list of
    # prose. A consumer had to know that an empty list means "whole" and infer
    # completeness from the truthiness of a message list — so a snapshot whose
    # geography stage was skipped for budget rendered exactly like one where
    # GDELT genuinely reported no countries, and the threat factors computed
    # from it carried the confidence of a full picture. State it outright, in
    # the same vocabulary the ArticleSet coverage record uses.
    stage_names = ("tone", "geography", "audience", "volume")
    out["stages_total"] = len(stage_names)
    out["stages_ok"] = sum(
        1 for k in stage_names
        if isinstance(out.get(k), dict) and out[k] and not out[k].get("error"))
    out["complete"] = not out["degraded"]
    out["partial"] = bool(out["degraded"]) and out["stages_ok"] > 0

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

    data, err = _get(DOC_PATH, {"query": "news", "mode": "artlist",
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
        "transport": _scheme,
        "http_fallback_allowed": ALLOW_HTTP,
        "retry_attempts": RETRY_ATTEMPTS,
        "min_interval_s": MIN_INTERVAL,
        "timespan_hours": TIMESPAN_HOURS,
        "article_windows": ARTICLE_WINDOWS,
        "max_records_per_window": MAX_RECORDS,
        "theoretical_max_articles": ARTICLE_WINDOWS * MAX_RECORDS,
        "breaker_open": _breaker_is_open(),
        "consecutive_failures": _consecutive_failures,
        "cache_entries": len(_cache),
    }
