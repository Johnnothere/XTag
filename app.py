"""XTag — cross-platform hashtag & keyword search aggregator.

Backend fetches results in parallel from:
- Direct APIs: YouTube, Reddit, Bluesky, Mastodon, Hacker News, Google News RSS
- Google Custom Search (site-restricted): X, Instagram, TikTok, Facebook,
  LinkedIn, Pinterest, Threads, Tumblr — platforms without public APIs.

Returns a unified feed with per-platform badges.
"""
from __future__ import annotations

import html
import json
import os
import re
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
SENTIMENT_ENABLED = bool(ANTHROPIC_API_KEY or BABELSTREET_API_KEY)
USER_AGENT = "web:xtag:1.0 (by /u/xtag_search)"
# Browser-like UA required for Telegram's t.me/s/ web preview
BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
              "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Safari/605.1.15")
TIMEOUT = 6  # seconds per platform
SERPAPI_TIMEOUT = 20  # SerpApi runs a full browser; 12s was too tight under load
CACHE_TTL = 1800  # 30 minutes — protects the SerpApi credit budget on repeat searches
_cache: dict[str, tuple[float, dict]] = {}

# Telegram channels to search via t.me/s/ web preview (no auth needed).
# Editable in Railway → Variables as TELEGRAM_CHANNELS (comma-separated names,
# with or without @ or t.me/ prefix). Falls back to this curated starter set.
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
        # Normalize: strip @, t.me/, https://t.me/, /s/ etc.
        c = c.replace("https://", "").replace("http://", "")
        c = c.replace("t.me/s/", "").replace("t.me/", "")
        c = c.lstrip("@/").strip("/")
        if c and c not in channels:
            channels.append(c)
    return channels[:40]  # cap to keep fan-out reasonable


TELEGRAM_CHANNELS = _parse_tg_channels()

# Domains → platform id (for tagging Google CSE results).
# Include reddit/bsky/youtube/mastodon in case they're added to the CSE site list.
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
    # These get filled from CSE if you add them to your engine's site list:
    "reddit.com": "reddit",
    "old.reddit.com": "reddit",
    "bsky.app": "bluesky",
    "youtube.com": "youtube",
    "youtu.be": "youtube",
    "m.youtube.com": "youtube",
    "mastodon.social": "mastodon",
    "mastodon.online": "mastodon",
    "mstdn.social": "mastodon",
}

# ---------- helpers ----------


def _query_parts(q: str) -> tuple:
    """Return (is_hashtag, hashtag_form, plain_form).

    hashtag_form keeps a leading # and strips spaces (#Team_313).
    plain_form is the bare keyword(s) with # removed (Team_313 / energy security).
    Relevance is much better when hashtag searches keep the # on platforms that
    support it, so callers can pick the right form.
    """
    raw = (q or "").strip()
    is_tag = raw.startswith("#")
    plain = raw.lstrip("#").strip()
    tag = "#" + re.sub(r"\s+", "_", plain) if plain else ""
    return is_tag, tag, plain


