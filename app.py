"""XTag — Narrative Intelligence Platform v2.

Phase 0: Unified document schema, source normalisation, in-memory ingestion store.
Phase 1: Expanded sources — GDELT, adversary/state media RSS, academic (OpenAlex/arXiv),
         podcast watchlist (Podcast Index), plus all original social platforms.
Phase 2: Narrative engine — claim extraction, framing analysis, entity graph, velocity
         tracking, coordination detection, cross-platform propagation.

Backend: Flask + Gunicorn on Railway.
"""
from __future__ import annotations

import base64
import html
import json
import hashlib
import os
import re
import subprocess
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
from urllib.parse import quote_plus, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, make_response

app = Flask(__name__)

# ── API keys ──────────────────────────────────────────────────────────────────
YOUTUBE_API_KEY      = os.environ.get("YOUTUBE_API_KEY", "").strip()
SERPAPI_KEY          = os.environ.get("SERPAPI_KEY", "").strip()
SCRAPEBADGER_KEY     = os.environ.get("SCRAPEBADGER_KEY", "").strip()
ANTHROPIC_API_KEY    = os.environ.get("ANTHROPIC_API_KEY", "").strip()
BABELSTREET_API_KEY  = os.environ.get("BABELSTREET_API_KEY", "").strip()
BLUESKY_IDENTIFIER   = os.environ.get("BLUESKY_IDENTIFIER", "").strip()
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()
PODCAST_INDEX_KEY    = os.environ.get("PODCAST_INDEX_KEY", "").strip()
PODCAST_INDEX_SECRET = os.environ.get("PODCAST_INDEX_SECRET", "").strip()
OPENALEX_MAILTO      = os.environ.get("OPENALEX_MAILTO", "").strip() or "osint@xtag.app"

SB_BASE = "https://scrapebadger.com/v1"
SENTIMENT_ENABLED = bool(ANTHROPIC_API_KEY or BABELSTREET_API_KEY)
USER_AGENT  = "web:xtag:2.0 (narrative-intelligence)"
BROWSER_UA  = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
               "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")
TIMEOUT     = 6
SERPAPI_TIMEOUT = 20
CACHE_TTL   = 1800
_cache: dict[str, tuple[float, dict]] = {}
_bsky_session = {"jwt": None, "ts": 0.0}

# ── Phase 0: Unified Document Schema ─────────────────────────────────────────
SOURCE_CREDIBILITY: dict[str, str] = {
    "presstv.ir": "state", "presstv.com": "state", "irna.ir": "state",
    "mehrnews.com": "state", "tasnimnews.com": "state", "almayadeen.net": "state",
    "almanar.com.lb": "state", "al-manar.com.lb": "state",
    "tass.com": "state", "tass.ru": "state", "rt.com": "state",
    "sputniknews.com": "state", "cgtn.com": "state", "xinhuanet.com": "state",
    "khamenei.ir": "state", "leader.ir": "state", "almasdarnews.com": "low",
    "reuters.com": "high", "apnews.com": "high", "bbc.com": "high", "bbc.co.uk": "high",
    "theguardian.com": "high", "nytimes.com": "high", "wsj.com": "high",
    "ft.com": "high", "economist.com": "high", "foreignpolicy.com": "high",
    "foreignaffairs.com": "high", "aljazeera.com": "high",
    "arxiv.org": "high", "doi.org": "high", "openalex.org": "high",
}

def _credibility_for_url(url: str) -> str:
    try:
        host = urlparse(url).hostname or ""
        host = host.lower().lstrip("www.")
        return SOURCE_CREDIBILITY.get(host, "unknown")
    except:
        return "unknown"

def make_doc(platform, url, text, title=None, author=None, author_url=None,
             thumbnail=None, timestamp=None, meta=None,
             source_type="social", language="en", credibility="unknown", raw=None):
    doc_id = hashlib.sha256((url or "").encode()).hexdigest()[:16]
    return {
        "id": doc_id, "platform": platform, "source_type": source_type,
        "url": url or "", "title": _strip_html(title) if title else None,
        "excerpt": _truncate(_strip_html(text)) if text else "",
        "author": author, "author_url": author_url, "thumbnail": thumbnail,
        "timestamp": timestamp, "meta": meta, "language": language,
        "credibility": credibility, "sentiment": None, "s_claude": None,
        "s_babel": None, "framing": None, "stance": None, "claims": [],
        "entities": [], "engagement": 0, "_raw": raw or {},
    }

# ── Adversary/state media RSS feeds ──────────────────────────────────────────
# NOTE ON EDITIONS: English editions of state media are sanitised for foreign
# audiences. The native-language originals carry markedly different framing and
# are the higher-value collection target. Both are included; native-language
# feeds are marked lang so the multilingual pipeline analyses them in-language.
ADVERSARY_RSS_FEEDS = [
    # ── Native language — PRIMARY collection value ──
    {"url": "https://www.almanar.com.lb/rss", "platform": "state_media",
     "author": "Al-Manar عربي (Hezbollah)", "credibility": "state", "lang": "ar"},
    {"url": "https://www.aljazeera.net/aljazeerarss/a7c186be-1baa-4bd4-9d80-a84db769f779/73d0e1b4-532f-45ef-b135-bfdff8b8cab9",
     "platform": "state_media", "author": "Al Jazeera عربي", "credibility": "medium", "lang": "ar"},
    {"url": "https://www.almayadeen.net/rss/all", "platform": "state_media",
     "author": "Al Mayadeen عربي", "credibility": "state", "lang": "ar"},
    {"url": "https://arabic.rt.com/rss/", "platform": "state_media",
     "author": "RT عربي (Russia)", "credibility": "state", "lang": "ar"},
    {"url": "https://www.mehrnews.com/rss", "platform": "state_media",
     "author": "Mehr فارسی (Iran)", "credibility": "state", "lang": "fa"},
    {"url": "https://www.irna.ir/rss", "platform": "state_media",
     "author": "IRNA فارسی (Iran)", "credibility": "state", "lang": "fa"},
    {"url": "https://farsi.khamenei.ir/rss-full", "platform": "state_media",
     "author": "Khamenei فارسی", "credibility": "state", "lang": "fa"},
    # ── English editions — for comparison against native framing ──
    {"url": "https://english.al-manar.com.lb/rss.php", "platform": "state_media", "author": "Al-Manar EN (Hezbollah)", "credibility": "state", "lang": "en"},
    {"url": "https://en.mehrnews.com/rss", "platform": "state_media", "author": "Mehr News EN (Iran)", "credibility": "state", "lang": "en"},
    {"url": "https://en.irna.ir/rss", "platform": "state_media", "author": "IRNA EN (Iran)", "credibility": "state", "lang": "en"},
    {"url": "https://www.tasnimnews.com/en/rss", "platform": "state_media", "author": "Tasnim EN (Iran)", "credibility": "state", "lang": "en"},
    {"url": "https://english.khamenei.ir/rss", "platform": "state_media", "author": "Khamenei.ir EN", "credibility": "state", "lang": "en"},
    {"url": "https://www.almayadeen.net/rss.xml", "platform": "state_media", "author": "Al Mayadeen EN", "credibility": "state", "lang": "en"},
    {"url": "https://www.presstv.ir/section/351020109.rss", "platform": "state_media", "author": "PressTV EN (Iran)", "credibility": "state", "lang": "en"},
    {"url": "https://tass.com/rss/v2.xml", "platform": "state_media", "author": "TASS EN (Russia)", "credibility": "state", "lang": "en"},
    {"url": "https://www.rt.com/rss/", "platform": "state_media", "author": "RT EN (Russia)", "credibility": "state", "lang": "en"},
    {"url": "https://www.cgtn.com/subscribe/rss/section/world.do", "platform": "state_media", "author": "CGTN EN (China)", "credibility": "state", "lang": "en"},
]

PODCAST_WATCHLIST = [
    "Intelligence Matters", "Risky Business", "Lawfare Podcast",
    "The Cipher Brief", "Geopolitics Decanted", "War on the Rocks",
    "Middle East Eye Podcast", "CTC Sentinel", "The Iran Primer",
    "Near East Policy", "Conflict Zones",
]

# ── Helpers ───────────────────────────────────────────────────────────────────
def _query_parts(q):
    raw = (q or "").strip()
    is_tag = raw.startswith("#")
    plain = raw.lstrip("#").strip()
    tag = "#" + re.sub(r"\s+", "_", plain) if plain else ""
    return is_tag, tag, plain

def _strip_html(s):
    if not s: return ""
    s = re.sub(r"<[^>]+>", " ", str(s))
    s = html.unescape(s)
    return re.sub(r"\s+", " ", s).strip()

def _truncate(s, n=280):
    if len(s) <= n: return s
    return s[:n-1].rstrip() + "…"

def _iso(dt_val):
    if dt_val is None: return None
    if isinstance(dt_val, (int, float)):
        try: return datetime.fromtimestamp(dt_val, tz=timezone.utc).isoformat()
        except: return None
    if isinstance(dt_val, str): return dt_val
    return None

def _empty(platform, error=None):
    return {"platform": platform, "results": [], "error": error}

_INT_RE = re.compile(r"\d[\d,]*")

def _engagement_breakdown(meta):
    out = {"reactions": 0, "comments": 0, "shares": 0}
    if not meta: return out
    s = str(meta)
    def grab(pattern):
        m = re.search(pattern, s)
        if not m: return 0
        try: return int(m.group(1).replace(",", ""))
        except: return 0
    out["reactions"] += grab(r"♥\s*([\d,]+)")
    out["reactions"] += grab(r"▶\s*([\d,]+)")
    out["shares"]    += grab(r"↺\s*([\d,]+)")
    out["comments"]  += grab(r"\U0001f4ac\s*([\d,]+)")
    out["reactions"] += grab(r"([\d,]+)\s*pts")
    out["reactions"] += grab(r"([\d,]+)\s*likes")
    out["comments"]  += grab(r"([\d,]+)\s*comments")
    return out

