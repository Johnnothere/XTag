"""XTag — cross-platform hashtag & keyword search aggregator.

Backend fetches results in parallel from:
- Direct APIs: YouTube, Reddit, Bluesky, Mastodon, Hacker News, Google News RSS
- Google Custom Search (site-restricted): X, Instagram, TikTok, Facebook,
  LinkedIn, Pinterest, Threads, Tumblr — platforms without public APIs.

Returns a unified feed with per-platform badges.
"""
from __future__ import annotations

import base64
import html
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from urllib.parse import quote_plus, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template, request, make_response

app = Flask(__name__)

YOUTUBE_API_KEY = os.environ.get("YOUTUBE_API_KEY", "").strip()
SERPAPI_KEY = os.environ.get("SERPAPI_KEY", "").strip()
SCRAPEBADGER_KEY = os.environ.get("SCRAPEBADGER_KEY", "").strip()
SB_BASE = "https://scrapebadger.com/v1"
# Sentiment: Claude is the working engine; Babel Street is optional enrichment.
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "").strip()
BABELSTREET_API_KEY = os.environ.get("BABELSTREET_API_KEY", "").strip()
BLUESKY_IDENTIFIER = os.environ.get("BLUESKY_IDENTIFIER", "").strip()
BLUESKY_APP_PASSWORD = os.environ.get("BLUESKY_APP_PASSWORD", "").strip()
_bsky_session = {"jwt": None, "ts": 0.0}
SENTIMENT_ENABLED = bool(ANTHROPIC_API_KEY or BABELSTREET_API_KEY)
USER_AGENT = "web:xtag:1.0 (by /u/xtag_search)"
# Browser-like UA required for Telegram's t.me/s/ web preview
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")
TIMEOUT = 6  # seconds per platform
SERPAPI_TIMEOUT = 20  # SerpApi runs a full browser; 12s was too tight under load
CACHE_TTL = 1800  # 30 minutes — protects the SerpApi credit budget on repeat searches
_cache: dict[str, tuple[float, dict]] = {}

# ── NotebookLM integration ────────────────────────────────────────────────────
NOTEBOOKLM_SYNC_INTERVAL = 3600  # 60 minutes

def _load_auth_chunks() -> str:
    """Reassemble auth from NOTEBOOKLM_AUTH_1, _2, _3 ... env vars."""
    parts, i = [], 1
    while True:
        part = os.environ.get(f"NOTEBOOKLM_AUTH_{i}", "").strip()
        if not part:
            break
        parts.append(part)
        i += 1
    return "".join(parts)

NOTEBOOKLM_AUTH_ARCHIVE = _load_auth_chunks()
_notebook_store: dict[str, dict] = {}
_notebooklm_status: dict = {"last_sync": None, "notebooks": 0, "error": None}
# ─────────────────────────────────────────────────────────────────────────────

# Telegram channels to search via t.me/s/ web preview (no auth needed).
_DEFAULT_TG_CHANNELS = (
    "telegram,durov,bbcnews,reuters,cnn,aljazeera,dwnews,rtnews,"
    "sputnik,tass_agency,nexta_live,disclosetv,insiderpaper,"
    "bellingcat,intelslava,worldnews"
)


def _parse_tg_channels() -> list:
    raw = os.environ.get("TELEGRAM_CHANNELS", "").strip() or _DEFAULT_TG_CHANNELS
    channels = []
    for c in raw.split(","):
        c = c.strip()
        if not c:
            continue
        c = c.replace("https://", "").replace("http://", "")
        c = c.replace("t.me/s/", "").replace("t.me/", "")
        c = c.lstrip("@/").strip("/")
        if c and c not in channels:
            channels.append(c)
    return channels[:40]


TELEGRAM_CHANNELS = _parse_tg_channels()

DOMAIN_MAP = {
    "x.com": "x",
    "twitter.com": "x",
    "mobile.twitter.com": "x",
    "instagram.com": "instagram",
    "tiktok.com": "tiktok",
    "vm.tiktok.com": "tiktok",
    "facebook.com": "facebook",
    "m.facebook.com": "facebook",
    "linkedin.com": "linkedin",
    "pinterest.com": "pinterest",
    "pin.it": "pinterest",
    "threads.net": "threads",
    "threads.com": "threads",
    "tumblr.com": "tumblr",
    "reddit.com": "reddit",
    "old.reddit.com": "reddit",
    "bsky.app": "bluesky",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "m.youtube.com": "youtube",
    "mastodon.social": "mastodon",
    "mastodon.online": "mastodon",
    "mstdn.social": "mastodon",
    "notebooklm.google.com": "notebooklm",
}

# ---------- helpers ----------


def _query_parts(q: str) -> tuple:
    raw = (q or "").strip()
    is_tag = raw.startswith("#")
    plain = raw.lstrip("#").strip()
    tag = "#" + re.sub(r"\s+", "_", plain) if plain else ""
    return is_tag, tag, plain