def _strip_html(s: str | None) -> str:
    """Remove HTML tags and unescape entities. Mastodon / Google News return HTML."""
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
    """Coerce various timestamp shapes to ISO 8601."""
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
                "maxResults": 25,
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
    """Reddit via ScrapeBadger — the public .json endpoint was deprecated May 2026.

    Uses ScrapeBadger's Reddit search (structured JSON, works from cloud IPs).
    Falls back to an error note if no ScrapeBadger key is set.
    """
    is_tag, tag, plain = _query_parts(q)
    keyword = tag if is_tag else plain
    if not keyword:
        return _empty("reddit", "empty query")
    if not SCRAPEBADGER_KEY:
        return _empty("reddit", "SCRAPEBADGER_KEY not set (Reddit .json is deprecated)")
    try:
        r = requests.get(
            f"{SB_BASE}/reddit/search/posts",
            params={"q": keyword, "sort": "relevance", "t": "year", "limit": 25},
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

    # ScrapeBadger may return a list, or {"posts":[...]}/{"data":[...]}/{"results":[...]}
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
    """X/Twitter via ScrapeBadger advanced search — rich engagement data."""
    is_tag, tag, plain = _query_parts(q)
    keyword = tag if is_tag else plain
    if not keyword:
        return _empty("x", "empty query")
    if not SCRAPEBADGER_KEY:
        return _empty("x", "SCRAPEBADGER_KEY not set")
    try:
        r = requests.get(
            f"{SB_BASE}/twitter/tweets/advanced_search",
            params={"query": keyword, "query_type": "Top", "count": 25},
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
    """TikTok via ScrapeBadger video search — 5 credits per call."""
    is_tag, tag, plain = _query_parts(q)
    keyword = tag if is_tag else plain
    if not keyword:
        return _empty("tiktok", "empty query")
    if not SCRAPEBADGER_KEY:
        return _empty("tiktok", "SCRAPEBADGER_KEY not set")
    try:
        r = requests.get(
            f"{SB_BASE}/tiktok/search/videos",
            params={"query": keyword, "region": "US", "count": 20},
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

    # ScrapeBadger's TikTok response shape varies; try several container keys.
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


def search_bluesky(q: str) -> dict:
    try:
        r = requests.get(
            "https://public.api.bsky.app/xrpc/app.bsky.feed.searchPosts",
            params={"q": (_query_parts(q)[1] if _query_parts(q)[0] else _query_parts(q)[2]), "limit": 25},
            headers={"User-Agent": USER_AGENT},
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
            # Convert at:// URI to bsky.app link
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
        return _empty("bluesky", str(e)[:120])


def search_mastodon(q: str) -> dict:
    """Mastodon's keyword search requires auth; use the public hashtag timeline for #tags."""
    try:
        tag = q.lstrip("#").strip()
        if not tag:
            return _empty("mastodon", "empty query")
        # Public unauthenticated hashtag timeline
        r = requests.get(
            f"https://mastodon.social/api/v1/timelines/tag/{quote_plus(tag)}",
            params={"limit": 12},
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
        for entry in feed.entries[:25]:
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
    """Map a URL's hostname to a platform id via DOMAIN_MAP."""
    try:
        host = urlparse(url).hostname or ""
        host = host.lower().lstrip(".")
        if host.startswith("www."):
            host = host[4:]
        # Try full host, then strip subdomains progressively
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

# Flat OR filter covering every social domain — used in a single SerpApi call
_ALL_SOCIAL_DOMAINS = [d for domains in SERPAPI_PLATFORM_DOMAINS.values() for d in domains]
SERPAPI_SITE_FILTER = "(" + " OR ".join(f"site:{d}" for d in _ALL_SOCIAL_DOMAINS) + ")"


def _extract_author(platform_id: str, url: str) -> str | None:
    """Best-effort handle/author extraction from a result URL's path."""
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
    """One SerpApi Google call across all social domains, split by platform.

    SerpApi runs a real browser + solves CAPTCHAs, so it returns what a real Google
    user sees — including X/Instagram/TikTok/etc. that the dead CSE API can't reach.
    Cost: 1 search credit per call (free tier = 250/month). Results are split into
    per-platform buckets by URL domain so the UI can badge them correctly.
    """
    all_platforms = list(SERPAPI_PLATFORM_DOMAINS.keys())
    is_tag, tag, plain = _query_parts(q)
    clean_q = plain

    if not clean_q:
        return {p: _empty(p, "empty query") for p in all_platforms}
    if not SERPAPI_KEY:
        return {p: _empty(p, "SERPAPI_KEY not set") for p in all_platforms}

    out: dict = {p: {"platform": p, "results": [], "error": None} for p in all_platforms}
    # For hashtags, quote the underscore form so Google matches the tag closely.
    search_term = f'"{tag}"' if is_tag else clean_q
    query = f"{search_term} {SERPAPI_SITE_FILTER}"

    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={
                "engine": "google",
                "q": query,
                "num": 60,       # pull up to 60 results, spread across ~8 platforms
                "api_key": SERPAPI_KEY,
                "safe": "off",
            },
            timeout=SERPAPI_TIMEOUT,
        )
    except requests.Timeout:
        # One retry — SerpApi's headless browser occasionally runs slow under load
        try:
            r = requests.get(
                "https://serpapi.com/search",
                params={
                    "engine": "google",
                    "q": query,
                    "num": 60,
                    "api_key": SERPAPI_KEY,
                    "safe": "off",
                },
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

    # SerpApi may return its own error field even on 200
    if isinstance(data, dict) and data.get("error"):
        err = str(data["error"])[:140]
        return {p: _empty(p, f"SerpApi: {err}") for p in all_platforms}

    for item in data.get("organic_results", []) or []:
        url = item.get("link", "")
        platform = _detect_platform_from_url(url)
        if not platform or platform not in out:
            continue

        # SerpApi thumbnail sources vary
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
            "timestamp": item.get("date"),  # SerpApi sometimes provides this
            "meta": (item.get("displayed_link") or "").replace("https://", "").replace("www.", "").split("/")[0],
        })

    return out


def search_google_web(q: str) -> dict:
    """Plain Google web search (no site filter) — the general 'Web' results that
    social-searcher shows. One SerpApi credit. Returns a single 'google' platform group."""
    is_tag, tag, plain = _query_parts(q)
    if not plain:
        return _empty("google", "empty query")
    if not SERPAPI_KEY:
        return _empty("google", "SERPAPI_KEY not set")
    search_term = f'"{tag}"' if is_tag else plain
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": search_term, "num": 20,
                    "api_key": SERPAPI_KEY, "safe": "off"},
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
    """Fetch one channel's t.me/s/ web preview and return posts matching keyword.

    Telegram serves the message feed only for channels that have the web preview
    enabled; others redirect to the contact page (no feed). We detect that and
    skip silently for that channel.
    """
    url = f"https://t.me/s/{channel}"
    try:
        r = requests.get(url, headers={"User-Agent": BROWSER_UA},
                         timeout=TIMEOUT, allow_redirects=True)
    except requests.RequestException:
        return []
    if r.status_code != 200:
        return []
    # If Telegram bounced us off /s/, the feed isn't public — skip
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
        # Keyword filter — match against lowercased post text
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

        # thumbnail from photo/video preview if present
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
    """Search curated public Telegram channels via t.me/s/ preview (no auth).

    Fetches each channel in TELEGRAM_CHANNELS in parallel, filters recent posts
    for the keyword. Only covers channels the operator has added — Telegram has
    no accessible global channel index, so 'all of Telegram' isn't possible.
    """
    keyword = q.lstrip("#").strip()
    keyword_lc = keyword.lower()
    if not keyword:
        return {"platform": "telegram", "results": [], "error": "empty query"}
    if not TELEGRAM_CHANNELS:
        return {"platform": "telegram", "results": [], "error": "no channels configured"}

    all_posts = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_tg_channel, ch, keyword_lc): ch
                   for ch in TELEGRAM_CHANNELS}
        try:
            for fut in as_completed(futures, timeout=TIMEOUT + 6):
                try:
                    all_posts.extend(fut.result())
                except Exception:
                    pass
        except Exception:
            pass  # overall timeout — return what we have

    # Newest first, cap to 20
    all_posts.sort(key=lambda p: p.get("_ts_sort", ""), reverse=True)
    for p in all_posts:
        p.pop("_ts_sort", None)
    return {"platform": "telegram", "results": all_posts[:30], "error": None}


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