def _parse_dt(val):
    if val is None: return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12: ts /= 1000.0
        try: return datetime.fromtimestamp(ts, tz=timezone.utc)
        except: return None
    s = str(val).strip()
    if not s: return None
    if s.isdigit(): return _parse_dt(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except: pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt: return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except: pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except: continue
    return None

# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — MULTILINGUAL PIPELINE
# Analyse in SOURCE language, translate for display only.
# ═══════════════════════════════════════════════════════════════════════════════

LANG_META = {
    "ar": {"name": "Arabic",   "native": "العربية",   "rtl": True,  "flag": "🇸🇦"},
    "fa": {"name": "Persian",  "native": "فارسی",     "rtl": True,  "flag": "🇮🇷"},
    "he": {"name": "Hebrew",   "native": "עברית",     "rtl": True,  "flag": "🇮🇱"},
    "ru": {"name": "Russian",  "native": "Русский",   "rtl": False, "flag": "🇷🇺"},
    "zh": {"name": "Chinese",  "native": "中文",       "rtl": False, "flag": "🇨🇳"},
    "ur": {"name": "Urdu",     "native": "اردو",      "rtl": True,  "flag": "🇵🇰"},
    "tr": {"name": "Turkish",  "native": "Türkçe",    "rtl": False, "flag": "🇹🇷"},
    "en": {"name": "English",  "native": "English",   "rtl": False, "flag": "🇬🇧"},
}

# Persian-specific letters (not in standard Arabic): پ چ ژ گ ک ی
_FARSI_CHARS = set("\u067E\u0686\u0698\u06AF\u06A9\u06CC")
# Urdu-specific: ٹ ڈ ڑ ں ھ ے
_URDU_CHARS = set("\u0679\u0688\u0691\u06BA\u06BE\u06D2")

def detect_language(text: str) -> str:
    """
    Script-based language detection. Fast, no dependencies, reliable for the
    scripts that matter here (Arabic/Farsi/Hebrew/Cyrillic/CJK).
    Returns ISO 639-1 code.
    """
    if not text:
        return "en"
    sample = text[:600]
    counts = {"arabic": 0, "hebrew": 0, "cyrillic": 0, "cjk": 0, "latin": 0}
    has_farsi = has_urdu = False

    for ch in sample:
        cp = ord(ch)
        if 0x0600 <= cp <= 0x06FF or 0x0750 <= cp <= 0x077F or 0xFB50 <= cp <= 0xFDFF:
            counts["arabic"] += 1
            if ch in _FARSI_CHARS: has_farsi = True
            if ch in _URDU_CHARS:  has_urdu = True
        elif 0x0590 <= cp <= 0x05FF:
            counts["hebrew"] += 1
        elif 0x0400 <= cp <= 0x04FF:
            counts["cyrillic"] += 1
        elif 0x4E00 <= cp <= 0x9FFF or 0x3040 <= cp <= 0x30FF:
            counts["cjk"] += 1
        elif ch.isalpha() and cp < 0x250:
            counts["latin"] += 1

    total = sum(counts.values())
    if total < 3:
        return "en"

    dominant = max(counts, key=counts.get)
    ratio = counts[dominant] / total
    if ratio < 0.25:
        return "en"

    if dominant == "arabic":
        if has_urdu:  return "ur"
        if has_farsi: return "fa"
        return "ar"
    if dominant == "hebrew":   return "he"
    if dominant == "cyrillic": return "ru"
    if dominant == "cjk":      return "zh"
    return "en"


def translate_batch(texts: list, source_lang: str) -> list | None:
    """
    Translate a batch of texts to English for DISPLAY only.
    Analysis is always performed on the original language text.
    """
    if not ANTHROPIC_API_KEY or not texts:
        return None
    lang_name = LANG_META.get(source_lang, {}).get("name", source_lang)
    batch = texts[:40]
    numbered = "\n".join(f"{i+1}. {t[:300]}" for i, t in enumerate(batch))
    prompt = (
        f"Translate each numbered {lang_name} text to English.\n"
        "Preserve meaning precisely — this is for intelligence analysis, so keep "
        "connotation, register, and any loaded or euphemistic language intact.\n"
        "Do NOT sanitise, soften, or editorialise.\n"
        'Output ONLY a JSON array of strings, one per input, in order. No prose.\n\n'
        + numbered
    )
    text = _claude_call(prompt, 2600)
    if not text:
        return None
    try:
        arr = json.loads(re.sub(r"```json|```", "", text).strip())
        out = [str(x) for x in arr]
        while len(out) < len(texts):
            out.append("")
        return out[:len(texts)]
    except Exception:
        return None


def enrich_languages(platforms: dict) -> dict:
    """
    Detect language on every document, then translate non-English documents
    for display. Returns language distribution summary.
    Sets on each doc: language, language_name, rtl, title_en, excerpt_en, translated.
    """
    all_docs = []
    for group in platforms.values():
        all_docs.extend(group.get("results", []) or [])

    lang_counts: dict[str, int] = defaultdict(int)
    by_lang: dict[str, list] = defaultdict(list)

    for doc in all_docs:
        combined = ((doc.get("title") or "") + " " + (doc.get("excerpt") or "")).strip()
        lang = detect_language(combined)
        doc["language"] = lang
        meta = LANG_META.get(lang, {})
        doc["language_name"] = meta.get("name", lang)
        doc["rtl"] = meta.get("rtl", False)
        doc["translated"] = False
        lang_counts[lang] += 1
        if lang != "en":
            by_lang[lang].append(doc)

    # Translate non-English docs, grouped by language, in parallel
    def _translate_lang(lang: str, docs: list):
        # Cap translation volume per language to control cost/latency
        subset = docs[:40]
        titles = [(d.get("title") or "")[:300] for d in subset]
        excerpts = [(d.get("excerpt") or "")[:300] for d in subset]
        combined = [f"{t} || {e}".strip(" |") for t, e in zip(titles, excerpts)]
        translated = translate_batch(combined, lang)
        if not translated:
            return
        for d, tr in zip(subset, translated):
            if not tr:
                continue
            if "||" in tr:
                t_part, _, e_part = tr.partition("||")
                d["title_en"] = t_part.strip() or None
                d["excerpt_en"] = e_part.strip()
            else:
                d["excerpt_en"] = tr.strip()
            d["translated"] = True

    if by_lang and ANTHROPIC_API_KEY:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futures = [ex.submit(_translate_lang, lang, docs)
                       for lang, docs in list(by_lang.items())[:5]]
            for f in futures:
                try: f.result(timeout=25)
                except Exception: pass

    distribution = [
        {"lang": lang, "count": n,
         "name": LANG_META.get(lang, {}).get("name", lang),
         "native": LANG_META.get(lang, {}).get("native", lang),
         "flag": LANG_META.get(lang, {}).get("flag", "")}
        for lang, n in sorted(lang_counts.items(), key=lambda kv: kv[1], reverse=True)
    ]
    return {
        "distribution": distribution,
        "languages_detected": len(lang_counts),
        "non_english_docs": sum(n for l, n in lang_counts.items() if l != "en"),
        "total_docs": len(all_docs),
    }


# ── NotebookLM ────────────────────────────────────────────────────────────────
NOTEBOOKLM_SYNC_INTERVAL = 3600
def _load_auth_chunks():
    parts, i = [], 1
    while True:
        part = os.environ.get(f"NOTEBOOKLM_AUTH_{i}", "").strip()
        if not part: break
        parts.append(part); i += 1
    return "".join(parts)
NOTEBOOKLM_AUTH_ARCHIVE = _load_auth_chunks()
_notebook_store: dict[str, dict] = {}
_notebooklm_status: dict = {"last_sync": None, "notebooks": 0, "error": None}

# ── Telegram channels ─────────────────────────────────────────────────────────
_DEFAULT_TG_CHANNELS = (
    "telegram,durov,bbcnews,reuters,cnn,aljazeera,dwnews,rtnews,"
    "sputnik,tass_agency,nexta_live,disclosetv,insiderpaper,"
    "bellingcat,intelslava,worldnews,iranintl,middleeasteye,"
    "almayadeen_net,almanar_tv"
)
def _parse_tg_channels():
    raw = os.environ.get("TELEGRAM_CHANNELS", "").strip() or _DEFAULT_TG_CHANNELS
    channels = []
    for c in raw.split(","):
        c = c.strip()
        if not c: continue
        c = c.replace("https://","").replace("http://","").replace("t.me/s/","").replace("t.me/","")
        c = c.lstrip("@/").strip("/")
        if c and c not in channels: channels.append(c)
    return channels[:60]
TELEGRAM_CHANNELS = _parse_tg_channels()

DOMAIN_MAP = {
    "x.com":"x","twitter.com":"x","mobile.twitter.com":"x",
    "instagram.com":"instagram","tiktok.com":"tiktok","vm.tiktok.com":"tiktok",
    "facebook.com":"facebook","m.facebook.com":"facebook","linkedin.com":"linkedin",
    "pinterest.com":"pinterest","pin.it":"pinterest","threads.net":"threads",
    "threads.com":"threads","tumblr.com":"tumblr","reddit.com":"reddit",
    "old.reddit.com":"reddit","bsky.app":"bluesky","youtube.com":"youtube",
    "youtu.be":"youtube","m.youtube.com":"youtube","mastodon.social":"mastodon",
    "mastodon.online":"mastodon","mstdn.social":"mastodon",
    "notebooklm.google.com":"notebooklm",
}

# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE FETCHERS
# ═══════════════════════════════════════════════════════════════════════════════

def search_youtube(q):
    if not YOUTUBE_API_KEY: return _empty("youtube", "YOUTUBE_API_KEY not set")
    try:
        r = requests.get("https://www.googleapis.com/youtube/v3/search",
            params={"part":"snippet","q":_query_parts(q)[2],"type":"video","maxResults":50,"key":YOUTUBE_API_KEY},
            timeout=TIMEOUT)
        r.raise_for_status()
        results = []
        for item in r.json().get("items",[]):
            vid = item.get("id",{}).get("videoId"); sn = item.get("snippet",{})
            if not vid: continue
            results.append(make_doc("youtube", f"https://www.youtube.com/watch?v={vid}",
                _strip_html(sn.get("description")), title=_strip_html(sn.get("title")),
                author=sn.get("channelTitle"),
                author_url=f"https://www.youtube.com/channel/{sn.get('channelId','')}",
                thumbnail=sn.get("thumbnails",{}).get("medium",{}).get("url"),
                timestamp=sn.get("publishedAt"), source_type="social"))
        return {"platform":"youtube","results":results,"error":None}
    except Exception as e: return _empty("youtube", str(e)[:120])

def search_reddit(q):
    is_tag,tag,plain = _query_parts(q); keyword = tag if is_tag else plain
    if not keyword: return _empty("reddit","empty query")
    if not SCRAPEBADGER_KEY: return _empty("reddit","SCRAPEBADGER_KEY not set")
    try:
        r = requests.get(f"{SB_BASE}/reddit/search/posts",
            params={"q":keyword,"sort":"relevance","t":"year","limit":100},
            headers={"x-api-key":SCRAPEBADGER_KEY}, timeout=SERPAPI_TIMEOUT)
    except Exception as e: return _empty("reddit", str(e)[:120])
    if r.status_code >= 400: return _empty("reddit", f"HTTP {r.status_code}")
    try: data = r.json()
    except: return _empty("reddit","bad JSON")
    items = data if isinstance(data,list) else (data.get("posts") or data.get("data") or [])
    results = []
    for d in items:
        if not isinstance(d,dict): continue
        permalink = d.get("permalink") or d.get("url") or ""
        if permalink.startswith("/"): permalink = f"https://www.reddit.com{permalink}"
        sub = d.get("subreddit") or ""; author = d.get("author") or "unknown"
        score = d.get("score") or 0; nc = d.get("num_comments") or 0
        ts = d.get("created_utc") or d.get("created")
        thumb = d.get("thumbnail")
        if not (isinstance(thumb,str) and thumb.startswith("http")): thumb = None
        results.append(make_doc("reddit", permalink or "https://www.reddit.com",
            _strip_html(d.get("selftext") or d.get("body") or ""),
            title=_strip_html(d.get("title")), author=f"u/{author}",
            author_url=f"https://www.reddit.com/user/{author}", thumbnail=thumb,
            timestamp=_iso(ts) if isinstance(ts,(int,float)) else ts,
            meta=f"r/{sub} · {score} pts · {nc} comments", source_type="social"))
    return {"platform":"reddit","results":results,"error":None}

def search_sb_twitter(q):
    is_tag,tag,plain = _query_parts(q); keyword = tag if is_tag else plain
    if not keyword: return _empty("x","empty query")
    if not SCRAPEBADGER_KEY: return _empty("x","SCRAPEBADGER_KEY not set")
    try:
        r = requests.get(f"{SB_BASE}/twitter/tweets/advanced_search",
            params={"query":keyword,"query_type":"Top","count":100},
            headers={"x-api-key":SCRAPEBADGER_KEY}, timeout=SERPAPI_TIMEOUT)
    except Exception as e: return _empty("x", str(e)[:120])
    if r.status_code >= 400: return _empty("x", f"HTTP {r.status_code}")
    try: data = r.json()
    except: return _empty("x","bad JSON")
    tweets = data.get("data") if isinstance(data,dict) else (data if isinstance(data,list) else [])
    results = []
    for t in (tweets or []):
        if not isinstance(t,dict): continue
        tid = t.get("id",""); username = t.get("username") or ""
        media = t.get("media") or []; thumb = None
        if media and isinstance(media[0],dict): thumb = media[0].get("preview_image_url") or media[0].get("url")
        favs=t.get("favorite_count",0); rts=t.get("retweet_count",0); reps=t.get("reply_count",0)
        results.append(make_doc("x",
            f"https://x.com/{username}/status/{tid}" if username and tid else "https://x.com",
            _strip_html(t.get("full_text") or t.get("text") or ""),
            author=f"@{username}" if username else None,
            author_url=f"https://x.com/{username}" if username else None,
            thumbnail=thumb, timestamp=t.get("created_at"),
            meta=f"♥ {favs} · ↺ {rts} · 💬 {reps}", source_type="social"))
    return {"platform":"x","results":results,"error":None}

def search_sb_tiktok(q):
    is_tag,tag,plain = _query_parts(q); keyword = tag if is_tag else plain
    if not keyword: return _empty("tiktok","empty query")
    if not SCRAPEBADGER_KEY: return _empty("tiktok","SCRAPEBADGER_KEY not set")
    try:
        r = requests.get(f"{SB_BASE}/tiktok/search/videos",
            params={"query":keyword,"region":"US","count":50},
            headers={"x-api-key":SCRAPEBADGER_KEY}, timeout=SERPAPI_TIMEOUT)
    except Exception as e: return _empty("tiktok", str(e)[:120])
    if r.status_code >= 400: return _empty("tiktok", f"HTTP {r.status_code}")
    try: data = r.json()
    except: return _empty("tiktok","bad JSON")
    videos = []
    if isinstance(data,list): videos = data
    elif isinstance(data,dict):
        for key in ("videos","data","results","aweme_list","item_list","videoList","items"):
            v = data.get(key)
            if isinstance(v,list) and v: videos = v; break
        if not videos and isinstance(data.get("data"),dict):
            for key in ("videos","aweme_list","item_list","videoList","items"):
                v = data["data"].get(key)
                if isinstance(v,list) and v: videos = v; break
    results = []
    for v in (videos or []):
        if not isinstance(v,dict): continue
        author = v.get("author") or v.get("author_info") or {}
        if not isinstance(author,dict): author = {}
        handle = author.get("unique_id") or author.get("uniqueId") or ""
        stats = v.get("stats") or v.get("statistics") or {}
        def _tt(k, *keys):
            for key in (k,)+keys:
                val = stats.get(key)
                if val is None: val = v.get(key)
                if val is not None:
                    try: return int(val)
                    except: pass
            return 0
        plays=_tt("play_count","playCount"); likes=_tt("digg_count","diggCount","like_count")
        comms=_tt("comment_count","commentCount")
        vmeta = v.get("video") or {}
        results.append(make_doc("tiktok",
            v.get("url") or v.get("share_url") or "https://www.tiktok.com",
            _strip_html(v.get("description") or v.get("desc") or v.get("title") or ""),
            author=f"@{handle}" if handle else None,
            author_url=f"https://www.tiktok.com/@{handle}" if handle else None,
            thumbnail=vmeta.get("cover") or v.get("cover") or v.get("thumbnail"),
            timestamp=v.get("create_time_at") or v.get("create_time"),
            meta=f"▶ {plays} · ♥ {likes} · 💬 {comms}", source_type="social"))
    return {"platform":"tiktok","results":results,"error":None}

def _bsky_token():
    if not BLUESKY_IDENTIFIER or not BLUESKY_APP_PASSWORD: return None
    now = time.time()
    if _bsky_session["jwt"] and now - _bsky_session["ts"] < 3600: return _bsky_session["jwt"]
    try:
        r = requests.post("https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier":BLUESKY_IDENTIFIER,"password":BLUESKY_APP_PASSWORD},
            headers={"User-Agent":USER_AGENT,"Content-Type":"application/json"}, timeout=TIMEOUT)
        if r.status_code >= 400: return None
        jwt = r.json().get("accessJwt"); _bsky_session["jwt"]=jwt; _bsky_session["ts"]=now
        return jwt
    except: return None