def _strip_html(s: str | None) -> str:
    if not s:
        return ""
    s = re.sub(r"<[^>]+>", " ", s)
    s = html.unescape(s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _truncate(s: str, n: int = 280) -> str:
    if len(s) <= n:
        return s
    return s[: n - 1].rstrip() + "…"


def _iso(dt_val) -> str | None:
    if dt_val is None:
        return None
    if isinstance(dt_val, (int, float)):
        try:
            return datetime.fromtimestamp(dt_val, tz=timezone.utc).isoformat()
        except (OSError, ValueError):
            return None
    if isinstance(dt_val, str):
        return dt_val
    return None


def _empty(platform: str, error: str | None = None) -> dict:
    return {"platform": platform, "results": [], "error": error}


# ---------- platform fetchers ----------


def search_youtube(q: str) -> dict:
    if not YOUTUBE_API_KEY:
        return _empty("youtube", "YOUTUBE_API_KEY not set")
    try:
        r = requests.get(
            "https://www.googleapis.com/youtube/v3/search",
            params={
                "part": "snippet",
                "q": (_query_parts(q)[1] if _query_parts(q)[0] else _query_parts(q)[2]),
                "type": "video",
                "maxResults": 50,
                "key": YOUTUBE_API_KEY,
            },
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for item in data.get("items", []):
            vid = item.get("id", {}).get("videoId")
            sn = item.get("snippet", {})
            if not vid:
                continue
            results.append({
                "platform": "youtube",
                "title": _strip_html(sn.get("title")),
                "excerpt": _truncate(_strip_html(sn.get("description"))),
                "url": f"https://www.youtube.com/watch?v={vid}",
                "author": sn.get("channelTitle"),
                "author_url": f"https://www.youtube.com/channel/{sn.get('channelId', '')}",
                "thumbnail": sn.get("thumbnails", {}).get("medium", {}).get("url"),
                "timestamp": sn.get("publishedAt"),
                "meta": None,
            })
        return {"platform": "youtube", "results": results, "error": None}
    except requests.HTTPError as e:
        code = e.response.status_code if e.response else "?"
        return _empty("youtube", f"HTTP {code} — check API key & quota")
    except Exception as e:
        return _empty("youtube", str(e)[:120])


def search_reddit(q: str) -> dict:
    is_tag, tag, plain = _query_parts(q)
    keyword = tag if is_tag else plain
    if not keyword:
        return _empty("reddit", "empty query")
    if not SCRAPEBADGER_KEY:
        return _empty("reddit", "SCRAPEBADGER_KEY not set (Reddit .json is deprecated)")
    try:
        r = requests.get(
            f"{SB_BASE}/reddit/search/posts",
            params={"q": keyword, "sort": "relevance", "t": "year", "limit": 100},
            headers={"x-api-key": SCRAPEBADGER_KEY},
            timeout=SERPAPI_TIMEOUT,
        )
    except requests.RequestException as e:
        return _empty("reddit", f"ScrapeBadger network: {type(e).__name__}")
    if r.status_code == 401:
        return _empty("reddit", "ScrapeBadger: invalid API key")
    if r.status_code == 402:
        return _empty("reddit", "ScrapeBadger: out of credits")
    if r.status_code == 429:
        return _empty("reddit", "ScrapeBadger: rate limited")
    if r.status_code >= 400:
        return _empty("reddit", f"ScrapeBadger HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        return _empty("reddit", f"bad JSON: {str(e)[:80]}")

    items = data if isinstance(data, list) else (
        data.get("posts") or data.get("data") or data.get("results") or []
    )
    results = []
    for d in items:
        if not isinstance(d, dict):
            continue
        permalink = d.get("permalink") or d.get("url") or ""
        if permalink and permalink.startswith("/"):
            permalink = f"https://www.reddit.com{permalink}"
        subreddit = d.get("subreddit") or d.get("subreddit_name") or ""
        author = d.get("author") or d.get("author_name") or "unknown"
        score = d.get("score") or d.get("ups") or 0
        n_comments = d.get("num_comments") or d.get("comments") or 0
        ts = d.get("created_utc") or d.get("created") or d.get("created_at")
        thumb = d.get("thumbnail")
        if isinstance(thumb, str) and not thumb.startswith("http"):
            thumb = None
        results.append({
            "platform": "reddit",
            "title": _strip_html(d.get("title")),
            "excerpt": _truncate(_strip_html(d.get("selftext") or d.get("body") or "")),
            "url": permalink or "https://www.reddit.com",
            "author": f"u/{author}",
            "author_url": f"https://www.reddit.com/user/{author}",
            "thumbnail": thumb,
            "timestamp": _iso(ts) if isinstance(ts, (int, float)) else ts,
            "meta": f"r/{subreddit} · {score} pts · {n_comments} comments",
        })
    return {"platform": "reddit", "results": results, "error": None}


def search_sb_twitter(q: str) -> dict:
    is_tag, tag, plain = _query_parts(q)
    keyword = tag if is_tag else plain
    if not keyword:
        return _empty("x", "empty query")
    if not SCRAPEBADGER_KEY:
        return _empty("x", "SCRAPEBADGER_KEY not set")
    try:
        r = requests.get(
            f"{SB_BASE}/twitter/tweets/advanced_search",
            params={"query": keyword, "query_type": "Top", "count": 100},
            headers={"x-api-key": SCRAPEBADGER_KEY},
            timeout=SERPAPI_TIMEOUT,
        )
    except requests.RequestException as e:
        return _empty("x", f"ScrapeBadger network: {type(e).__name__}")
    if r.status_code == 401:
        return _empty("x", "ScrapeBadger: invalid API key")
    if r.status_code == 402:
        return _empty("x", "ScrapeBadger: out of credits")
    if r.status_code == 429:
        return _empty("x", "ScrapeBadger: rate limited")
    if r.status_code >= 400:
        return _empty("x", f"ScrapeBadger HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        return _empty("x", f"bad JSON: {str(e)[:80]}")

    tweets = data.get("data") if isinstance(data, dict) else (data if isinstance(data, list) else [])
    results = []
    for t in (tweets or []):
        if not isinstance(t, dict):
            continue
        tid = t.get("id", "")
        username = t.get("username") or ""
        media = t.get("media") or []
        thumb = None
        if media and isinstance(media[0], dict):
            thumb = media[0].get("preview_image_url") or media[0].get("url")
        if not thumb and t.get("thumbnail_url"):
            thumb = t.get("thumbnail_url")
        favs = t.get("favorite_count", 0)
        rts = t.get("retweet_count", 0)
        reps = t.get("reply_count", 0)
        results.append({
            "platform": "x",
            "title": None,
            "excerpt": _truncate(_strip_html(t.get("full_text") or t.get("text") or "")),
            "url": f"https://x.com/{username}/status/{tid}" if username and tid else "https://x.com",
            "author": f"@{username}" if username else (t.get("user_name") or None),
            "author_url": f"https://x.com/{username}" if username else None,
            "thumbnail": thumb,
            "timestamp": t.get("created_at"),
            "meta": f"♥ {favs} · ↺ {rts} · 💬 {reps}",
        })
    return {"platform": "x", "results": results, "error": None}


def search_sb_tiktok(q: str) -> dict:
    is_tag, tag, plain = _query_parts(q)
    keyword = tag if is_tag else plain
    if not keyword:
        return _empty("tiktok", "empty query")
    if not SCRAPEBADGER_KEY:
        return _empty("tiktok", "SCRAPEBADGER_KEY not set")
    try:
        r = requests.get(
            f"{SB_BASE}/tiktok/search/videos",
            params={"query": keyword, "region": "US", "count": 50},
            headers={"x-api-key": SCRAPEBADGER_KEY},
            timeout=SERPAPI_TIMEOUT,
        )
    except requests.RequestException as e:
        return _empty("tiktok", f"ScrapeBadger network: {type(e).__name__}")
    if r.status_code == 401:
        return _empty("tiktok", "ScrapeBadger: invalid API key")
    if r.status_code == 402:
        return _empty("tiktok", "ScrapeBadger: out of credits")
    if r.status_code == 429:
        return _empty("tiktok", "ScrapeBadger: rate limited")
    if r.status_code >= 400:
        return _empty("tiktok", f"ScrapeBadger HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        return _empty("tiktok", f"bad JSON: {str(e)[:80]}")

    videos = []
    if isinstance(data, list):
        videos = data
    elif isinstance(data, dict):
        for key in ("videos", "data", "results", "aweme_list", "item_list", "videoList", "items"):
            v = data.get(key)
            if isinstance(v, list) and v:
                videos = v
                break
        if not videos and isinstance(data.get("data"), dict):
            inner = data["data"]
            for key in ("videos", "aweme_list", "item_list", "videoList", "items"):
                v = inner.get(key)
                if isinstance(v, list) and v:
                    videos = v
                    break

    def _tt_int(v, stats, *keys):
        for k in keys:
            val = (stats.get(k) if isinstance(stats, dict) else None)
            if val is None and isinstance(v, dict):
                val = v.get(k)
            if val is not None:
                try:
                    return int(val)
                except (TypeError, ValueError):
                    pass
        return 0

    results = []
    for v in (videos or []):
        if not isinstance(v, dict):
            continue
        author = v.get("author") or v.get("author_info") or {}
        if not isinstance(author, dict):
            author = {}
        handle = author.get("unique_id") or author.get("uniqueId") or author.get("sec_uid") or ""
        stats = v.get("stats") or v.get("statistics") or v.get("stats_v2") or {}
        vmeta = v.get("video") or {}
        plays = _tt_int(v, stats, "play_count", "playCount", "play")
        likes = _tt_int(v, stats, "digg_count", "diggCount", "like_count", "likeCount")
        comments = _tt_int(v, stats, "comment_count", "commentCount", "comment")
        desc = v.get("description") or v.get("desc") or v.get("title") or ""
        thumb = vmeta.get("cover") or vmeta.get("origin_cover") or v.get("cover") or v.get("thumbnail")
        results.append({
            "platform": "tiktok",
            "title": None,
            "excerpt": _truncate(_strip_html(desc)),
            "url": v.get("url") or v.get("share_url") or v.get("shareUrl") or "https://www.tiktok.com",
            "author": f"@{handle}" if handle else (author.get("nickname") or None),
            "author_url": f"https://www.tiktok.com/@{handle}" if handle else None,
            "thumbnail": thumb,
            "timestamp": v.get("create_time_at") or v.get("create_time") or v.get("createTime"),
            "meta": f"▶ {plays} · ♥ {likes} · 💬 {comments}",
        })
    return {"platform": "tiktok", "results": results, "error": None}


def _bsky_token():
    if not BLUESKY_IDENTIFIER or not BLUESKY_APP_PASSWORD:
        return None
    now = time.time()
    if _bsky_session["jwt"] and now - _bsky_session["ts"] < 3600:
        return _bsky_session["jwt"]
    try:
        r = requests.post(
            "https://bsky.social/xrpc/com.atproto.server.createSession",
            json={"identifier": BLUESKY_IDENTIFIER, "password": BLUESKY_APP_PASSWORD},
            headers={"User-Agent": USER_AGENT, "Content-Type": "application/json"},
            timeout=TIMEOUT,
        )
        if r.status_code >= 400:
            return None
        jwt = r.json().get("accessJwt")
        _bsky_session["jwt"] = jwt
        _bsky_session["ts"] = now
        return jwt
    except Exception:
        return None


def _bsky_headers():
    h = {"User-Agent": USER_AGENT}
    tok = _bsky_token()
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    return h


def _bsky_base():
    return "https://bsky.social" if (BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD) else "https://public.api.bsky.app"


def search_bluesky(q: str) -> dict:
    try:
        r = requests.get(
            f"{_bsky_base()}/xrpc/app.bsky.feed.searchPosts",
            params={"q": (_query_parts(q)[1] if _query_parts(q)[0] else _query_parts(q)[2]), "limit": 100},
            headers=_bsky_headers(),
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for post in data.get("posts", []):
            author = post.get("author", {}) or {}
            record = post.get("record", {}) or {}
            handle = author.get("handle", "")
            uri = post.get("uri", "")
            post_id = uri.split("/")[-1] if uri else ""
            web_url = f"https://bsky.app/profile/{handle}/post/{post_id}" if handle and post_id else "https://bsky.app"
            results.append({
                "platform": "bluesky",
                "title": None,
                "excerpt": _truncate(_strip_html(record.get("text", ""))),
                "url": web_url,
                "author": author.get("displayName") or f"@{handle}",
                "author_url": f"https://bsky.app/profile/{handle}",
                "thumbnail": author.get("avatar"),
                "timestamp": post.get("indexedAt"),
                "meta": f"♥ {post.get('likeCount', 0)} · ↺ {post.get('repostCount', 0)} · 💬 {post.get('replyCount', 0)}",
            })
        return {"platform": "bluesky", "results": results, "error": None}
    except Exception as e:
        msg = str(e)[:120]
        if "403" in msg and not (BLUESKY_IDENTIFIER and BLUESKY_APP_PASSWORD):
            msg = "Bluesky now requires auth for search — set BLUESKY_IDENTIFIER + BLUESKY_APP_PASSWORD"
        return _empty("bluesky", msg)


def search_mastodon(q: str) -> dict:
    try:
        tag = q.lstrip("#").strip()
        if not tag:
            return _empty("mastodon", "empty query")
        r = requests.get(
            f"https://mastodon.social/api/v1/timelines/tag/{quote_plus(tag)}",
            params={"limit": 40},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        statuses = r.json()
        if not isinstance(statuses, list):
            return _empty("mastodon", "unexpected response shape")
        results = []
        for st in statuses:
            acc = st.get("account", {}) or {}
            results.append({
                "platform": "mastodon",
                "title": None,
                "excerpt": _truncate(_strip_html(st.get("content", ""))),
                "url": st.get("url"),
                "author": acc.get("display_name") or f"@{acc.get('username', '')}",
                "author_url": acc.get("url"),
                "thumbnail": acc.get("avatar"),
                "timestamp": st.get("created_at"),
                "meta": f"♥ {st.get('favourites_count', 0)} · ↺ {st.get('reblogs_count', 0)} · 💬 {st.get('replies_count', 0)}",
            })
        return {"platform": "mastodon", "results": results, "error": None}
    except Exception as e:
        return _empty("mastodon", str(e)[:120])


def search_hackernews(q: str) -> dict:
    try:
        r = requests.get(
            "https://hn.algolia.com/api/v1/search",
            params={"query": _query_parts(q)[2], "hitsPerPage": 20, "tags": "story"},
            headers={"User-Agent": USER_AGENT},
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        data = r.json()
        results = []
        for hit in data.get("hits", []):
            obj_id = hit.get("objectID", "")
            story_url = hit.get("url") or f"https://news.ycombinator.com/item?id={obj_id}"
            results.append({
                "platform": "hackernews",
                "title": hit.get("title") or hit.get("story_title"),
                "excerpt": _truncate(_strip_html(hit.get("story_text"))),
                "url": story_url,
                "author": hit.get("author"),
                "author_url": f"https://news.ycombinator.com/user?id={hit.get('author', '')}",
                "thumbnail": None,
                "timestamp": hit.get("created_at"),
                "meta": f"{hit.get('points', 0)} pts · {hit.get('num_comments', 0)} comments · discuss: news.ycombinator.com/item?id={obj_id}",
            })
        return {"platform": "hackernews", "results": results, "error": None}
    except Exception as e:
        return _empty("hackernews", str(e)[:120])


def search_gnews(q: str) -> dict:
    try:
        url = f"https://news.google.com/rss/search?q={quote_plus(q)}&hl=en-US&gl=US&ceid=US:en"
        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=TIMEOUT)
        r.raise_for_status()
        feed = feedparser.parse(r.content)
        results = []
        for entry in feed.entries[:60]:
            source = ""
            if hasattr(entry, "source") and entry.source:
                source = entry.source.get("title", "") if isinstance(entry.source, dict) else str(entry.source)
            results.append({
                "platform": "gnews",
                "title": _strip_html(entry.get("title")),
                "excerpt": _truncate(_strip_html(entry.get("summary", ""))),
                "url": entry.get("link"),
                "author": source or None,
                "author_url": None,
                "thumbnail": None,
                "timestamp": entry.get("published"),
                "meta": source,
            })
        return {"platform": "gnews", "results": results, "error": None}
    except Exception as e:
        return _empty("gnews", str(e)[:120])


def _detect_platform_from_url(url: str) -> str | None:
    try:
        host = urlparse(url).hostname or ""
        host = host.lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        while host:
            if host in DOMAIN_MAP:
                return DOMAIN_MAP[host]
            if "." not in host:
                break
            host = host.split(".", 1)[1]
        return None
    except Exception:
        return None


SERPAPI_PLATFORM_DOMAINS = {
    "instagram": ["instagram.com"],
    "facebook":  ["facebook.com"],
    "linkedin":  ["linkedin.com"],
    "pinterest": ["pinterest.com"],
    "threads":   ["threads.net"],
    "tumblr":    ["tumblr.com"],
    "bluesky":   ["bsky.app"],
    "youtube":   ["youtube.com"],
}

_ALL_SOCIAL_DOMAINS = [d for domains in SERPAPI_PLATFORM_DOMAINS.values() for d in domains]
SERPAPI_SITE_FILTER = "(" + " OR ".join(f"site:{d}" for d in _ALL_SOCIAL_DOMAINS) + ")"


def _extract_author(platform_id: str, url: str) -> str | None:
    try:
        path_parts = [p for p in urlparse(url).path.split("/") if p]
        if platform_id == "x" and path_parts:
            return f"@{path_parts[0]}"
        if platform_id in ("instagram", "threads") and path_parts:
            if path_parts[0] not in ("p", "reel", "tv", "explore"):
                return f"@{path_parts[0]}"
        if platform_id == "tiktok" and path_parts and path_parts[0].startswith("@"):
            return path_parts[0]
        if platform_id == "linkedin" and len(path_parts) > 1 and path_parts[0] == "in":
            return path_parts[1]
        if platform_id == "reddit" and len(path_parts) >= 2 and path_parts[0] == "r":
            return f"r/{path_parts[1]}"
        if platform_id == "youtube" and path_parts and path_parts[0].startswith("@"):
            return path_parts[0]
    except Exception:
        pass
    return None


def search_serpapi(q: str) -> dict:
    all_platforms = list(SERPAPI_PLATFORM_DOMAINS.keys())
    is_tag, tag, plain = _query_parts(q)
    clean_q = plain

    if not clean_q:
        return {p: _empty(p, "empty query") for p in all_platforms}
    if not SERPAPI_KEY:
        return {p: _empty(p, "SERPAPI_KEY not set") for p in all_platforms}

    out: dict = {p: {"platform": p, "results": [], "error": None} for p in all_platforms}
    search_term = f'"{tag}"' if is_tag else clean_q
    query = f"{search_term} {SERPAPI_SITE_FILTER}"

    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": query, "num": 60, "api_key": SERPAPI_KEY, "safe": "off"},
            timeout=SERPAPI_TIMEOUT,
        )
    except requests.Timeout:
        try:
            r = requests.get(
                "https://serpapi.com/search",
                params={"engine": "google", "q": query, "num": 60, "api_key": SERPAPI_KEY, "safe": "off"},
                timeout=SERPAPI_TIMEOUT,
            )
        except requests.RequestException as e:
            err = f"SerpApi network: {type(e).__name__} (after retry)"
            return {p: _empty(p, err) for p in all_platforms}
    except requests.RequestException as e:
        err = f"SerpApi network: {type(e).__name__}"
        return {p: _empty(p, err) for p in all_platforms}

    if r.status_code == 401:
        return {p: _empty(p, "SerpApi: invalid API key") for p in all_platforms}
    if r.status_code == 429:
        return {p: _empty(p, "SerpApi: out of search credits (250/mo free) or rate-limited") for p in all_platforms}
    if r.status_code >= 400:
        detail = ""
        try:
            body = r.json()
            detail = (body.get("error") or "")[:140] if isinstance(body, dict) else ""
        except Exception:
            detail = (r.text or "")[:140]
        return {p: _empty(p, f"SerpApi HTTP {r.status_code}: {detail}") for p in all_platforms}

    try:
        data = r.json()
    except Exception as e:
        return {p: _empty(p, f"SerpApi bad JSON: {str(e)[:80]}") for p in all_platforms}

    if isinstance(data, dict) and data.get("error"):
        err = str(data["error"])[:140]
        return {p: _empty(p, f"SerpApi: {err}") for p in all_platforms}

    for item in data.get("organic_results", []) or []:
        url = item.get("link", "")
        platform = _detect_platform_from_url(url)
        if not platform or platform not in out:
            continue
        thumb = item.get("thumbnail")
        if not thumb:
            rich = item.get("rich_snippet") or {}
            top = rich.get("top") or {}
            imgs = top.get("images") or []
            if imgs and isinstance(imgs[0], str):
                thumb = imgs[0]
        out[platform]["results"].append({
            "platform": platform,
            "title": _strip_html(item.get("title")),
            "excerpt": _truncate(_strip_html(item.get("snippet", ""))),
            "url": url,
            "author": _extract_author(platform, url),
            "author_url": None,
            "thumbnail": thumb,
            "timestamp": item.get("date"),
            "meta": (item.get("displayed_link") or "").replace("https://", "").replace("www.", "").split("/")[0],
        })

    return out


def search_google_web(q: str) -> dict:
    is_tag, tag, plain = _query_parts(q)
    if not plain:
        return _empty("google", "empty query")
    if not SERPAPI_KEY:
        return _empty("google", "SERPAPI_KEY not set")
    search_term = f'"{tag}"' if is_tag else plain
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": search_term, "num": 20, "api_key": SERPAPI_KEY, "safe": "off"},
            timeout=SERPAPI_TIMEOUT,
        )
    except requests.RequestException as e:
        return _empty("google", f"SerpApi network: {type(e).__name__}")
    if r.status_code == 401:
        return _empty("google", "SerpApi: invalid API key")
    if r.status_code == 429:
        return _empty("google", "SerpApi: out of credits")
    if r.status_code >= 400:
        return _empty("google", f"SerpApi HTTP {r.status_code}")
    try:
        data = r.json()
    except Exception as e:
        return _empty("google", f"bad JSON: {str(e)[:80]}")
    if isinstance(data, dict) and data.get("error"):
        return _empty("google", f"SerpApi: {str(data['error'])[:120]}")

    results = []
    for item in data.get("organic_results", []) or []:
        url = item.get("link", "")
        if not url:
            continue
        thumb = item.get("thumbnail")
        results.append({
            "platform": "google",
            "title": _strip_html(item.get("title")),
            "excerpt": _truncate(_strip_html(item.get("snippet", ""))),
            "url": url,
            "author": (item.get("source") or (item.get("displayed_link") or "").replace("https://", "").replace("www.", "").split("/")[0]) or None,
            "author_url": None,
            "thumbnail": thumb,
            "timestamp": item.get("date"),
            "meta": (item.get("displayed_link") or "").replace("https://", "").replace("www.", "").split("/")[0],
        })
    return {"platform": "google", "results": results, "error": None}


def _fetch_tg_channel(channel: str, keyword_lc: str) -> list:
    url = f"https://t.me/s/{channel}"
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA}, timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    if "tgme_widget_message_text" not in r.text:
        return []
    try:
        soup = BeautifulSoup(r.text, "lxml")
    except Exception:
        return []

    posts = []
    for m in soup.select(".tgme_widget_message"):
        text_el = m.select_one(".tgme_widget_message_text")
        if not text_el:
            continue
        text = text_el.get_text(" ", strip=True)
        if not text:
            continue
        if keyword_lc and keyword_lc not in text.lower():
            continue
        link_el = m.select_one("a.tgme_widget_message_date")
        link = link_el.get("href") if link_el else f"https://t.me/{channel}"
        time_el = m.select_one("time")
        dt = time_el.get("datetime") if time_el else None
        views_el = m.select_one(".tgme_widget_message_views")
        views = views_el.get_text(strip=True) if views_el else None
        data_post = m.get("data-post", "")
        chan_name = data_post.split("/")[0] if "/" in data_post else channel
        thumb = None
        photo = m.select_one(".tgme_widget_message_photo_wrap, .tgme_widget_message_video_thumb")
        if photo and photo.get("style"):
            mobj = re.search(r"background-image:\s*url\(['\"]?(.*?)['\"]?\)", photo["style"])
            if mobj:
                thumb = mobj.group(1)
        posts.append({
            "platform": "telegram",
            "title": None,
            "excerpt": _truncate(text, 300),
            "url": link,
            "author": f"@{chan_name}",
            "author_url": f"https://t.me/{chan_name}",
            "thumbnail": thumb,
            "timestamp": dt,
            "meta": f"t.me/{chan_name}" + (f" · {views} views" if views else ""),
            "_ts_sort": dt or "",
        })
    return posts


def search_telegram(q: str) -> dict:
    keyword = q.lstrip("#").strip()
    keyword_lc = keyword.lower()
    if not keyword:
        return {"platform": "telegram", "results": [], "error": "empty query"}
    if not TELEGRAM_CHANNELS:
        return {"platform": "telegram", "results": [], "error": "no channels configured"}

    all_posts = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_tg_channel, ch, keyword_lc): ch for ch in TELEGRAM_CHANNELS}
        try:
            for fut in as_completed(futures, timeout=TIMEOUT + 6):
                try:
                    all_posts.extend(fut.result())
                except Exception:
                    pass
        except Exception:
            pass

    all_posts.sort(key=lambda p: p.get("_ts_sort", ""), reverse=True)
    for p in all_posts:
        p.pop("_ts_sort", None)
    return {"platform": "telegram", "results": all_posts[:30], "error": None}


# ---------- NotebookLM integration ----------


def _restore_notebooklm_auth() -> bool:
    """Decode reassembled auth and extract to ~/.notebooklm on Railway startup."""
    if not NOTEBOOKLM_AUTH_ARCHIVE:
        return False
    try:
        archive = NOTEBOOKLM_AUTH_ARCHIVE
	archive += "=" * (-len(archive) % 4)
	data = base64.b64decode(archive)
        home = os.path.expanduser("~")
        proc = subprocess.run(["tar", "-xzf", "-", "-C", home], input=data, capture_output=True)
        if proc.returncode != 0:
            app.logger.warning("NotebookLM: auth restore failed — %s", proc.stderr.decode()[:200])
            return False
        app.logger.info("NotebookLM: auth restored.")
        return True
    except Exception as exc:
        app.logger.warning("NotebookLM: auth restore error — %s", exc)
        return False


async def _sync_notebooks_async() -> dict:
    """Read-only: lists notebooks, sources, notes. Never writes to NotebookLM."""
    from notebooklm import NotebookLMClient
    synced: dict[str, dict] = {}
    async with NotebookLMClient.from_storage() as client:
        for nb in await client.notebooks.list():
            nb_id = str(getattr(nb, "id", None) or getattr(nb, "notebook_id", "") or "")
            nb_title = str(getattr(nb, "title", None) or getattr(nb, "name", "") or "Notebook")
            if not nb_id:
                continue
            sources_out = []
            try:
                for s in await client.sources.list(nb_id):
                    sources_out.append({
                        "title":      str(getattr(s, "title",      None) or ""),
                        "url":        str(getattr(s, "url",        None) or getattr(s, "source_url",  "") or ""),
                        "snippet":    str(getattr(s, "snippet",    None) or getattr(s, "description", "") or ""),
                        "created_at": str(getattr(s, "created_at", None) or "") or None,
                    })
            except Exception:
                pass
            notes_out = []
            try:
                for n in await client.notes.list(nb_id):
                    notes_out.append({
                        "title":      str(getattr(n, "title",      None) or "Note"),
                        "content":    str(getattr(n, "content",    None) or getattr(n, "text", "") or ""),
                        "created_at": str(getattr(n, "created_at", None) or "") or None,
                    })
            except Exception:
                pass
            synced[nb_id] = {
                "id": nb_id, "title": nb_title,
                "sources": sources_out, "notes": notes_out,
                "synced_at": datetime.now(timezone.utc).isoformat(),
            }
    return synced


def _notebooklm_sync_loop() -> None:
    """Background thread — syncs every 60 min. Read-only. Runs on Railway."""
    import asyncio
    while True:
        try:
            synced = asyncio.run(_sync_notebooks_async())
            _notebook_store.clear()
            _notebook_store.update(synced)
            _notebooklm_status.update({
                "last_sync": datetime.now(timezone.utc).isoformat(),
                "notebooks": len(synced),
                "error": None,
            })
            app.logger.info("NotebookLM: synced %d notebook(s)", len(synced))
        except Exception as exc:
            _notebooklm_status["error"] = str(exc)[:200]
            app.logger.warning("NotebookLM: sync error — %s", exc)
        time.sleep(NOTEBOOKLM_SYNC_INTERVAL)


def search_notebooklm(q: str) -> dict:
    """Search cached notebook sources and notes."""
    if not _notebook_store:
        return _empty("notebooklm", "Syncing..." if NOTEBOOKLM_AUTH_ARCHIVE else "NOTEBOOKLM_AUTH_1 not set")
    q_lower = (q or "").lstrip("#").lower().strip()
    results = []
    for nb_id, nb in _notebook_store.items():
        nb_title = nb.get("title", "Notebook")
        nb_url   = f"https://notebooklm.google.com/notebook/{nb_id}"
        for src in nb.get("sources", []):
            if not q_lower or q_lower in f"{src.get('title','')} {src.get('url','')} {src.get('snippet','')}".lower():
                results.append({
                    "platform": "notebooklm",
                    "title": src.get("title") or "(untitled source)",
                    "excerpt": _truncate(src.get("snippet") or src.get("url") or ""),
                    "url": src.get("url") or nb_url,
                    "author": nb_title, "author_url": nb_url,
                    "thumbnail": None, "timestamp": src.get("created_at"),
                    "meta": f"NotebookLM · {nb_title} · source",
                })
        for note in nb.get("notes", []):
            if not q_lower or q_lower in f"{note.get('title','')} {note.get('content','')}".lower():
                results.append({
                    "platform": "notebooklm",
                    "title": note.get("title", "Note"),
                    "excerpt": _truncate(note.get("content", "")),
                    "url": nb_url,
                    "author": nb_title, "author_url": nb_url,
                    "thumbnail": None, "timestamp": note.get("created_at"),
                    "meta": f"NotebookLM · {nb_title} · note",
                })
    return {"platform": "notebooklm", "results": results[:50], "error": None}


# ---------- platform registry ----------

API_PLATFORMS = {
    "youtube": search_youtube,
    "reddit": search_reddit,
    "bluesky": search_bluesky,
    "mastodon": search_mastodon,
    "hackernews": search_hackernews,
    "gnews": search_gnews,
    "telegram": search_telegram,
    "x": search_sb_twitter,
    "tiktok": search_sb_tiktok,
    "google": search_google_web,
    "notebooklm": search_notebooklm,
}


# ---------- routes ----------


@app.route("/")
def index():
    resp = make_response(render_template(
        "index.html",
        youtube_enabled=bool(YOUTUBE_API_KEY),
        cse_enabled=bool(SERPAPI_KEY),
        sentiment_enabled=SENTIMENT_ENABLED,
    ))
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    resp.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return resp


# ---------- sentiment: Claude + Babel Street, reconciled ----------

BABEL_CAP = 24
BABEL_WORKERS = 12
BABEL_BUDGET = 12
BABEL_TIMEOUT = 10
_SCORE = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}


def _norm_label(raw) -> str:
    lab = str(raw or "").lower()
    if "pos" in lab:
        return "positive"
    if "neg" in lab:
        return "negative"
    return "neutral"


def _babel_one(text: str):
    try:
        r = requests.post(
            "https://analytics.babelstreet.com/rest/v1/sentiment",
            headers={"X-BabelStreetAPI-Key": BABELSTREET_API_KEY,
                     "Content-Type": "application/json", "Accept": "application/json"},
            json={"content": text[:3500]},
            timeout=BABEL_TIMEOUT,
        )
        if r.status_code >= 400:
            return None
        body = r.json()
        if not isinstance(body, dict):
            return None
        doc = body.get("document") or (body.get("sentiment") or {}).get("document") or {}
        return _norm_label(doc.get("label")) if doc.get("label") is not None else None
    except Exception:
        return None


def _sentiment_babelstreet(texts: list, indices: list) -> dict:
    if not BABELSTREET_API_KEY or not indices:
        return {}
    out = {}
    with ThreadPoolExecutor(max_workers=BABEL_WORKERS) as ex:
        futs = {ex.submit(_babel_one, texts[i]): i for i in indices}
        try:
            for fut in as_completed(list(futs.keys()), timeout=BABEL_BUDGET):
                lab = fut.result()
                if lab:
                    out[futs[fut]] = lab
        except Exception:
            pass
    return out


def _sentiment_claude(texts: list) -> list | None:
    if not ANTHROPIC_API_KEY or not texts:
        return None
    batch = texts[:120]
    numbered = "\n".join(f"{i+1}. {t[:240]}" for i, t in enumerate(batch))
    prompt = (
        "Classify the sentiment of each numbered social-media post as exactly one of: "
        "positive, neutral, negative. Consider the tone toward the main subject. "
        "Respond with ONLY a JSON array of lowercase strings, one per post, in order. "
        f"No prose.\n\n{numbered}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 2000,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=SERPAPI_TIMEOUT + 8,
        )
        if r.status_code >= 400:
            return None
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"```json|```", "", text).strip()
        import json as _json
        arr = _json.loads(text)
        out = [_norm_label(v) for v in arr]
        while len(out) < len(texts):
            out.append("neutral")
        return out[:len(texts)]
    except Exception:
        return None