BABEL_CAP = 24          # Babel Street = one HTTP call per post; cap the second-opinion subset
BABEL_WORKERS = 12
BABEL_BUDGET = 12       # seconds — overall wall-clock budget for the Babel Street pass
BABEL_TIMEOUT = 10      # seconds per Babel Street call
_SCORE = {"positive": 1.0, "neutral": 0.0, "negative": -1.0}


def _norm_label(raw) -> str:
    lab = str(raw or "").lower()
    if "pos" in lab:
        return "positive"
    if "neg" in lab:
        return "negative"
    return "neutral"


def _babel_one(text: str):
    """Score one document via Babel Street /sentiment. Returns a label or None.
    Language is omitted so Babel auto-detects — keeps it multilingual-safe (e.g. Arabic channels)."""
    try:
        r = requests.post(
            "https://analytics.babelstreet.com/rest/v1/sentiment",
            headers={"X-BabelStreetAPI-Key": BABELSTREET_API_KEY,
                     "Content-Type": "application/json",
                     "Accept": "application/json"},
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
    """Score the given subset (indices into texts) concurrently, within BABEL_BUDGET.
    Returns {index: label} for whatever finished in time. Empty if no key / all failed."""
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
            pass  # budget hit — take whatever finished
    return out


def _sentiment_claude(texts: list) -> list | None:
    """Score sentiment via the Anthropic API. Returns list aligned to texts, or None.
    Caps at 120 posts per call so the JSON response can't overflow max_tokens."""
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
            headers={
                "x-api-key": ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 2000,
                "messages": [{"role": "user", "content": prompt}],
            },
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
    """Score every result with Claude, add a Babel Street second opinion on the top-engagement
    posts, reconcile the two, attach labels, and compute a net sentiment score. Mutates results."""
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

    # Claude scores the whole set in one batched call.
    claude = _sentiment_claude(texts)
    if claude:
        engines.append("claude")

    # Babel Street second opinion on the highest-engagement subset.
    order = sorted(range(len(flat)),
                   key=lambda i: int(flat[i][0].get("engagement") or 0), reverse=True)
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


# ---------- narratives: theme clustering from the fetched results ----------

def extract_narratives(platforms: dict, max_posts: int = 80, max_narratives: int = 6) -> list:
    """Cluster the fetched posts into recurring narratives via Claude. Returns [{label, count}]
    ranked by count. No acceleration % — that needs stored history, which XTag doesn't keep."""
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
    """Split a result's meta string into reactions / comments / shares using its markers.
    Views/plays (impressions) are deliberately excluded from engagement."""
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

    out["reactions"] += grab(r"\u2665\s*([\d,]+)")      # ♥ likes / favourites
    out["shares"]    += grab(r"\u21ba\s*([\d,]+)")      # ↺ reposts / retweets
    out["comments"]  += grab(r"\U0001f4ac\s*([\d,]+)")  # comments emoji
    out["reactions"] += grab(r"([\d,]+)\s*pts")          # reddit / HN points
    out["comments"]  += grab(r"([\d,]+)\s*comments")     # reddit / HN comments
    return out


def _engagement_from_meta(meta) -> int:
    """Sum the integer counts embedded in a result's meta string (likes/comments/plays/etc.).
    Real figures, parsed back out of the human-readable string — an engagement proxy for sorting."""
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
    """Best-effort parse of the many timestamp shapes the fetchers produce -> UTC datetime or None."""
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
    """Compute the dashboard's live tiles: totals (with engagement breakdown) + source mix."""
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

    # Check cache
    cache_key = q.lower()
    now = time.time()
    if cache_key in _cache:
        ts, cached = _cache[cache_key]
        if now - ts < CACHE_TTL:
            return jsonify({**cached, "cached": True})

    # Fire all platforms in parallel — direct APIs + Google CSE
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

    # Merge: direct API results first (richer metadata), then SerpApi dedupe-appended.
    # If direct API errored but SerpApi has results, clear the error so user sees data.
    out: dict[str, dict] = {}
    for pid in set(direct_out.keys()) | set(cse_out.keys()):
        direct = direct_out.get(pid)
        cse = cse_out.get(pid)
        if direct and cse:
            existing_urls = {r.get("url") for r in direct.get("results", []) if r.get("url")}
            cse_extra = [r for r in cse.get("results", [])
                         if r.get("url") and r["url"] not in existing_urls]
            merged_results = direct.get("results", []) + cse_extra
            # Prefer direct's error only if BOTH failed; otherwise show whichever has data
            if merged_results:
                error = None
            else:
                error = direct.get("error") or cse.get("error")
            out[pid] = {"platform": pid, "results": merged_results, "error": error}
        else:
            out[pid] = direct or cse

    # Numeric engagement on every result (parsed from the human-readable meta string).
    for group in out.values():
        for r in group.get("results", []):
            eb = _engagement_breakdown(r.get("meta"))
            r["engagement"] = eb["reactions"] + eb["comments"] + eb["shares"]

    # Sentiment — Claude + Babel Street, reconciled. Never allowed to crash a search.
    sentiment = {"scored": 0, "positive": 0, "neutral": 0, "negative": 0,
                 "net": None, "engines": [], "agreement": None, "babel_scored": 0}
    if SENTIMENT_ENABLED:
        try:
            sentiment = attach_sentiment(out)
        except Exception as e:
            app.logger.warning("sentiment failed: %s", e)
            sentiment["error"] = str(e)[:120]

    # Trending narratives — themes clustered from the fetched results (Claude). Best-effort.
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
    # Cap cache size (simple LRU-ish)
    if len(_cache) > 200:
        oldest = sorted(_cache.items(), key=lambda kv: kv[1][0])[:50]
        for k, _ in oldest:
            _cache.pop(k, None)
    return jsonify(payload)



@app.route("/api/brief", methods=["POST"])
def api_brief():
    """Grounded intelligence brief: Claude synthesises what the searched term is about,
    using the actual fetched posts as context plus its own knowledge. Cached per query."""
    body = request.get_json(silent=True) or {}
    q = (body.get("q") or "").strip()
    snippets = body.get("snippets") or []
    if not q:
        return jsonify({"error": "missing q"}), 400
    if not ANTHROPIC_API_KEY:
        return jsonify({"brief": None}), 200

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
            return jsonify({"brief": None}), 200
        data = r.json()
        brief = "".join(b.get("text", "") for b in data.get("content", [])
                        if b.get("type") == "text").strip()
        if not brief:
            return jsonify({"brief": None}), 200
        payload = {"brief": brief}
        _cache[cache_key] = (now, payload)
        return jsonify(payload)
    except Exception:
        return jsonify({"brief": None}), 200


@app.route("/healthz")
def health():
    return {
        "ok": True,
        "youtube_configured": bool(YOUTUBE_API_KEY),
        "serpapi_configured": bool(SERPAPI_KEY),
        "scrapebadger_configured": bool(SCRAPEBADGER_KEY),
        "sentiment_engine": ("babelstreet" if BABELSTREET_API_KEY else "claude" if ANTHROPIC_API_KEY else None),
        "telegram_channels": len(TELEGRAM_CHANNELS),
        "cache_size": len(_cache),
    }, 200


@app.route("/api/telegram/channels")
def telegram_channels():
    """Return the current curated Telegram channel list (for the UI)."""
    return {"channels": TELEGRAM_CHANNELS, "count": len(TELEGRAM_CHANNELS)}, 200


@app.route("/debug/tiktok")
def debug_tiktok():
    """Diagnostic: dump the raw ScrapeBadger TikTok response so we can map its real shape."""
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
        # summarise structure
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
    """Diagnostic: run the brief pipeline directly (GET, no cache) and surface the raw result/error."""
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
    """Diagnostic: hit SerpApi with a test query and return status + credit info."""
    debug_token = os.environ.get("DEBUG_TOKEN", "").strip()
    if debug_token and request.headers.get("X-Debug-Token") != debug_token:
        return {"error": "auth required"}, 401

    if not SERPAPI_KEY:
        return {"serpapi_key_set": False, "error": "SERPAPI_KEY not set"}, 200

    q = (request.args.get("q") or "test").strip()
    try:
        r = requests.get(
            "https://serpapi.com/search",
            params={"engine": "google", "q": f"{q} site:x.com",
                    "num": 3, "api_key": SERPAPI_KEY},
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
    """Show remaining SerpApi search credits this month."""
    debug_token = os.environ.get("DEBUG_TOKEN", "").strip()
    if debug_token and request.headers.get("X-Debug-Token") != debug_token:
        return {"error": "auth required"}, 401
    if not SERPAPI_KEY:
        return {"error": "SERPAPI_KEY not set"}, 200
    try:
        r = requests.get("https://serpapi.com/account",
                         params={"api_key": SERPAPI_KEY}, timeout=SERPAPI_TIMEOUT)
        return r.json(), r.status_code
    except Exception as e:
        return {"error": f"{type(e).__name__}: {str(e)[:160]}"}, 500


@app.route("/debug/scrapebadger")
def debug_scrapebadger():
    """Diagnostic: test ScrapeBadger Reddit search + show account credits."""
    debug_token = os.environ.get("DEBUG_TOKEN", "").strip()
    if debug_token and request.headers.get("X-Debug-Token") != debug_token:
        return {"error": "auth required"}, 401
    if not SCRAPEBADGER_KEY:
        return {"scrapebadger_key_set": False, "error": "SCRAPEBADGER_KEY not set"}, 200
    out = {"scrapebadger_key_last4": SCRAPEBADGER_KEY[-4:]}
    q = (request.args.get("q") or "test").strip()
    # Account info (no credits charged)
    try:
        acct = requests.get(f"{SB_BASE}/account",
                            headers={"x-api-key": SCRAPEBADGER_KEY}, timeout=SERPAPI_TIMEOUT)
        out["account_status"] = acct.status_code
        if acct.ok:
            out["account"] = acct.json()
    except Exception as e:
        out["account_error"] = f"{type(e).__name__}: {str(e)[:100]}"
    # Reddit search test
    try:
        r = requests.get(f"{SB_BASE}/reddit/search/posts",
                         params={"q": q, "limit": 3},
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


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8000, debug=True)