def search_bluesky(q):
    try:
        base = "https://bsky.social" if (BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD) else "https://public.api.bsky.app"
        h = {"User-Agent":USER_AGENT}
        tok = _bsky_token()
        if tok: h["Authorization"] = f"Bearer {tok}"
        is_tag,tag,plain = _query_parts(q)
        r = requests.get(f"{base}/xrpc/app.bsky.feed.searchPosts",
            params={"q":tag if is_tag else plain,"limit":100}, headers=h, timeout=TIMEOUT)
        r.raise_for_status()
        results = []
        for post in r.json().get("posts",[]):
            author = post.get("author",{}) or {}; record = post.get("record",{}) or {}
            handle = author.get("handle",""); uri = post.get("uri","")
            post_id = uri.split("/")[-1] if uri else ""
            results.append(make_doc("bluesky",
                f"https://bsky.app/profile/{handle}/post/{post_id}" if handle and post_id else "https://bsky.app",
                _strip_html(record.get("text","")),
                author=author.get("displayName") or f"@{handle}",
                author_url=f"https://bsky.app/profile/{handle}",
                thumbnail=author.get("avatar"), timestamp=post.get("indexedAt"),
                meta=f"♥ {post.get('likeCount',0)} · ↺ {post.get('repostCount',0)} · 💬 {post.get('replyCount',0)}",
                source_type="social"))
        return {"platform":"bluesky","results":results,"error":None}
    except Exception as e:
        msg = str(e)[:120]
        if "403" in msg and not (BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD):
            msg = "Bluesky requires auth — set BLUESKY_IDENTIFIER + BLUESKY_APP_PASSWORD"
        return _empty("bluesky", msg)