def attach_sentiment(platforms: dict) -> dict:
    flat = []
    for group in platforms.values():
        for r in group.get("results", []):
            txt = ((r.get("title") or "") + " " + (r.get("excerpt") or "")).strip()
            if txt:
                flat.append((r, txt))
    if not flat:
        return {"scored": 0, "positive": 0, "neutral": 0, "negative": 0,
                "net": None, "engines": [], "agreement": None, "babel_scored": 0}

    texts = [t for _, t in flat]
    engines = []

    claude = _sentiment_claude(texts)
    if claude:
        engines.append("claude")

    order = sorted(range(len(flat)), key=lambda i: int(flat[i][0].get("engagement") or 0), reverse=True)
    babel = _sentiment_babelstreet(texts, order[:BABEL_CAP])
    if babel:
        engines.append("babelstreet")

    counts = {"positive": 0, "neutral": 0, "negative": 0}
    net_sum = 0.0
    scored = agree_n = agree_d = 0
    for i, (r, _) in enumerate(flat):
        c = claude[i] if claude and i < len(claude) else None
        b = babel.get(i)
        if c and b:
            agree_d += 1
            agree_n += 1 if c == b else 0
            score = (_SCORE[c] + _SCORE[b]) / 2.0
            final = "positive" if score > 0.25 else "negative" if score < -0.25 else "neutral"
            r["s_claude"], r["s_babel"] = c, b
        elif c:
            score, final = _SCORE[c], c
            r["s_claude"] = c
        elif b:
            score, final = _SCORE[b], b
            r["s_babel"] = b
        else:
            continue
        r["sentiment"] = final
        counts[final] += 1
        net_sum += score
        scored += 1

    return {
        "scored": scored, **counts,
        "net": round(net_sum / scored, 2) if scored else None,
        "engines": engines,
        "agreement": round(agree_n / agree_d, 2) if agree_d else None,
        "babel_scored": len(babel),
    }