def search_mastodon(q):
    try:
        tag = q.lstrip("#").strip()
        if not tag: return _empty("mastodon","empty query")
        r = requests.get(f"https://mastodon.social/api/v1/timelines/tag/{quote_plus(tag)}",
            params={"limit":40}, headers={"User-Agent":USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        statuses = r.json()
        if not isinstance(statuses,list): return _empty("mastodon","unexpected shape")
        results = []
        for st in statuses:
            acc = st.get("account",{}) or {}
            results.append(make_doc("mastodon", st.get("url",""),
                _strip_html(st.get("content","")),
                author=acc.get("display_name") or f"@{acc.get('username','')}",
                author_url=acc.get("url"), thumbnail=acc.get("avatar"),
                timestamp=st.get("created_at"),
                meta=f"♥ {st.get('favourites_count',0)} · ↺ {st.get('reblogs_count',0)} · 💬 {st.get('replies_count',0)}",
                source_type="social"))
        return {"platform":"mastodon","results":results,"error":None}
    except Exception as e: return _empty("mastodon", str(e)[:120])

def search_hackernews(q):
    try:
        r = requests.get("https://hn.algolia.com/api/v1/search",
            params={"query":_query_parts(q)[2],"hitsPerPage":20,"tags":"story"},
            headers={"User-Agent":USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        results = []
        for hit in r.json().get("hits",[]):
            obj_id = hit.get("objectID","")
            results.append(make_doc("hackernews",
                hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}",
                _strip_html(hit.get("story_text")),
                title=hit.get("title") or hit.get("story_title"),
                author=hit.get("author"),
                author_url=f"https://news.ycombinator.com/user?id={hit.get('author','')}",
                timestamp=hit.get("created_at"),
                meta=f"{hit.get('points',0)} pts · {hit.get('num_comments',0)} comments",
                source_type="social"))
        return {"platform":"hackernews","results":results,"error":None}
    except Exception as e: return _empty("hackernews", str(e)[:120])

def search_gnews(q):
    try:
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, headers={"User-Agent":USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        feed = feedparser.parse(r.content); results = []
        for entry in feed.entries[:60]:
            source = ""
            if hasattr(entry,"source") and entry.source:
                source = entry.source.get("title","") if isinstance(entry.source,dict) else str(entry.source)
            eurl = entry.get("link","")
            results.append(make_doc("gnews", eurl, _strip_html(entry.get("summary","")),
                title=_strip_html(entry.get("title")), author=source or None,
                timestamp=entry.get("published"), meta=source,
                source_type="news", credibility=_credibility_for_url(eurl)))
        return {"platform":"gnews","results":results,"error":None}
    except Exception as e: return _empty("gnews", str(e)[:120])

def _fetch_tg_channel(channel, keyword_lc):
    try:
        r = requests.get(f"https://t.me/s/{channel}", headers={"User-Agent":BROWSER_UA},
                         timeout=TIMEOUT, allow_redirects=True)
    except: return []
    if r.status_code != 200 or "tgme_widget_message_text" not in r.text: return []
    try: soup = BeautifulSoup(r.text,"lxml")
    except: return []
    posts = []
    for m in soup.select(".tgme_widget_message"):
        text_el = m.select_one(".tgme_widget_message_text")
        if not text_el: continue
        text = text_el.get_text(" ", strip=True)
        if not text: continue
        if keyword_lc and keyword_lc not in text.lower(): continue
        link_el = m.select_one("a.tgme_widget_message_date")
        link = link_el.get("href") if link_el else f"https://t.me/{channel}"
        time_el = m.select_one("time")
        dt = time_el.get("datetime") if time_el else None
        views_el = m.select_one(".tgme_widget_message_views")
        views = views_el.get_text(strip=True) if views_el else None
        data_post = m.get("data-post","")
        chan_name = data_post.split("/")[0] if "/" in data_post else channel
        thumb = None
        photo = m.select_one(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_thumb")
        if photo and photo.get("style"):
            mobj = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", photo["style"])
            if mobj: thumb = mobj.group(1)
        doc = make_doc("telegram", link, text, author=f"@{chan_name}",
                       author_url=f"https://t.me/{chan_name}", thumbnail=thumb, timestamp=dt,
                       meta=f"t.me/{chan_name}" + (f" · {views} views" if views else ""),
                       source_type="social")
        doc["_ts_sort"] = dt or ""
        posts.append(doc)
    return posts

def search_telegram(q):
    keyword = q.lstrip("#").strip(); keyword_lc = keyword.lower()
    if not keyword: return {"platform":"telegram","results":[],"error":"empty query"}
    if not TELEGRAM_CHANNELS: return {"platform":"telegram","results":[],"error":"no channels"}
    all_posts = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_tg_channel, ch, keyword_lc): ch for ch in TELEGRAM_CHANNELS}
        try:
            for fut in as_completed(futures, timeout=TIMEOUT+6):
                try: all_posts.extend(fut.result())
                except: pass
        except: pass
    all_posts.sort(key=lambda p: p.get("_ts_sort",""), reverse=True)
    for p in all_posts: p.pop("_ts_sort",None)
    return {"platform":"telegram","results":all_posts[:50],"error":None}

# ── GDELT (Phase 1) ───────────────────────────────────────────────────────────
def search_gdelt(q):
    plain = _query_parts(q)[2]
    if not plain: return _empty("gdelt","empty query")
    try:
        r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc",
            params={"query":plain,"mode":"artlist","maxrecords":75,"format":"json",
                    "sort":"DateDesc","TIMESPAN":"72H"},
            headers={"User-Agent":USER_AGENT}, timeout=TIMEOUT+10)
        if r.status_code == 204: return _empty("gdelt","no results")
        r.raise_for_status()
        data = r.json(); articles = data.get("articles") or []; results = []
        for art in articles:
            url_str = art.get("url","")
            doc = make_doc("gdelt", url_str,
                _strip_html((art.get("title") or "")),
                title=_strip_html(art.get("title")),
                author=art.get("domain"), timestamp=art.get("seendate"),
                meta=f"{art.get('domain','')} · tone:{art.get('tone','')}",
                source_type="news", language=art.get("language","en"),
                credibility=_credibility_for_url(url_str))
            tone = art.get("tone")
            if tone is not None:
                try:
                    t = float(tone)
                    doc["framing"] = "negative" if t < -2 else "positive" if t > 2 else "neutral"
                except: pass
            results.append(doc)
        return {"platform":"gdelt","results":results,"error":None}
    except Exception as e: return _empty("gdelt", str(e)[:120])

# ── State media RSS (Phase 1) ─────────────────────────────────────────────────
# ── Cross-lingual query expansion ─────────────────────────────────────────────
# Searching "Hezbollah" in English will never match an Arabic feed that says
# "حزب الله". Queries are expanded into the target languages before filtering.

# Curated translations for high-frequency OSINT terms — instant, no API call.
QUERY_LEXICON: dict[str, dict[str, list]] = {
    "hezbollah":   {"ar": ["حزب الله"], "fa": ["حزب الله"], "he": ["חיזבאללה"]},
    "hizbullah":   {"ar": ["حزب الله"], "fa": ["حزب الله"], "he": ["חיזבאללה"]},
    "israel":      {"ar": ["إسرائيل", "الكيان الصهيوني"], "fa": ["اسرائیل", "رژیم صهیونیستی"], "he": ["ישראל"]},
    "iran":        {"ar": ["إيران"], "fa": ["ایران"], "he": ["איראן"]},
    "irgc":        {"ar": ["الحرس الثوري"], "fa": ["سپاه پاسداران"], "he": ["משמרות המהפכה"]},
    "lebanon":     {"ar": ["لبنان"], "fa": ["لبنان"], "he": ["לבנון"]},
    "gaza":        {"ar": ["غزة"], "fa": ["غزه"], "he": ["עזה"]},
    "hamas":       {"ar": ["حماس"], "fa": ["حماس"], "he": ["חמאס"]},
    "syria":       {"ar": ["سوريا"], "fa": ["سوریه"], "he": ["סוריה"]},
    "yemen":       {"ar": ["اليمن"], "fa": ["یمن"], "he": ["תימן"]},
    "houthi":      {"ar": ["الحوثي", "أنصار الله"], "fa": ["حوثی"], "he": ["חות'ים"]},
    "nasrallah":   {"ar": ["نصر الله"], "fa": ["نصرالله"], "he": ["נסראללה"]},
    "khamenei":    {"ar": ["خامنئي"], "fa": ["خامنه‌ای"], "he": ["ח'אמנputti"]},
    "idf":         {"ar": ["الجيش الإسرائيلي"], "fa": ["ارتش اسرائیل"], "he": ["צה\"ל"]},
    "ceasefire":   {"ar": ["وقف إطلاق النار"], "fa": ["آتش‌بس"], "he": ["הפסקת אש"]},
    "nuclear":     {"ar": ["نووي"], "fa": ["هسته‌ای"], "he": ["גרעיני"]},
    "sanctions":   {"ar": ["عقوبات"], "fa": ["تحریم"], "he": ["סנקציות"]},
    "drone":       {"ar": ["مسيرة", "طائرة مسيرة"], "fa": ["پهپاد"], "he": ["רחפן"]},
    "missile":     {"ar": ["صاروخ"], "fa": ["موشک"], "he": ["טיל"]},
    "russia":      {"ar": ["روسيا"], "fa": ["روسیه"], "he": ["רוסיה"]},
    "ukraine":     {"ar": ["أوكرانيا"], "fa": ["اوکراین"], "he": ["אוקראינה"]},
    "nato":        {"ar": ["الناتو"], "fa": ["ناتو"], "he": ["נאט\"ו"]},
    "palestine":   {"ar": ["فلسطين"], "fa": ["فلسطین"], "he": ["פלסטין"]},
}

_query_expansion_cache: dict[str, dict] = {}

def expand_query(q: str, target_langs: tuple = ("ar", "fa", "he")) -> dict:
    """
    Expand an English query into target languages so native-language sources
    can actually be searched. Lexicon first (instant), Claude fallback (cached).
    Returns {lang: [terms]} including the original under 'en'.
    """
    key = q.lower().strip()
    if key in _query_expansion_cache:
        return _query_expansion_cache[key]

    out: dict[str, list] = {"en": [key]}
    # Lexicon lookup — handles multi-word queries by checking each known term
    matched = False
    for term, translations in QUERY_LEXICON.items():
        if term in key:
            matched = True
            for lang, variants in translations.items():
                if lang in target_langs:
                    out.setdefault(lang, []).extend(variants)

    # Claude fallback for anything not in the lexicon
    if not matched and ANTHROPIC_API_KEY and len(key) < 60:
        lang_names = ", ".join(LANG_META.get(l, {}).get("name", l) for l in target_langs)
        prompt = (
            f'Translate the search term "{q}" into: {lang_names}.\n'
            "Give the term as it would actually appear in news media in that language "
            "(including common alternative renderings).\n"
            'ONLY JSON: {"ar":["..."],"fa":["..."],"he":["..."]}. No prose.'
        )
        text = _claude_call(prompt, 300)
        if text:
            try:
                arr = json.loads(re.sub(r"```json|```", "", text).strip())
                for lang, variants in arr.items():
                    if lang in target_langs and isinstance(variants, list):
                        out.setdefault(lang, []).extend(str(v) for v in variants[:3])
            except Exception:
                pass

    # Dedupe
    for lang in out:
        out[lang] = list(dict.fromkeys(out[lang]))
    _query_expansion_cache[key] = out
    return out


def _fetch_rss_feed(feed_cfg, terms: list):
    """Fetch one feed; match against ANY of the supplied terms (multilingual)."""
    try:
        r = requests.get(feed_cfg["url"], headers={"User-Agent":BROWSER_UA}, timeout=TIMEOUT+2)
        if r.status_code >= 400: return []
        feed = feedparser.parse(r.content); results = []
        feed_lang = feed_cfg.get("lang", "en")
        for entry in feed.entries[:40]:
            title = _strip_html(entry.get("title",""))
            summary = _strip_html(entry.get("summary",""))
            haystack = (title + " " + summary)
            haystack_lc = haystack.lower()
            if terms and not any(t.lower() in haystack_lc for t in terms if t):
                continue
            eurl = entry.get("link","")
            doc = make_doc("state_media", eurl, summary, title=title,
                author=feed_cfg.get("author"), timestamp=entry.get("published"),
                meta=feed_cfg.get("author",""), source_type="state_media",
                language=feed_lang,
                credibility=feed_cfg.get("credibility","state"))
            results.append(doc)
        return results
    except Exception:
        return []


def search_state_media(q):
    keyword = q.lstrip("#").strip()
    if not keyword: return _empty("state_media","empty query")
    expansion = expand_query(keyword)
    all_results = []
    errors = 0
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {}
        for cfg in ADVERSARY_RSS_FEEDS:
            feed_lang = cfg.get("lang", "en")
            # Match against terms in the feed's own language, plus the raw query
            terms = list(expansion.get(feed_lang, [])) + [keyword]
            futures[ex.submit(_fetch_rss_feed, cfg, terms)] = cfg
        try:
            for fut in as_completed(futures, timeout=TIMEOUT+8):
                try: all_results.extend(fut.result())
                except Exception: errors += 1
        except Exception: pass
    all_results.sort(key=lambda d: d.get("timestamp") or "", reverse=True)
    err = None
    if not all_results:
        err = f"no matches across {len(ADVERSARY_RSS_FEEDS)} state media feeds"
    return {"platform":"state_media","results":all_results[:80],"error":err,
            "expansion":{k:v for k,v in expansion.items() if k!="en"}}

# ── Academic (Phase 1) ────────────────────────────────────────────────────────
def search_academic(q):
    plain = _query_parts(q)[2]
    if not plain: return _empty("academic","empty query")
    results = []
    errors = []
    try:
        r = requests.get("https://api.openalex.org/works",
            params={"search":plain,"filter":"is_oa:true","per_page":20,
                    "sort":"publication_date:desc","mailto":OPENALEX_MAILTO},
            headers={"User-Agent":USER_AGENT}, timeout=TIMEOUT+4)
        if r.status_code == 429:
            errors.append("OpenAlex rate limited — set OPENALEX_MAILTO env var for the polite pool")
        r.raise_for_status()
        for work in r.json().get("results",[]):
            primary_loc = work.get("primary_location") or {}
            source = primary_loc.get("source") or {}
            best_url = (work.get("doi") or primary_loc.get("landing_page_url") or "")
            authors = [a.get("author",{}).get("display_name","")
                       for a in (work.get("authorships") or [])[:3]]
            author_str = "; ".join(a for a in authors if a)
            abstract_inv = work.get("abstract_inverted_index") or {}
            abstract = ""
            if abstract_inv:
                try:
                    pairs = sorted([(pos,word) for word,positions in abstract_inv.items() for pos in positions])
                    abstract = " ".join(word for _,word in pairs[:80])
                except: pass
            results.append(make_doc("academic",
                best_url or f"https://openalex.org/{work.get('id','').split('/')[-1]}",
                abstract or work.get("title",""),
                title=work.get("title"), author=author_str or source.get("display_name"),
                timestamp=work.get("publication_date"),
                meta=f"{source.get('display_name','')} · {work.get('publication_year','')} · {work.get('cited_by_count',0)} citations",
                source_type="academic", credibility="high"))
    except Exception as e:
        app.logger.warning("OpenAlex: %s", e)
        if not errors: errors.append(f"OpenAlex: {str(e)[:80]}")
    try:
        r = requests.get("https://export.arxiv.org/api/query",
            params={"search_query":f"all:{plain}","max_results":15,"sortBy":"submittedDate","sortOrder":"descending"},
            headers={"User-Agent":USER_AGENT}, timeout=TIMEOUT+4)
        if r.status_code == 429:
            errors.append("arXiv rate limited — retry shortly")
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        for entry in feed.entries:
            authors = ", ".join(a.get("name","") for a in getattr(entry,"authors",[])[:3])
            cats = " · ".join(t.get("term","") for t in getattr(entry,"tags",[])[:2])
            results.append(make_doc("academic", entry.get("link",""),
                _strip_html(entry.get("summary","")),
                title=_strip_html(entry.get("title")),
                author=authors or None, timestamp=entry.get("published"),
                meta=f"arXiv · {cats}", source_type="academic", credibility="high"))
    except Exception as e:
        app.logger.warning("arXiv: %s", e)
        errors.append(f"arXiv: {str(e)[:80]}")
    # Honesty: if we got nothing, say WHY rather than silently returning empty
    err = None if results else (" · ".join(errors[:2]) if errors else "no matching papers")
    return {"platform":"academic","results":results,"error":err}

# ── Podcasts (Phase 1) ────────────────────────────────────────────────────────
def search_podcasts(q):
    plain = _query_parts(q)[2]
    if not plain: return _empty("podcasts","empty query")
    try:
        epoch_time = int(time.time())
        headers = {"User-Agent":USER_AGENT}
        if PODCAST_INDEX_KEY and PODCAST_INDEX_SECRET:
            hash_str = PODCAST_INDEX_KEY + PODCAST_INDEX_SECRET + str(epoch_time)
            auth_hash = hashlib.sha1(hash_str.encode()).hexdigest()
            headers.update({"X-Auth-Date":str(epoch_time),"X-Auth-Key":PODCAST_INDEX_KEY,"Authorization":auth_hash})
        r = requests.get("https://api.podcastindex.org/api/1.0/search/byterm",
            params={"q":plain,"max":10}, headers=headers, timeout=TIMEOUT)
        if r.status_code >= 400: return _empty("podcasts", f"HTTP {r.status_code}")
        feeds_data = r.json().get("feeds",[])
        results = []
        for feed in feeds_data[:10]:
            feed_title = feed.get("title","")
            on_watchlist = any(w.lower() in feed_title.lower() for w in PODCAST_WATCHLIST)
            results.append(make_doc("podcasts",
                feed.get("link") or feed.get("url") or "",
                _strip_html(feed.get("description") or feed.get("title") or ""),
                title=feed_title, author=feed.get("author") or feed.get("ownerName"),
                thumbnail=feed.get("image"),
                timestamp=_iso(feed.get("newestItemPublishTime")),
                meta=f"Podcast · {feed.get('episodeCount',0)} eps" + (" · ★ Watchlist" if on_watchlist else ""),
                source_type="podcast"))
        return {"platform":"podcasts","results":results,"error":None}
    except Exception as e: return _empty("podcasts", str(e)[:120])

# ── SerpAPI ───────────────────────────────────────────────────────────────────
SERPAPI_PLATFORM_DOMAINS = {
    "instagram":["instagram.com"],"facebook":["facebook.com"],"linkedin":["linkedin.com"],
    "pinterest":["pinterest.com"],"threads":["threads.net"],"tumblr":["tumblr.com"],
    "bluesky":["bsky.app"],"youtube":["youtube.com"],
}
_ALL_SOCIAL_DOMAINS = [d for domains in SERPAPI_PLATFORM_DOMAINS.values() for d in domains]
SERPAPI_SITE_FILTER = "(" + " OR ".join(f"site:{d}" for d in _ALL_SOCIAL_DOMAINS) + ")"

def _detect_platform_from_url(url):
    try:
        host = (urlparse(url).hostname or "").lower().lstrip(".")
        if host.startswith("www."): host = host[4:]
        while host:
            if host in DOMAIN_MAP: return DOMAIN_MAP[host]
            if "." not in host: break
            host = host.split(".",1)[1]
    except: pass
    return None

def _extract_author(platform_id, url):
    try:
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        if platform_id == "x" and path_parts: return f"@{path_parts[0]}"
        if platform_id in ("instagram","threads") and path_parts and path_parts[0] not in ("p","reel","tv","explore"):
            return f"@{path_parts[0]}"
        if platform_id == "tiktok" and path_parts and path_parts[0].startswith("@"): return path_parts[0]
        if platform_id == "linkedin" and len(path_parts) > 1 and path_parts[0] == "in": return path_parts[1]
        if platform_id == "reddit" and len(path_parts) >= 2 and path_parts[0] == "r": return f"r/{path_parts[1]}"
        if platform_id == "youtube" and path_parts and path_parts[0].startswith("@"): return path_parts[0]
    except: pass
    return None

def search_serpapi(q):
    all_platforms = list(SERPAPI_PLATFORM_DOMAINS.keys())
    is_tag,tag,plain = _query_parts(q)
    if not plain: return {p:_empty(p,"empty query") for p in all_platforms}
    if not SERPAPI_KEY: return {p:_empty(p,"SERPAPI_KEY not set") for p in all_platforms}
    out = {p:{"platform":p,"results":[],"error":None} for p in all_platforms}
    query = f'"{tag}"' if is_tag else plain
    query += f" {SERPAPI_SITE_FILTER}"
    try:
        r = requests.get("https://serpapi.com/search",
            params={"engine":"google","q":query,"num":60,"api_key":SERPAPI_KEY,"safe":"off"},
            timeout=SERPAPI_TIMEOUT)
    except Exception as e: return {p:_empty(p,str(e)[:120]) for p in all_platforms}
    if r.status_code >= 400: return {p:_empty(p,f"SerpApi HTTP {r.status_code}") for p in all_platforms}
    try: data = r.json()
    except: return {p:_empty(p,"bad JSON") for p in all_platforms}
    if isinstance(data,dict) and data.get("error"):
        return {p:_empty(p,str(data["error"])[:120]) for p in all_platforms}
    for item in (data.get("organic_results") or []):
        url_str = item.get("link","")
        platform = _detect_platform_from_url(url_str)
        if not platform or platform not in out: continue
        thumb = item.get("thumbnail")
        if not thumb:
            rich = item.get("rich_snippet") or {}
            imgs = (rich.get("top") or {}).get("images") or []
            if imgs and isinstance(imgs[0],str): thumb = imgs[0]
        doc = make_doc(platform, url_str, _strip_html(item.get("snippet","")),
            title=_strip_html(item.get("title")), author=_extract_author(platform,url_str),
            thumbnail=thumb, timestamp=item.get("date"),
            meta=(item.get("displayed_link") or "").replace("https://","").replace("www.","").split("/")[0],
            source_type="social")
        out[platform]["results"].append(doc)
    return out

def search_google_web(q):
    is_tag,tag,plain = _query_parts(q)
    if not plain: return _empty("google","empty query")
    if not SERPAPI_KEY: return _empty("google","SERPAPI_KEY not set")
    try:
        r = requests.get("https://serpapi.com/search",
            params={"engine":"google","q":f'"{tag}"' if is_tag else plain,
                    "num":20,"api_key":SERPAPI_KEY,"safe":"off"}, timeout=SERPAPI_TIMEOUT)
    except Exception as e: return _empty("google", str(e)[:120])
    if r.status_code >= 400: return _empty("google", f"SerpApi HTTP {r.status_code}")
    try: data = r.json()
    except: return _empty("google","bad JSON")
    if isinstance(data,dict) and data.get("error"): return _empty("google",str(data["error"])[:120])
    results = []
    for item in (data.get("organic_results") or []):
        url_str = item.get("link","")
        results.append(make_doc("google", url_str, _strip_html(item.get("snippet","")),
            title=_strip_html(item.get("title")),
            author=(item.get("source") or (item.get("displayed_link") or "").replace("https://","").replace("www.","").split("/")[0]) or None,
            thumbnail=item.get("thumbnail"), timestamp=item.get("date"),
            meta=(item.get("displayed_link") or "").replace("https://","").replace("www.","").split("/")[0],
            source_type="news", credibility=_credibility_for_url(url_str)))
    return {"platform":"google","results":results,"error":None}

# ── NotebookLM ────────────────────────────────────────────────────────────────
def _restore_notebooklm_auth():
    if not NOTEBOOKLM_AUTH_ARCHIVE: return False
    try:
        archive = NOTEBOOKLM_AUTH_ARCHIVE + "=" * (-len(NOTEBOOKLM_AUTH_ARCHIVE) % 4)
        data = base64.b64decode(archive)
        home = os.path.expanduser("~")
        subprocess.run(["tar","-xzf","-","-C",home,"--no-same-owner"], input=data, capture_output=True)
        if os.path.isdir(os.path.join(home,".notebooklm")):
            _notebooklm_status["restore"]="ok"; return True
        return False
    except: return False

async def _sync_notebooks_async():
    from notebooklm import NotebookLMClient
    synced = {}
    async with NotebookLMClient.from_storage() as client:
        for nb in await client.notebooks.list():
            nb_id = str(getattr(nb,"id",None) or getattr(nb,"notebook_id","") or "")
            nb_title = str(getattr(nb,"title",None) or "Notebook")
            if not nb_id: continue
            sources_out, notes_out = [], []
            try:
                for s in await client.sources.list(nb_id):
                    sources_out.append({"title":str(getattr(s,"title","") or ""),
                        "url":str(getattr(s,"url","") or getattr(s,"source_url","") or ""),
                        "snippet":str(getattr(s,"snippet","") or getattr(s,"description","") or ""),
                        "created_at":str(getattr(s,"created_at","") or "")})
            except: pass
            try:
                for n in await client.notes.list(nb_id):
                    notes_out.append({"title":str(getattr(n,"title","Note") or "Note"),
                        "content":str(getattr(n,"content","") or getattr(n,"text","") or ""),
                        "created_at":str(getattr(n,"created_at","") or "")})
            except: pass
            synced[nb_id] = {"id":nb_id,"title":nb_title,"sources":sources_out,
                             "notes":notes_out,"synced_at":datetime.now(timezone.utc).isoformat()}
    return synced

def _notebooklm_sync_loop():
    import asyncio
    while True:
        try:
            synced = asyncio.run(_sync_notebooks_async())
            _notebook_store.clear(); _notebook_store.update(synced)
            _notebooklm_status.update({"last_sync":datetime.now(timezone.utc).isoformat(),
                                       "notebooks":len(synced),"error":None})
        except Exception as exc: _notebooklm_status["error"] = str(exc)[:200]
        time.sleep(NOTEBOOKLM_SYNC_INTERVAL)

def search_notebooklm(q):
    if not _notebook_store:
        return _empty("notebooklm","Syncing..." if NOTEBOOKLM_AUTH_ARCHIVE else "NOTEBOOKLM_AUTH_1 not set")
    q_lower = (q or "").lstrip("#").lower().strip(); results = []
    for nb_id, nb in _notebook_store.items():
        nb_title = nb.get("title","Notebook"); nb_url = f"https://notebooklm.google.com/notebook/{nb_id}"
        for src in nb.get("sources",[]):
            if not q_lower or q_lower in f"{src.get('title','')} {src.get('url','')} {src.get('snippet','')}".lower():
                results.append(make_doc("notebooklm", src.get("url") or nb_url,
                    src.get("snippet") or "", title=src.get("title") or "(untitled)",
                    author=nb_title, author_url=nb_url, timestamp=src.get("created_at"),
                    meta=f"NotebookLM · {nb_title} · source", source_type="academic"))
        for note in nb.get("notes",[]):
            if not q_lower or q_lower in f"{note.get('title','')} {note.get('content','')}".lower():
                results.append(make_doc("notebooklm", nb_url,
                    note.get("content",""), title=note.get("title","Note"),
                    author=nb_title, author_url=nb_url, timestamp=note.get("created_at"),
                    meta=f"NotebookLM · {nb_title} · note", source_type="academic"))
    return {"platform":"notebooklm","results":results[:50],"error":None}

# ── Platform registry ─────────────────────────────────────────────────────────
API_PLATFORMS = {
    "youtube":search_youtube, "reddit":search_reddit, "bluesky":search_bluesky,
    "mastodon":search_mastodon, "hackernews":search_hackernews, "gnews":search_gnews,
    "telegram":search_telegram, "x":search_sb_twitter, "tiktok":search_sb_tiktok,
    "google":search_google_web, "notebooklm":search_notebooklm,
    "gdelt":search_gdelt, "state_media":search_state_media,
    "academic":search_academic, "podcasts":search_podcasts,
}

# ═══════════════════════════════════════════════════════════════════════════════
# NARRATIVE ENGINE (Phase 2)
# ═══════════════════════════════════════════════════════════════════════════════

def _top_docs(platforms, n=80):
    flat = []
    for group in platforms.values():
        flat.extend(group.get("results",[]) or [])
    flat.sort(key=lambda d: d.get("engagement",0), reverse=True)
    return flat[:n]

def _claude_call(prompt, max_tokens=900):
    if not ANTHROPIC_API_KEY: return None
    try:
        r = requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":max_tokens,
                  "messages":[{"role":"user","content":prompt}]}, timeout=SERPAPI_TIMEOUT)
        if r.status_code >= 400: return None
        return "".join(b.get("text","") for b in r.json().get("content",[]) if b.get("type")=="text")
    except: return None

def extract_narratives_v2(platforms, q, max_posts=80):
    docs = _top_docs(platforms, max_posts)
    if len(docs) < 6: return []
    numbered = "\n".join(
        f"{i+1}. [{d['platform'].upper()}] {((d.get('title') or '')+'  '+(d.get('excerpt') or '')).strip()[:200]}"
        for i,d in enumerate(docs))
    prompt = (
        f"Narrative intelligence analyst. Search: '{q}'.\n"
        "Identify up to 8 distinct NARRATIVE CLUSTERS. A narrative = recurring story/frame/claim-set.\n"
        "For each: label (max 7 words), count, framing (fear|anger|hope|pride|grief|threat|disinformation|neutral), "
        "platforms (list), key_claim (one sentence), actors (list), velocity (accelerating|stable|declining).\n"
        "ONLY JSON array. No prose.\n\n" + numbered)
    text = _claude_call(prompt, 1400)
    if not text: return []
    try:
        arr = json.loads(re.sub(r"```json|```","",text).strip())
        out = []
        for o in arr:
            if not isinstance(o,dict) or not o.get("label"): continue
            out.append({"label":str(o.get("label",""))[:80],"count":int(o.get("count") or 0),
                        "framing":o.get("framing","neutral"),"platforms":o.get("platforms") or [],
                        "key_claim":o.get("key_claim",""),"actors":o.get("actors") or [],
                        "velocity":o.get("velocity","stable")})
        return sorted(out, key=lambda x:x["count"], reverse=True)[:8]
    except: return []

def extract_entities(platforms, q, max_docs=50):
    docs = _top_docs(platforms, max_docs)
    if len(docs) < 5: return {}
    text_blob = "\n".join(
        f"[{d['platform']}] {((d.get('title') or '')+'  '+(d.get('excerpt') or '')).strip()[:180]}"
        for d in docs)
    prompt = (
        f"OSINT entity extraction for: '{q}'.\n"
        "Extract entities (people, orgs, countries, locations, weapons, events) and their relationships.\n"
        'ONLY JSON: {"entities":[{"name":"...","type":"person|org|country|location|weapon|event","mentions":N,"sentiment":"positive|negative|neutral"}],'
        '"edges":[{"from":"...","to":"...","relation":"..."}]}. Max 20 entities, 15 edges. No prose.\n\n' + text_blob)
    text = _claude_call(prompt, 900)
    if not text: return {}
    try: return json.loads(re.sub(r"```json|```","",text).strip())
    except: return {}

def compute_velocity(platforms):
    all_docs = _top_docs(platforms, 500)
    now = datetime.now(timezone.utc)
    windows = {"1h":0,"6h":0,"24h":0,"48h":0,"72h":0,"7d":0}
    hourly = defaultdict(int)
    for doc in all_docs:
        dt = _parse_dt(doc.get("timestamp"))
        if not dt: continue
        diff = now - dt; hours = diff.total_seconds() / 3600
        bucket = int(hours)
        if 0 <= bucket < 168: hourly[bucket] += 1
        for w,h in [("1h",1),("6h",6),("24h",24),("48h",48),("72h",72),("7d",168)]:
            if hours <= h: windows[w] += 1
    recent = windows["6h"]; prior = windows["24h"] - windows["6h"]
    acceleration = "accelerating" if recent > prior*1.3 else "declining" if recent < prior*0.5 else "stable"
    platform_first_seen = {}
    for doc in sorted(all_docs, key=lambda d: d.get("timestamp") or ""):
        p = doc.get("platform","")
        if p and p not in platform_first_seen: platform_first_seen[p] = doc.get("timestamp") or ""
    return {"windows":windows,"acceleration":acceleration,
            "hourly_distribution":dict(sorted(hourly.items())[:24]),
            "platform_first_seen":platform_first_seen,"total_docs":len(all_docs)}

def detect_coordination(platforms):
    all_docs = _top_docs(platforms, 300); signals = []
    def _shingles(text, k=4):
        words = text.lower().split()
        return {" ".join(words[i:i+k]) for i in range(max(0,len(words)-k+1))}
    texts = [(d, _shingles((d.get("title") or "")+" "+(d.get("excerpt") or "")))
             for d in all_docs if (d.get("title") or d.get("excerpt"))]
    dupe_pairs = []
    for i in range(min(len(texts),100)):
        for j in range(i+1,min(len(texts),100)):
            da,sha = texts[i]; db,shb = texts[j]
            if da.get("platform") == db.get("platform"): continue
            if not sha or not shb: continue
            sim = len(sha & shb) / max(len(sha | shb),1)
            if sim > 0.55:
                dupe_pairs.append({"platform_a":da.get("platform"),"url_a":da.get("url"),
                                   "platform_b":db.get("platform"),"url_b":db.get("url"),
                                   "similarity":round(sim,2)})
    if dupe_pairs:
        signals.append({"type":"near_duplicate_cross_platform",
                        "severity":"high" if len(dupe_pairs) > 5 else "medium",
                        "description":f"{len(dupe_pairs)} near-identical posts found across platforms",
                        "examples":dupe_pairs[:5]})
    now = datetime.now(timezone.utc)
    times = sorted([dt for doc in all_docs if (dt:=_parse_dt(doc.get("timestamp")))])
    burst_count = 0
    for i,t in enumerate(times):
        wend = t + timedelta(minutes=30)
        count = sum(1 for t2 in times[i:] if t2 <= wend)
        if count > burst_count: burst_count = count
    if burst_count > 15:
        signals.append({"type":"timing_synchronicity","severity":"medium",
                        "description":f"Peak of {burst_count} posts within 30 minutes"})
    url_platforms = defaultdict(list)
    for doc in all_docs:
        url = doc.get("url",""); plat = doc.get("platform","")
        if url and plat: url_platforms[url].append(plat)
    cross_shared = {url:plats for url,plats in url_platforms.items() if len(set(plats)) > 2}
    if cross_shared:
        signals.append({"type":"cross_platform_url_sharing","severity":"low",
                        "description":f"{len(cross_shared)} URLs shared across 3+ platforms",
                        "examples":list(cross_shared.keys())[:3]})
    coord_score = min(100, len(dupe_pairs)*8 + (burst_count > 15)*20 + len(cross_shared)*5)
    return {"signals":signals,"coordination_score":coord_score,
            "risk":"high" if coord_score > 60 else "medium" if coord_score > 25 else "low",
            "near_duplicate_pairs":len(dupe_pairs),"peak_burst_30min":burst_count,
            "cross_shared_urls":len(cross_shared)}