# ---------- narratives ----------

def extract_narratives(platforms: dict, max_posts: int = 80, max_narratives: int = 6) -> list:
    if not ANTHROPIC_API_KEY:
        return []
    items = []
    for group in platforms.values():
        for r in group.get("results", []):
            txt = ((r.get("title") or "") + " " + (r.get("excerpt") or "")).strip()
            if txt:
                items.append((int(r.get("engagement") or 0), txt))
    if len(items) < 8:
        return []
    items.sort(key=lambda t: t[0], reverse=True)
    posts = [t[1][:240] for t in items[:max_posts]]
    numbered = "\n".join(f"{i+1}. {p}" for i, p in enumerate(posts))
    prompt = (
        f"Below are {len(posts)} social-media posts about a single search topic. "
        f"Identify up to {max_narratives} distinct recurring narratives or angles running through them. "
        "For each, give a short human-readable label (max 6 words) and the count of posts that fit it. "
        "Only include narratives supported by at least 2 posts. Respond with ONLY a JSON array of "
        'objects like [{"label": "...", "count": N}], ordered by count descending. No prose.\n\n'
        f"{numbered}"
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 700,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=SERPAPI_TIMEOUT,
        )
        if r.status_code >= 400:
            return []
        data = r.json()
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        text = re.sub(r"```json|```", "", text).strip()
        import json as _json
        arr = _json.loads(text)
        out = []
        for o in arr:
            if isinstance(o, dict) and o.get("label"):
                try:
                    cnt = int(o.get("count") or 0)
                except (TypeError, ValueError):
                    cnt = 0
                out.append({"label": str(o["label"])[:80], "count": cnt})
        out.sort(key=lambda x: x["count"], reverse=True)
        return out[:max_narratives]
    except Exception:
        return []