def trace_propagation(platforms):
    all_docs = _top_docs(platforms, 300)
    platform_earliest = {}
    for doc in all_docs:
        dt = _parse_dt(doc.get("timestamp"))
        if not dt: continue
        p = doc.get("platform","unknown")
        if p not in platform_earliest or dt < platform_earliest[p]: platform_earliest[p] = dt
    if not platform_earliest: return {"origin":None,"propagation_chain":[],"spread_hours":None}
    sorted_p = sorted(platform_earliest.items(), key=lambda kv:kv[1])
    origin = sorted_p[0][0]
    chain = []
    for i,(plat,dt) in enumerate(sorted_p):
        prev_dt = sorted_p[i-1][1] if i > 0 else dt
        lag = round((dt-prev_dt).total_seconds()/3600,1) if i > 0 else 0
        chain.append({"platform":plat,"first_seen":dt.isoformat(),"lag_hours":lag})
    spread = round((sorted_p[-1][1]-sorted_p[0][1]).total_seconds()/3600,1) if len(sorted_p)>1 else None
    return {"origin":origin,"propagation_chain":chain,"spread_hours":spread,"platforms_reached":len(chain)}

# ── Sentiment + framing ───────────────────────────────────────────────────────
BABEL_CAP=24; BABEL_WORKERS=12; BABEL_BUDGET=12; BABEL_TIMEOUT=10
_SCORE={"positive":1.0,"neutral":0.0,"negative":-1.0}

def _norm_label(raw):
    lab = str(raw or "").lower()
    if "pos" in lab: return "positive"
    if "neg" in lab: return "negative"
    return "neutral"

def _babel_one(text):
    try:
        r = requests.post("https://analytics.babelstreet.com/rest/v1/sentiment",
            headers={"X-BabelStreetAPI-Key":BABELSTREET_API_KEY,"Content-Type":"application/json","Accept":"application/json"},
            json={"content":text[:3500]}, timeout=BABEL_TIMEOUT)
        if r.status_code >= 400: return None
        body = r.json()
        if not isinstance(body,dict): return None
        doc = body.get("document") or (body.get("sentiment") or {}).get("document") or {}
        return _norm_label(doc.get("label")) if doc.get("label") is not None else None
    except: return None

def _sentiment_babelstreet(texts, indices):
    if not BABELSTREET_API_KEY or not indices: return {}
    out = {}
    with ThreadPoolExecutor(max_workers=BABEL_WORKERS) as ex:
        futs = {ex.submit(_babel_one,texts[i]):i for i in indices}
        try:
            for fut in as_completed(list(futs.keys()),timeout=BABEL_BUDGET):
                lab = fut.result()
                if lab: out[futs[fut]] = lab
        except: pass
    return out

def _sentiment_claude(texts, lang="en"):
    """
    Analyse sentiment + framing IN THE SOURCE LANGUAGE.
    Translating first destroys exactly the connotative signal we're hunting for,
    so the model is instructed to reason natively in the source language.
    """
    if not ANTHROPIC_API_KEY or not texts: return None, None
    batch = texts[:120]
    numbered = "\n".join(f"{i+1}. {t[:240]}" for i,t in enumerate(batch))
    meta = LANG_META.get(lang, {})
    lang_name = meta.get("name", "English")
    if lang == "en":
        lang_instruction = ""
    else:
        lang_instruction = (
            f"\nThese posts are in {lang_name}. Analyse them NATIVELY in {lang_name} — "
            "do NOT mentally translate to English first. Judge connotation, register, "
            "religious/political idiom, and euphemism as a native reader of "
            f"{lang_name} media would. Output labels in English.\n"
        )
    prompt = (
        "For each numbered post classify:\n"
        "1. SENTIMENT: positive|neutral|negative\n"
        "2. FRAMING: fear|anger|hope|pride|grief|threat|disinformation|neutral\n"
        + lang_instruction +
        'ONLY JSON array: [{"s":"...","f":"..."},...] one per post. No prose.\n\n' + numbered)
    text = _claude_call(prompt, 2400)
    if not text: return None, None
    try:
        arr = json.loads(re.sub(r"```json|```","",text).strip())
        sentiments = [_norm_label(v.get("s")) for v in arr]
        framings   = [str(v.get("f","neutral")).lower() for v in arr]
        while len(sentiments) < len(texts): sentiments.append("neutral")
        while len(framings) < len(texts): framings.append("neutral")
        return sentiments[:len(texts)], framings[:len(texts)]
    except: return None, None

def attach_sentiment(platforms):
    """
    Language-aware sentiment. Documents are grouped by detected language and
    each group is analysed in its own language, in parallel.
    """
    flat = []
    for group in platforms.values():
        for r in group.get("results",[]):
            txt = ((r.get("title") or "")+" "+(r.get("excerpt") or "")).strip()
            if txt: flat.append((r,txt))
    if not flat:
        return {"scored":0,"positive":0,"neutral":0,"negative":0,"net":None,
                "engines":[],"agreement":None,"babel_scored":0,"framing_counts":{},
                "by_language":{}}

    # Group indices by language
    lang_groups: dict[str, list] = defaultdict(list)
    for i, (r, _) in enumerate(flat):
        lang_groups[r.get("language", "en")].append(i)

    texts = [t for _, t in flat]
    engines = []
    claude_s: list = [None] * len(flat)
    claude_f: list = [None] * len(flat)

    def _run_lang(lang: str, indices: list):
        sub_texts = [texts[i] for i in indices]
        s, f = _sentiment_claude(sub_texts, lang)
        if not s: return
        for local_i, global_i in enumerate(indices):
            if local_i < len(s): claude_s[global_i] = s[local_i]
            if f and local_i < len(f): claude_f[global_i] = f[local_i]

    if ANTHROPIC_API_KEY:
        with ThreadPoolExecutor(max_workers=5) as ex:
            futs = [ex.submit(_run_lang, lang, idxs)
                    for lang, idxs in sorted(lang_groups.items(),
                                             key=lambda kv: len(kv[1]), reverse=True)[:6]]
            for fut in futs:
                try: fut.result(timeout=30)
                except Exception: pass
        if any(x is not None for x in claude_s): engines.append("claude")

    order = sorted(range(len(flat)), key=lambda i:flat[i][0].get("engagement",0), reverse=True)
    babel = _sentiment_babelstreet(texts, order[:BABEL_CAP])
    if babel: engines.append("babelstreet")

    counts={"positive":0,"neutral":0,"negative":0}; framing_counts=defaultdict(int)
    by_language: dict[str, dict] = defaultdict(lambda: {"positive":0,"neutral":0,"negative":0,"scored":0})
    net_sum=0.0; scored=agree_n=agree_d=0
    for i,(r,_) in enumerate(flat):
        c = claude_s[i]; f = claude_f[i]; b = babel.get(i)
        if c and b:
            agree_d+=1; agree_n+=(1 if c==b else 0)
            score = (_SCORE[c]+_SCORE[b])/2.0
            final = "positive" if score>0.25 else "negative" if score<-0.25 else "neutral"
            r["s_claude"]=c; r["s_babel"]=b
        elif c: score,final=_SCORE[c],c; r["s_claude"]=c
        elif b: score,final=_SCORE[b],b; r["s_babel"]=b
        else: continue
        r["sentiment"]=final
        if f: r["framing"]=f; framing_counts[f]+=1
        counts[final]+=1; net_sum+=score; scored+=1
        lg = r.get("language","en")
        by_language[lg][final]+=1; by_language[lg]["scored"]+=1

    return {**counts,"scored":scored,"net":round(net_sum/scored,2) if scored else None,
            "engines":engines,"agreement":round(agree_n/agree_d,2) if agree_d else None,
            "babel_scored":len(babel),"framing_counts":dict(framing_counts),
            "by_language":{k:dict(v) for k,v in by_language.items()}}

def _build_aggregates(platforms):
    source_mix=[]; total=with_results=0; reactions=comments=shares=0
    state_media=academic=news=social=0; searched=len(platforms)
    for pid,group in platforms.items():
        results=group.get("results",[]) or []; n=len(results)
        if n: with_results+=1; total+=n
        for r in results:
            eb=_engagement_breakdown(r.get("meta"))
            reactions+=eb["reactions"]; comments+=eb["comments"]; shares+=eb["shares"]
            st=r.get("source_type","social")
            if st=="state_media": state_media+=1
            elif st=="academic": academic+=1
            elif st=="news": news+=1
            else: social+=1
        source_mix.append({"platform":pid,"count":n})
    return {"totals":{"mentions":total,"engagement":reactions+comments+shares,
                      "reactions":reactions,"comments":comments,"shares":shares,
                      "platforms_with_results":with_results,"platforms_searched":searched,
                      "state_media":state_media,"academic":academic,"news":news,"social":social},
            "source_mix":sorted([s for s in source_mix if s["count"]>0],key=lambda s:s["count"],reverse=True)}

# ═══════════════════════════════════════════════════════════════════════════════
# WATCHLISTS & ALERTS
# NOTE: This is an in-process store. It survives until the Railway dyno restarts
# (i.e. until the next deploy). Watchlist definitions are ALSO persisted client-side
# in localStorage, which is the durable copy. Background alerting between sessions
# would need a real DB + worker — this evaluates rules on demand.
# ═══════════════════════════════════════════════════════════════════════════════

_watchlists: dict[str, dict] = {}
_watch_lock = threading.Lock()

DEFAULT_RULES = {
    "coordination_above": 60,     # alert if coordination score exceeds
    "velocity_accelerating": True, # alert if narrative volume accelerating
    "mentions_above": None,        # alert if raw mention count exceeds
    "state_media_above": None,     # alert if state media pickup exceeds
    "new_narrative": True,         # alert if a narrative cluster appears that wasn't there before
}

def evaluate_watchlist(query: str, rules: dict, baseline: dict | None = None) -> dict:
    """
    Run a search for the query and evaluate alert rules against the result.
    Returns triggered alerts with evidence.
    """
    payload = _run_full_search(query)
    alerts = []

    coord = payload.get("coordination") or {}
    vel = payload.get("velocity") or {}
    totals = payload.get("totals") or {}
    narratives = payload.get("narratives") or []

    thresh = rules.get("coordination_above")
    if thresh is not None and coord.get("coordination_score", 0) >= thresh:
        alerts.append({
            "type": "coordination_spike",
            "severity": "high" if coord.get("coordination_score", 0) >= 75 else "medium",
            "message": f"Coordination score {coord.get('coordination_score')}/100 "
                       f"({coord.get('risk','?')} risk) — threshold was {thresh}",
            "evidence": coord.get("signals", [])[:3],
        })

    if rules.get("velocity_accelerating") and vel.get("acceleration") == "accelerating":
        w = vel.get("windows", {})
        alerts.append({
            "type": "velocity_acceleration",
            "severity": "medium",
            "message": f"Narrative volume accelerating — {w.get('6h',0)} posts in last 6h "
                       f"vs {max(w.get('24h',0)-w.get('6h',0),0)} in prior 18h",
            "evidence": [],
        })

    m_thresh = rules.get("mentions_above")
    if m_thresh is not None and totals.get("mentions", 0) >= m_thresh:
        alerts.append({
            "type": "volume_spike",
            "severity": "medium",
            "message": f"{totals.get('mentions')} mentions — threshold was {m_thresh}",
            "evidence": [],
        })

    sm_thresh = rules.get("state_media_above")
    if sm_thresh is not None and totals.get("state_media", 0) >= sm_thresh:
        alerts.append({
            "type": "state_media_pickup",
            "severity": "high",
            "message": f"{totals.get('state_media')} posts from state/adversary media "
                       f"— threshold was {sm_thresh}",
            "evidence": [],
        })

    if rules.get("new_narrative") and baseline:
        old_labels = {n.get("label","").lower() for n in (baseline.get("narratives") or [])}
        new_ones = [n for n in narratives if n.get("label","").lower() not in old_labels]
        if new_ones:
            alerts.append({
                "type": "new_narrative",
                "severity": "medium",
                "message": f"{len(new_ones)} new narrative cluster(s) detected since last check",
                "evidence": [{"label": n.get("label"), "framing": n.get("framing"),
                              "count": n.get("count")} for n in new_ones[:4]],
            })

    return {
        "query": query,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "alerts": alerts,
        "alert_count": len(alerts),
        "snapshot": {
            "mentions": totals.get("mentions", 0),
            "coordination_score": coord.get("coordination_score", 0),
            "acceleration": vel.get("acceleration"),
            "state_media": totals.get("state_media", 0),
            "narratives": [{"label": n.get("label"), "framing": n.get("framing"),
                            "count": n.get("count")} for n in narratives],
        },
    }


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — REPORT / DOSSIER GENERATION
# ═══════════════════════════════════════════════════════════════════════════════

def build_dossier(payload: dict, brief_text: str | None = None) -> dict:
    """
    Assemble a structured intelligence dossier from a search payload.
    Returns a dict ready for rendering to HTML/PDF or export as JSON.
    """
    q = payload.get("query", "")
    totals = payload.get("totals") or {}
    sentiment = payload.get("sentiment") or {}
    narratives = payload.get("narratives") or []
    entities = (payload.get("entities") or {}).get("entities") or []
    edges = (payload.get("entities") or {}).get("edges") or []
    coord = payload.get("coordination") or {}
    vel = payload.get("velocity") or {}
    prop = payload.get("propagation") or {}
    langs = payload.get("languages") or {}
    source_mix = payload.get("source_mix") or []

    # Top evidence: highest-engagement docs per platform
    evidence = []
    for pid, group in (payload.get("platforms") or {}).items():
        results = sorted(group.get("results", []) or [],
                         key=lambda d: d.get("engagement", 0), reverse=True)
        for doc in results[:3]:
            evidence.append({
                "platform": pid,
                "source_type": doc.get("source_type"),
                "credibility": doc.get("credibility"),
                "language": doc.get("language"),
                "author": doc.get("author"),
                "title": doc.get("title_en") or doc.get("title"),
                "excerpt": doc.get("excerpt_en") or doc.get("excerpt"),
                "url": doc.get("url"),
                "timestamp": doc.get("timestamp"),
                "sentiment": doc.get("sentiment"),
                "framing": doc.get("framing"),
                "engagement": doc.get("engagement", 0),
                "translated": doc.get("translated", False),
            })
    evidence.sort(key=lambda d: d.get("engagement", 0), reverse=True)

    # Confidence assessment — honest about what we actually have
    confidence_factors = []
    if totals.get("mentions", 0) < 20:
        confidence_factors.append(("LOW sample size", f"only {totals.get('mentions',0)} documents retrieved"))
    if totals.get("platforms_with_results", 0) < 4:
        confidence_factors.append(("Narrow source base", f"{totals.get('platforms_with_results',0)} platforms returned data"))
    if not sentiment.get("scored"):
        confidence_factors.append(("No sentiment scoring", "sentiment engine unavailable"))
    if sentiment.get("agreement") is not None and sentiment["agreement"] < 0.6:
        confidence_factors.append(("Engine disagreement", f"sentiment engines agree only {int(sentiment['agreement']*100)}% of the time"))
    if not narratives:
        confidence_factors.append(("No narrative clusters", "insufficient volume for clustering"))

    overall = "HIGH" if not confidence_factors else \
              "MODERATE" if len(confidence_factors) <= 2 else "LOW"

    return {
        "query": q,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "classification": "OSINT — OPEN SOURCE",
        "executive_brief": brief_text,
        "confidence": {"overall": overall, "caveats": confidence_factors},
        "totals": totals,
        "sentiment": sentiment,
        "languages": langs,
        "narratives": narratives,
        "entities": entities[:25],
        "entity_edges": edges[:20],
        "coordination": coord,
        "velocity": vel,
        "propagation": prop,
        "source_mix": source_mix,
        "evidence": evidence[:40],
    }


def generate_brief(q, snippets, narratives, entities, coordination):
    if not ANTHROPIC_API_KEY: return {"brief":None,"reason":"ANTHROPIC_API_KEY not set"}
    NL=chr(10)
    ctx=NL.join("- "+str(s).replace(NL," ")[:220] for s in snippets[:20] if s)
    narr_ctx=""
    if narratives:
        narr_ctx="\nNARRATIVES:\n"+"\n".join(
            f"- [{n.get('framing','').upper()}] {n.get('label','')} (x{n.get('count',0)}): {n.get('key_claim','')}"
            for n in narratives[:5])
    coord_ctx=""
    if coordination.get("coordination_score",0)>25:
        coord_ctx=f"\nCOORDINATION SIGNAL: score {coordination['coordination_score']}/100 ({coordination.get('risk','?')} risk)"
    ent_ctx=""
    if entities.get("entities"):
        top_ents=sorted(entities["entities"],key=lambda e:e.get("mentions",0),reverse=True)[:8]
        ent_ctx="\nKEY ENTITIES: "+", ".join(f"{e['name']} ({e['type']})" for e in top_ents)
    prompt=(f"Senior intelligence analyst. Query: '{q}'\n\n"
            +("REAL POSTS:\n"+ctx+"\n\n" if ctx else "")
            +(narr_ctx+"\n" if narr_ctx else "")+(ent_ctx+"\n" if ent_ctx else "")
            +(coord_ctx+"\n" if coord_ctx else "")
            +"\nStructured brief:\n"
            "**SITUATION**: 1 sentence.\n"
            "**KEY ACTORS**: Who is involved.\n"
            "**DOMINANT NARRATIVE**: Main story and framing.\n"
            "**COORDINATION RISK**: Signs of inauthentic behaviour (only if relevant).\n"
            "**ASSESSMENT**: 1-2 sentence significance.\n\n"
            "Bold key entities. English only. Output only the brief.")
    try:
        r=requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":700,
                  "messages":[{"role":"user","content":prompt}]}, timeout=SERPAPI_TIMEOUT)
        if r.status_code >= 400:
            reason="Intelligence brief unavailable."
            try:
                err=r.json().get("error") or {}; emsg=err.get("message","")
                if "credit" in emsg.lower() or "billing" in emsg.lower(): reason="API credit balance empty."
                elif emsg: reason=f"Brief unavailable: {emsg[:140]}"
            except: pass
            return {"brief":None,"reason":reason}
        brief="".join(b.get("text","") for b in r.json().get("content",[]) if b.get("type")=="text").strip()
        return {"brief":brief} if brief else {"brief":None,"reason":"Empty response"}
    except Exception as e: return {"brief":None,"reason":f"Request failed: {str(e)[:100]}"}

# ═══════════════════════════════════════════════════════════════════════════════
# FLASK ROUTES
# ═══════════════════════════════════════════════════════════════════════════════

@app.route("/")
def index():
    resp = make_response(render_template("index.html",
        youtube_enabled=bool(YOUTUBE_API_KEY),
        cse_enabled=bool(SERPAPI_KEY),
        sentiment_enabled=SENTIMENT_ENABLED))
    resp.headers["X-Content-Type-Options"]="nosniff"; resp.headers["X-Frame-Options"]="DENY"
    resp.headers["Referrer-Policy"]="strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"]="geolocation=(), microphone=(), camera=()"
    return resp

def _run_full_search(q: str, use_cache: bool = True) -> dict:
    """Core search + analysis pipeline. Used by /api/search and watchlist checks."""
    cache_key=q.lower(); now=time.time()
    if use_cache and cache_key in _cache:
        ts,cached=_cache[cache_key]
        if now-ts<CACHE_TTL: return {**cached,"cached":True}
    direct_out={}; cse_out={}
    with ThreadPoolExecutor(max_workers=len(API_PLATFORMS)+1) as ex:
        futures={ex.submit(fn,q):name for name,fn in API_PLATFORMS.items()}
        cse_future=ex.submit(search_serpapi,q)
        for fut in as_completed(list(futures.keys())+[cse_future],timeout=SERPAPI_TIMEOUT+10):
            if fut is cse_future:
                try: cse_out=fut.result()
                except Exception as e: cse_out={p:_empty(p,str(e)[:120]) for p in SERPAPI_PLATFORM_DOMAINS}
            else:
                name=futures[fut]
                try: direct_out[name]=fut.result()
                except Exception as e: direct_out[name]=_empty(name,str(e)[:120])
    out={}
    for pid in set(direct_out.keys())|set(cse_out.keys()):
        direct=direct_out.get(pid); cse=cse_out.get(pid)
        if direct and cse:
            existing_urls={r.get("url") for r in direct.get("results",[]) if r.get("url")}
            cse_extra=[r for r in cse.get("results",[]) if r.get("url") and r["url"] not in existing_urls]
            merged=direct.get("results",[])+cse_extra
            out[pid]={"platform":pid,"results":merged,"error":None if merged else (direct.get("error") or cse.get("error"))}
        else: out[pid]=direct or cse
    for group in out.values():
        for r in group.get("results",[]):
            eb=_engagement_breakdown(r.get("meta")); r["engagement"]=eb["reactions"]+eb["comments"]+eb["shares"]

    # PHASE 3: Detect language + translate for display BEFORE sentiment,
    # so sentiment can be run natively per-language.
    languages={"distribution":[],"languages_detected":0,"non_english_docs":0,"total_docs":0}
    try: languages=enrich_languages(out)
    except Exception as e: app.logger.warning("language enrichment failed: %s",e)

    sentiment={"scored":0,"positive":0,"neutral":0,"negative":0,"net":None,"engines":[],
               "agreement":None,"babel_scored":0,"framing_counts":{},"by_language":{}}
    if SENTIMENT_ENABLED:
        try: sentiment=attach_sentiment(out)
        except Exception as e: app.logger.warning("sentiment failed: %s",e); sentiment["error"]=str(e)[:120]

    narratives=[]; entities={}; velocity={}; coordination={}; propagation={}
    with ThreadPoolExecutor(max_workers=5) as ex:
        f_narr=ex.submit(extract_narratives_v2,out,q)
        f_ents=ex.submit(extract_entities,out,q)
        f_vel=ex.submit(compute_velocity,out)
        f_coord=ex.submit(detect_coordination,out)
        f_prop=ex.submit(trace_propagation,out)
        try: narratives=f_narr.result(timeout=25)
        except: pass
        try: entities=f_ents.result(timeout=20)
        except: pass
        try: velocity=f_vel.result(timeout=5)
        except: pass
        try: coordination=f_coord.result(timeout=10)
        except: pass
        try: propagation=f_prop.result(timeout=5)
        except: pass
    agg=_build_aggregates(out)
    payload={"query":q,"platforms":out,"sentiment":sentiment,"narratives":narratives,
             "entities":entities,"velocity":velocity,"coordination":coordination,
             "propagation":propagation,"languages":languages,
             "totals":agg["totals"],"source_mix":agg["source_mix"],"cached":False}
    _cache[cache_key]=(now,payload)
    if len(_cache)>200:
        oldest=sorted(_cache.items(),key=lambda kv:kv[1][0])[:50]
        for k,_ in oldest: _cache.pop(k,None)
    return payload


@app.route("/api/search")
def api_search():
    q=(request.args.get("q") or "").strip()
    if not q: return jsonify({"error":"missing q"}),400
    if len(q)>200: return jsonify({"error":"query too long"}),400
    return jsonify(_run_full_search(q))


# ── Watchlist endpoints ───────────────────────────────────────────────────────
@app.route("/api/watchlist", methods=["GET"])
def api_watchlist_list():
    with _watch_lock:
        return jsonify({"watchlists": list(_watchlists.values())})

@app.route("/api/watchlist", methods=["POST"])
def api_watchlist_add():
    body = request.get_json(silent=True) or {}
    q = (body.get("query") or "").strip()
    if not q: return jsonify({"error": "missing query"}), 400
    rules = {**DEFAULT_RULES, **(body.get("rules") or {})}
    wl_id = hashlib.sha256(q.lower().encode()).hexdigest()[:12]
    entry = {
        "id": wl_id, "query": q, "rules": rules,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "last_checked": None, "last_snapshot": None, "last_alerts": [],
    }
    with _watch_lock:
        _watchlists[wl_id] = entry
    return jsonify(entry)

@app.route("/api/watchlist/<wl_id>", methods=["DELETE"])
def api_watchlist_delete(wl_id):
    with _watch_lock:
        _watchlists.pop(wl_id, None)
    return jsonify({"deleted": wl_id})

@app.route("/api/watchlist/<wl_id>/check", methods=["POST"])
def api_watchlist_check(wl_id):
    with _watch_lock:
        entry = _watchlists.get(wl_id)
    if not entry:
        # Allow ad-hoc check by passing query + rules directly
        body = request.get_json(silent=True) or {}
        q = (body.get("query") or "").strip()
        if not q: return jsonify({"error": "watchlist not found"}), 404
        rules = {**DEFAULT_RULES, **(body.get("rules") or {})}
        baseline = body.get("baseline")
        return jsonify(evaluate_watchlist(q, rules, baseline))
    result = evaluate_watchlist(entry["query"], entry["rules"], entry.get("last_snapshot"))
    with _watch_lock:
        entry["last_checked"] = result["checked_at"]
        entry["last_snapshot"] = result["snapshot"]
        entry["last_alerts"] = result["alerts"]
    return jsonify(result)