# ---------- engagement, time & dashboard aggregates ----------

_INT_RE = re.compile(r"\d[\d,]*")


def _engagement_breakdown(meta) -> dict:
    out = {"reactions": 0, "comments": 0, "shares": 0}
    if not meta:
        return out
    s = str(meta)

    def grab(pattern):
        m = re.search(pattern, s)
        if not m:
            return 0
        try:
            return int(m.group(1).replace(",", ""))
        except (TypeError, ValueError):
            return 0

    out["reactions"] += grab(r"♥\s*([\d,]+)")
    out["shares"]    += grab(r"↺\s*([\d,]+)")
    out["comments"]  += grab(r"\U0001f4ac\s*([\d,]+)")
    out["reactions"] += grab(r"([\d,]+)\s*pts")
    out["comments"]  += grab(r"([\d,]+)\s*comments")
    return out


def _engagement_from_meta(meta) -> int:
    if not meta:
        return 0
    total = 0
    for m in _INT_RE.findall(str(meta)):
        try:
            total += int(m.replace(",", ""))
        except ValueError:
            pass
    return total


def _parse_dt(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        ts = float(val)
        if ts > 1e12:
            ts /= 1000.0
        try:
            return datetime.fromtimestamp(ts, tz=timezone.utc)
        except (OSError, ValueError, OverflowError):
            return None
    s = str(val).strip()
    if not s:
        return None
    if s.isdigit():
        return _parse_dt(int(s))
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        from email.utils import parsedate_to_datetime
        dt = parsedate_to_datetime(s)
        if dt is not None:
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
    except (TypeError, ValueError):
        pass
    for fmt in ("%a %b %d %H:%M:%S %z %Y", "%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            dt = datetime.strptime(s, fmt)
            return (dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _build_aggregates(platforms: dict) -> dict:
    source_mix = []
    total_mentions = with_results = 0
    reactions = comments = shares = 0
    searched = len(platforms)
    for pid, group in platforms.items():
        results = group.get("results", []) or []
        n = len(results)
        if n:
            with_results += 1
        total_mentions += n
        for r in results:
            eb = _engagement_breakdown(r.get("meta"))
            reactions += eb["reactions"]
            comments += eb["comments"]
            shares += eb["shares"]
        source_mix.append({"platform": pid, "count": n})
    source_mix = sorted([s for s in source_mix if s["count"] > 0],
                        key=lambda s: s["count"], reverse=True)
    return {
        "totals": {
            "mentions": total_mentions,
            "engagement": reactions + comments + shares,
            "reactions": reactions,
            "comments": comments,
            "shares": shares,
            "platforms_with_results": with_results,
            "platforms_searched": searched,
        },
        "source_mix": source_mix,
    }


@app.route("/api/search")
def api_search():
    q = (request.args.get("q") or "").strip()
    if not q:
        return jsonify({"error": "missing q"}), 400
    if len(q) > 200:
        return jsonify({"error": "query too long"}), 400

    cache_key = q.lower()
    now = time.time()
    if cache_key in _cache:
        ts, cached = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return jsonify({**cached, "cached": True})

    direct_out: dict[str, dict] = {}
    cse_out: dict[str, dict] = {}
    with ThreadPoolExecutor(max_workers=len(API_PLATFORMS) + 1) as ex:
        futures = {ex.submit(fn, q): name for name, fn in API_PLATFORMS.items()}
        cse_future = ex.submit(search_serpapi, q)

        for fut in as_completed(list(futures.keys()) + [cse_future], timeout=SERPAPI_TIMEOUT + 8):
            if fut is cse_future:
                try:
                    cse_out = fut.result()
                except Exception as e:
                    cse_out = {p: _empty(p, str(e)[:120]) for p in SERPAPI_PLATFORM_DOMAINS}
            else:
                name = futures[fut]
                try:
                    direct_out[name] = fut.result()
                except Exception as e:
                    direct_out[name] = _empty(name, str(e)[:120])

    out: dict[str, dict] = {}
    for pid in set(direct_out.keys()) | set(cse_out.keys()):
        direct = direct_out.get(pid)
        cse = cse_out.get(pid)
        if direct and cse:
            existing_urls = {r.get("url") for r in direct.get("results", []) if r.get("url")}
            cse_extra = [r for r in cse.get("results", []) if r.get("url") and r["url"] not in existing_urls]
            merged_results = direct.get("results", []) + cse_extra
            error = None if merged_results else (direct.get("error") or cse.get("error"))
            out[pid] = {"platform": pid, "results": merged_results, "error": error}
        else:
            out[pid] = direct or cse

    for group in out.values():
        for r in group.get("results", []):
            eb = _engagement_breakdown(r.get("meta"))
            r["engagement"] = eb["reactions"] + eb["comments"] + eb["shares"]

    sentiment = {"scored": 0, "positive": 0, "neutral": 0, "negative": 0,
                 "net": None, "engines": [], "agreement": None, "babel_scored": 0}
    if SENTIMENT_ENABLED:
        try:
            sentiment = attach_sentiment(out)
        except Exception as e:
            app.logger.warning("sentiment failed: %s", e)
            sentiment["error"] = str(e)[:120]

    narratives = []
    try:
        narratives = extract_narratives(out)
    except Exception as e:
        app.logger.warning("narratives failed: %s", e)

    agg = _build_aggregates(out)
    payload = {"query": q, "platforms": out, "sentiment": sentiment,
               "narratives": narratives, "totals": agg["totals"],
               "source_mix": agg["source_mix"], "cached": False}
    _cache[cache_key] = (now, payload)
    if len(_cache) > 200:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:50]
        for k, _ in oldest:
            _cache.pop(k, None)
    return jsonify(payload)


@app.route("/api/brief", methods=["POST"])
def api_brief():
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    snippets = body.get("snippets") or []
    if not q:
        return jsonify({"error": "missing q"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"brief": None, "reason": "Intelligence brief needs an Anthropic API key (ANTHROPIC_API_KEY)."}), 200

    cache_key = "__brief__" + q.lower()
    now = time.time()
    if cache_key in _cache:
        ts, cached = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return jsonify({**cached, "cached": True})

    context = "\n".join("- " + str(s).replace("\n", " ")[:220] for s in snippets[:15] if s)
    if context:
        prompt = (
            f'You are an OSINT analyst. A colleague searched the term "{q}" across social platforms. '
            f'Here are real posts currently circulating about it:\n\n{context}\n\n'
            f'Write a tight 2-3 sentence intelligence brief explaining what "{q}" refers to, who or what '
            f'is involved, and why it is being discussed. Ground it in the posts above and your own knowledge. '
            f'ALWAYS write the brief in English, even if the search term and the posts are in another language '
            f'(translate and explain the meaning for an English-speaking analyst). '
            f'Use **bold** for key people, groups, or events. Be factual and neutral. Output only the brief.'
        )
    else:
        prompt = (
            f'You are an OSINT analyst. Write a tight 2-3 sentence intelligence brief on the search term '
            f'"{q}": what it refers to, who or what is involved, and why it matters. '
            f'ALWAYS write the brief in English, even if the search term is in another language '
            f'(translate and explain it for an English-speaking analyst). '
            f'Use **bold** for key entities. Be factual and neutral. Output only the brief.'
        )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 350,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=SERPAPI_TIMEOUT,
        )
        if r.status_code >= 400:
            reason = "Intelligence brief unavailable."
            try:
                err = (r.json().get("error") or {})
                etype = err.get("type", "")
                emsg = err.get("message", "")
                if "credit" in emsg.lower() or "billing" in emsg.lower():
                    reason = "Intelligence brief unavailable — Anthropic API credit balance is empty. Add credits to enable briefs and sentiment."
                elif "rate" in etype.lower():
                    reason = "Intelligence brief rate-limited — try again in a moment."
                elif emsg:
                    reason = "Intelligence brief unavailable: " + emsg[:140]
            except Exception:
                pass
            return jsonify({"brief": None, "reason": reason}), 200
        data = r.json()
        brief = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
        if not brief:
            return jsonify({"brief": None, "reason": "Intelligence brief unavailable."}), 200
        payload = {"brief": brief}
        _cache[cache_key] = (now, payload)
        return jsonify(payload)
    except Exception:
        return jsonify({"brief": None, "reason": "Intelligence brief unavailable — request failed."}), 200


@app.route("/debug/tiktok")
def debug_tiktok():
    debug_token = os.environ.get("DEBUG_TOKEN", "").strip()
    if debug_token and request.headers.get("X-Debug-Token") != debug_token:
        return {"error": "auth required"}, 401
    if not SCRAPEBADGER_KEY:
        return {"error": "SCRAPEBADGER_KEY not set"}, 200
    q = (request.args.get("q") or "news").strip()
    is_tag, tag, plain = _query_parts(q)
    keyword = tag if is_tag else plain
    try:
        r = requests.get(
            f"{SB_BASE}/tiktok/search/videos",
            params={"query": keyword, "region": "US", "count": 10},
            headers={"x-api-key": SCRAPEBADGER_KEY},
            timeout=SERPAPI_TIMEOUT,
        )
        try:
            body = r.json()
        except Exception:
            body = {"raw_text": (r.text or "")[:1500]}
        top_keys = list(body.keys()) if isinstance(body, dict) else "(list)" if isinstance(body, list) else str(type(body))
        container = None
        sample = None
        if isinstance(body, list):
            container = "(root list)"
            sample = body[0] if body else None
        elif isinstance(body, dict):
            for k in ("videos", "data", "results", "aweme_list", "item_list", "videoList", "items"):
                v = body.get(k)
                if isinstance(v, list) and v:
                    container = k
                    sample = v[0]
                    break
            if container is None and isinstance(body.get("data"), dict):
                for k in ("videos", "aweme_list", "item_list", "videoList", "items"):
                    v = body["data"].get(k)
                    if isinstance(v, list) and v:
                        container = "data." + k
                        sample = v[0]
                        break
        return {
            "status_code": r.status_code,
            "keyword_used": keyword,
            "top_level_keys": top_keys,
            "detected_container": container,
            "sample_item_keys": list(sample.keys()) if isinstance(sample, dict) else None,
            "sample_item": json.dumps(sample)[:1800] if sample is not None else None,
            "parsed_by_current_code": len(search_sb_tiktok(q).get("results", [])),
        }, 200
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500


@app.route("/debug/brief")
def debug_brief():
    q = (request.args.get("q") or "").strip()
    if not q:
        return {"error": "pass ?q="}, 400
    if not ANTHROPIC_API_KEY:
        return {"error": "ANTHROPIC_API_KEY not set", "key_set": False}, 200
    prompt = (
        f'You are an OSINT analyst. Write a tight 2-3 sentence intelligence brief on the search term '
        f'"{q}": what it refers to, who or what is involved, and why it matters. '
        f'ALWAYS write in English even if the term is in another language. '
        f'Use **bold** for key entities. Output only the brief.'
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_API_KEY, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 350,
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=SERPAPI_TIMEOUT,
        )
        out = {"status_code": r.status_code, "key_last4": ANTHROPIC_API_KEY[-4:]}
        try:
            data = r.json()
            out["brief"] = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
            if r.status_code >= 400:
                out["api_error"] = data.get("error")
        except Exception as e:
            out["parse_error"] = str(e)[:200]
            out["raw"] = (r.text or "")[:500]
        return out, 200
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500


@app.route("/debug/serpapi")
def debug_serpapi():
    debug_token = os.environ.get("DEBUG_TOKEN", "").strip()
    if debug_token and request.headers.get("X-Debug-Token") != debug_token:
        return {"error": "auth required"}, 401
    if not SERPAPI_KEY:
        return {"serpapi_key_set": False, "error": "SERPAPI_KEY not set"}, 200
    q = (request.args.get("q") or "test").strip()
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": f"{q} site:x.com", "num": 3, "api_key": SERPAPI_KEY},
            timeout=SERPAPI_TIMEOUT,
        )
        try:
            body = r.json()
        except Exception:
            body = {"raw_text": (r.text or "")[:300]}
        info = body.get("search_information", {}) if isinstance(body, dict) else {}
        return {
            "serpapi_key_last4": SERPAPI_KEY[-4:],
            "status_code": r.status_code,
            "num_organic_results": len(body.get("organic_results", [])) if isinstance(body, dict) else 0,
            "serpapi_error": body.get("error") if isinstance(body, dict) else None,
            "total_results": info.get("total_results"),
            "first_link": (body.get("organic_results", [{}])[0].get("link")
                           if isinstance(body, dict) and body.get("organic_results") else None),
        }, 200
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:200]}"}, 500