@app.route("/api/watchlist/check-all", methods=["POST"])
def api_watchlist_check_all():
    """Evaluate every watchlist. Returns only those with triggered alerts."""
    body = request.get_json(silent=True) or {}
    # Accept client-side watchlists (localStorage is the durable copy)
    client_wls = body.get("watchlists") or []
    targets = []
    with _watch_lock:
        targets.extend(list(_watchlists.values()))
    seen = {t["query"].lower() for t in targets}
    for cw in client_wls:
        if cw.get("query") and cw["query"].lower() not in seen:
            targets.append({"id": cw.get("id"), "query": cw["query"],
                            "rules": {**DEFAULT_RULES, **(cw.get("rules") or {})},
                            "last_snapshot": cw.get("last_snapshot")})
    results = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(evaluate_watchlist, t["query"], t["rules"], t.get("last_snapshot")): t
                for t in targets[:10]}
        for fut in as_completed(futs, timeout=120):
            t = futs[fut]
            try:
                r = fut.result()
                r["watchlist_id"] = t.get("id")
                results.append(r)
            except Exception as e:
                results.append({"watchlist_id": t.get("id"), "query": t["query"],
                                "error": str(e)[:120], "alerts": [], "alert_count": 0})
    total_alerts = sum(r.get("alert_count", 0) for r in results)
    return jsonify({"results": results, "total_alerts": total_alerts,
                    "checked": len(results),
                    "checked_at": datetime.now(timezone.utc).isoformat()})


# ── Report / dossier endpoints ────────────────────────────────────────────────
@app.route("/api/dossier", methods=["POST"])
def api_dossier():
    """Return a structured dossier JSON for a query."""
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    if not q: return jsonify({"error": "missing q"}), 400
    payload = _run_full_search(q)
    brief_text = body.get("brief")
    if not brief_text and ANTHROPIC_API_KEY:
        snippets = []
        for group in (payload.get("platforms") or {}).values():
            for r in (group.get("results") or [])[:3]:
                t = ((r.get("title_en") or r.get("title") or "") + " " +
                     (r.get("excerpt_en") or r.get("excerpt") or "")).strip()
                if t: snippets.append(t)
        br = generate_brief(q, snippets[:20], payload.get("narratives") or [],
                            payload.get("entities") or {}, payload.get("coordination") or {})
        brief_text = br.get("brief")
    return jsonify(build_dossier(payload, brief_text))


@app.route("/report")
def report_view():
    """Printable HTML dossier. Opens in a new tab; user prints to PDF."""
    q = (request.args.get("q") or "").strip()
    if not q:
        return "<p style='font-family:sans-serif;padding:40px'>Pass ?q=your+query</p>", 400
    payload = _run_full_search(q)
    brief_text = None
    if ANTHROPIC_API_KEY:
        snippets = []
        for group in (payload.get("platforms") or {}).values():
            for r in (group.get("results") or [])[:3]:
                t = ((r.get("title_en") or r.get("title") or "") + " " +
                     (r.get("excerpt_en") or r.get("excerpt") or "")).strip()
                if t: snippets.append(t)
        br = generate_brief(q, snippets[:20], payload.get("narratives") or [],
                            payload.get("entities") or {}, payload.get("coordination") or {})
        brief_text = br.get("brief")
    d = build_dossier(payload, brief_text)
    return render_template("report.html", d=d)

@app.route("/api/brief",methods=["POST"])
def api_brief():
    body=request.get_json(silent=True) or {}
    q=(body.get("q") or "").strip(); snippets=body.get("snippets") or []
    narratives=body.get("narratives") or []; entities=body.get("entities") or {}
    coordination=body.get("coordination") or {}
    if not q: return jsonify({"error":"missing q"}),400
    if not ANTHROPIC_API_KEY: return jsonify({"brief":None,"reason":"ANTHROPIC_API_KEY needed"}),200
    cache_key="__brief__"+q.lower(); now=time.time()
    if cache_key in _cache:
        ts,cached=_cache[cache_key]
        if now-ts<CACHE_TTL: return jsonify({**cached,"cached":True})
    result=generate_brief(q,snippets,narratives,entities,coordination)
    if result.get("brief"): _cache[cache_key]=(now,result)
    return jsonify(result)

@app.route("/api/status")
def api_status():
    return jsonify({"sources":{"youtube":bool(YOUTUBE_API_KEY),"serpapi":bool(SERPAPI_KEY),
                               "scrapebadger":bool(SCRAPEBADGER_KEY),"anthropic":bool(ANTHROPIC_API_KEY),
                               "babelstreet":bool(BABELSTREET_API_KEY),
                               "bluesky":bool(BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD),
                               "podcast_index":bool(PODCAST_INDEX_KEY and PODCAST_INDEX_SECRET),
                               "gdelt":True,"state_media":True,"academic":True,
                               "telegram":bool(TELEGRAM_CHANNELS),"notebooklm":bool(NOTEBOOKLM_AUTH_ARCHIVE)},
                    "narrative_engine":"active","version":"2.0",
                    "telegram_channels":len(TELEGRAM_CHANNELS),
                    "state_media_feeds":len(ADVERSARY_RSS_FEEDS),
                    "podcast_watchlist":len(PODCAST_WATCHLIST)})

@app.route("/api/kb/chat",methods=["POST"])
def api_kb_chat():
    body=request.get_json(silent=True) or {}
    q=(body.get("q") or "").strip(); history=body.get("history") or []
    if not q: return jsonify({"error":"missing q"}),400
    if not ANTHROPIC_API_KEY: return jsonify({"answer":"Knowledge Bank needs ANTHROPIC_API_KEY.","sources":[]}),200
    NL=chr(10); matches=[]; q_lower=q.lower(); q_words=[w for w in q_lower.split() if len(w)>3]
    if _notebook_store:
        for nb_id,nb in _notebook_store.items():
            nb_title=nb.get("title","Unknown")
            for src in nb.get("sources",[]):
                text=(src.get("title","")+" "+src.get("snippet","")).strip()
                if q_lower in text.lower() or any(w in text.lower() for w in q_words):
                    matches.append({"nb":nb_title,"title":src.get("title",""),"text":text[:400],"url":src.get("url","")})
            for note in nb.get("notes",[]):
                text=(note.get("title","")+" "+note.get("content","")).strip()
                if q_lower in text.lower() or any(w in text.lower() for w in q_words):
                    matches.append({"nb":nb_title,"title":note.get("title","Note"),"text":text[:400],"url":""})
    if not matches and _notebook_store:
        for nb_id,nb in list(_notebook_store.items())[:3]:
            for src in list(nb.get("sources",[]))[:3]:
                text=(src.get("title","")+" "+src.get("snippet","")).strip()
                matches.append({"nb":nb.get("title","Unknown"),"title":src.get("title",""),"text":text[:200],"url":src.get("url","")})
    ctx=NL.join("- ["+m["nb"]+"] "+m["title"]+": "+m["text"] for m in matches[:15]) or "No relevant content."
    msgs=[h for h in history[-5:] if h.get("role") in ("user","assistant")]
    if not msgs or msgs[-1]["role"] != "user": msgs.append({"role":"user","content":q})
    system=("Expert intelligence analyst — Hezbollah Knowledge Bank.\n"+
            "Relevant content:\n\n"+ctx+"\n\nAnswer based on notebooks. Bold key entities. English only.")
    try:
        r=requests.post("https://api.anthropic.com/v1/messages",
            headers={"x-api-key":ANTHROPIC_API_KEY,"anthropic-version":"2023-06-01","content-type":"application/json"},
            json={"model":"claude-haiku-4-5-20251001","max_tokens":600,"system":system,"messages":msgs},
            timeout=SERPAPI_TIMEOUT)
        if r.status_code >= 400: return jsonify({"answer":"Knowledge Bank unavailable.","sources":[]}),200
        answer="".join(b.get("text","") for b in r.json().get("content",[]) if b.get("type")=="text").strip()
        sources=[{"nb":m["nb"],"title":m["title"],"text":m["text"][:200],"url":m["url"]} for m in matches[:5]]
        return jsonify({"answer":answer,"sources":sources})
    except: return jsonify({"answer":"Knowledge Bank unavailable.","sources":[]}),200

@app.route("/api/notebooklm/status")
def notebooklm_status():
    chunks=sum(1 for i in range(1,20) if os.environ.get(f"NOTEBOOKLM_AUTH_{i}"))
    return jsonify({"configured":chunks>0,"auth_chunks":chunks,"notebooks":_notebooklm_status["notebooks"],
                    "titles":[nb.get("title") for nb in _notebook_store.values()],
                    "last_sync":_notebooklm_status["last_sync"],"error":_notebooklm_status["error"],
                    "interval_min":NOTEBOOKLM_SYNC_INTERVAL//60})

def _debug_auth():
    tok=os.environ.get("DEBUG_TOKEN","").strip()
    return not tok or request.headers.get("X-Debug-Token")==tok

@app.route("/debug/brief")
def debug_brief():
    if not _debug_auth(): return {"error":"auth required"},401
    q=(request.args.get("q") or "").strip()
    if not q: return {"error":"pass ?q="},400
    if not ANTHROPIC_API_KEY: return {"error":"ANTHROPIC_API_KEY not set"},200
    text=_claude_call(f'Write a 2-3 sentence OSINT brief on: "{q}". Bold key entities. English only.',350)
    return {"brief":text,"key_last4":ANTHROPIC_API_KEY[-4:]},200

@app.route("/debug/gdelt")
def debug_gdelt():
    q=(request.args.get("q") or "test").strip(); result=search_gdelt(q)
    return jsonify({"count":len(result.get("results",[])),"error":result.get("error"),
                    "sample":(result.get("results") or [None])[0]})

@app.route("/debug/state_media")
def debug_state_media():
    q=(request.args.get("q") or "hezbollah").strip(); result=search_state_media(q)
    return jsonify({"count":len(result.get("results",[])),"error":result.get("error"),
                    "sample":(result.get("results") or [None])[0]})

@app.route("/debug/academic")
def debug_academic():
    q=(request.args.get("q") or "information operations").strip(); result=search_academic(q)
    return jsonify({"count":len(result.get("results",[])),"error":result.get("error"),
                    "sample":(result.get("results") or [None])[0]})

@app.route("/debug/tiktok")
def debug_tiktok():
    if not _debug_auth(): return {"error":"auth required"},401
    q=(request.args.get("q") or "news").strip()
    return jsonify({"parsed":len(search_sb_tiktok(q).get("results",[]))})

@app.route("/debug/serpapi")
def debug_serpapi():
    if not _debug_auth(): return {"error":"auth required"},401
    if not SERPAPI_KEY: return {"set":False},200
    q=(request.args.get("q") or "test").strip()
    try:
        r=requests.get("https://serpapi.com/search",
            params={"engine":"google","q":f"{q} site:x.com","num":3,"api_key":SERPAPI_KEY},
            timeout=SERPAPI_TIMEOUT)
        body=r.json()
        return {"key_last4":SERPAPI_KEY[-4:],"status":r.status_code,"results":len(body.get("organic_results",[]))},200
    except Exception as e: return {"error":str(e)[:200]},500

@app.route("/debug/scrapebadger")
def debug_scrapebadger():
    if not _debug_auth(): return {"error":"auth required"},401
    if not SCRAPEBADGER_KEY: return {"set":False},200
    try:
        acct=requests.get(f"{SB_BASE}/account",headers={"x-api-key":SCRAPEBADGER_KEY},timeout=SERPAPI_TIMEOUT)
        return {"key_last4":SCRAPEBADGER_KEY[-4:],"status":acct.status_code,"account":acct.json() if acct.ok else None},200
    except Exception as e: return {"error":str(e)[:200]},500

@app.route("/debug/account")
def debug_account():
    if not _debug_auth(): return {"error":"auth required"},401
    if not SERPAPI_KEY: return {"error":"SERPAPI_KEY not set"},200
    try:
        r=requests.get("https://serpapi.com/account",params={"api_key":SERPAPI_KEY},timeout=SERPAPI_TIMEOUT)
        return r.json(),r.status_code
    except Exception as e: return {"error":str(e)[:160]},500

if NOTEBOOKLM_AUTH_ARCHIVE and _restore_notebooklm_auth():
    threading.Thread(target=_notebooklm_sync_loop,daemon=True,name="notebooklm-sync").start()

if __name__ == "__main__":
    app.run(host="0.0.0.0",port=8000,debug=True)