@app.route("/debug/account")
def debug_account():
    debug_token = os.environ.get("DEBUG_TOKEN", "").strip()
    if debug_token and request.headers.get("X-Debug-Token") != debug_token:
        return {"error": "auth required"}, 401
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not set"}, 200
    try:
        r = requests.get("https://serpapi.com/account", params={"api_key": SERPAPI_KEY}, timeout=SERPAPI_TIMEOUT)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}, 500


@app.route("/debug/scrapebadger")
def debug_scrapebadger():
    debug_token = os.environ.get("DEBUG_TOKEN", "").strip()
    if debug_token and request.headers.get("X-Debug-Token") != debug_token:
        return {"error": "auth required"}, 401
    if not SCRAPEBADGER_KEY:
        return {"scrapebadger_key_set": False, "error": "SCRAPEBADGER_KEY not set"}, 200
    out = {"scrapebadger_key_last4": SCRAPEBADGER_KEY[-4:]}
    q = (request.args.get("q") or "test").strip()
    try:
        acct = requests.get(f"{SB_BASE}/account", headers={"x-api-key": SCRAPEBADGER_KEY}, timeout=SERPAPI_TIMEOUT)
        out["account_status"] = acct.status_code
        if acct.ok:
            out["account"] = acct.json()
    except Exception as e:
        out["account_error"] = f"{type(e).__name__}: {str(e)[:100]}"
    try:
        r = requests.get(f"{SB_BASE}/reddit/search/posts", params={"q": q, "limit": 3},
                         headers={"x-api-key": SCRAPEBADGER_KEY}, timeout=SERPAPI_TIMEOUT)
        out["reddit_search_status"] = r.status_code
        try:
            body = r.json()
            items = body if isinstance(body, list) else (
                body.get("posts") or body.get("data") or body.get("results") or [])
            out["reddit_num_results"] = len(items)
            out["reddit_error"] = body.get("error") if isinstance(body, dict) else None
        except Exception:
            out["reddit_raw"] = (r.text or "")[:200]
    except Exception as e:
        out["reddit_error"] = f"{type(e).__name__}: {str(e)[:100]}"
    return out, 200


# ---------- NotebookLM routes ----------


@app.route("/api/notebooklm/status")
def notebooklm_status():
    """Shows sync health and cached notebook titles."""
    chunks = sum(1 for i in range(1, 20) if os.environ.get(f"NOTEBOOKLM_AUTH_{i}"))
    return jsonify({
        "configured":   chunks > 0,
        "auth_chunks":  chunks,
        "notebooks":    _notebooklm_status["notebooks"],
        "titles":       [nb.get("title") for nb in _notebook_store.values()],
        "last_sync":    _notebooklm_status["last_sync"],
        "error":        _notebooklm_status["error"],
        "interval_min": NOTEBOOKLM_SYNC_INTERVAL // 60,
    })


# Start background sync thread if auth chunks are present.
# Syncs immediately on startup then every 60 minutes. Read-only.
if NOTEBOOKLM_AUTH_ARCHIVE and _restore_notebooklm_auth():
    threading.Thread(target=_notebooklm_sync_loop, daemon=True, name="notebooklm-sync").start()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
