"""XTag persistence layer (Supabase / Postgres via PostgREST).

Why this exists
---------------
Everything XTag knew used to live in process memory: watchlists, the search
cache, and every narrative snapshot. A Railway redeploy wiped all of it, so
"track narratives over time" could never actually work — there was no
"over time" to speak of. This module gives that state a home.

Design rules
------------
1. **Never break the app.** Every function degrades gracefully. If the DB is
   unconfigured or unreachable, callers get a falsy/empty result and the app
   carries on with in-memory behaviour. Persistence is an upgrade, not a
   dependency.
2. **No new packages.** Talks to PostgREST with `requests`, which is already
   a dependency. Avoids pulling supabase-py and its transitive tree.
3. **Service role only.** These tables are RLS deny-all; the service key is
   what makes them readable. It must never reach the browser.
"""
from __future__ import annotations

import hmac
import logging
import os
import random
import re
import time as _time
from datetime import datetime, timedelta, timezone

import requests

log = logging.getLogger("xtag.db")

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
# Accept either name — Supabase has renamed this key over time and it's an
# easy thing to get wrong when copying from the dashboard.
SUPABASE_SERVICE_KEY = (
    os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    or os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
)

DB_ENABLED = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
_REST = f"{SUPABASE_URL}/rest/v1" if SUPABASE_URL else ""
TIMEOUT = 8


def _headers(extra: dict | None = None) -> dict:
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
    }
    if extra:
        h.update(extra)
    return h


def _req(method: str, path: str, **kw):
    """Single choke point for every DB call.

    Returns the parsed body on success, or None on any failure. Callers treat
    None as "the DB didn't answer" and fall back — no exception ever escapes
    this module into a request handler.
    """
    if not DB_ENABLED:
        return None
    try:
        r = requests.request(
            method, f"{_REST}/{path}", headers=_headers(kw.pop("extra_headers", None)),
            timeout=TIMEOUT, **kw
        )
        if r.status_code >= 400:
            log.warning("supabase %s %s -> HTTP %s: %s",
                        method, path, r.status_code, r.text[:200])
            return None
        if r.status_code == 204 or not r.content:
            return []
        return r.json()
    except Exception as e:
        log.warning("supabase %s %s failed: %s", method, path, str(e)[:200])
        return None


# Ids reaching this module are either a sha256 prefix (watchlists: hex, 12
# chars) or a uuid (subscriptions), so hex/uuid characters are the whole legal
# alphabet. 64 is a generous ceiling on both.
_PK_RE = re.compile(r"\A[A-Za-z0-9_-]{1,64}\Z")


def _pk(value) -> str | None:
    """Validate a value before it is interpolated into a PostgREST path.

    A5: ids went into the path raw — f"watchlists?id=eq.{wl_id}". In PostgREST
    the path IS the query language, so an id there is not inert data: an id of
    "x&or=(id.not.is.null)" appends a second filter and turns a one-row DELETE
    into "delete every watchlist", and "x&select=*" reshapes a read. These ids
    arrive from URL path segments (/api/watchlist/<wl_id>) and from an
    unsubscribe form, so they are attacker-controlled.

    Percent-encoding is NOT a fix here: PostgREST decodes the query string
    before it parses filters, so an encoded "&" still separates parameters.
    The only safe treatment is to refuse anything that is not an id.

    Returns the value unchanged when it is one, else None. Callers turn None
    into the same empty/False result they already return for a missing row —
    a forged id deserves exactly the "not found" a wrong id gets, and the
    module's rule that a caller is never handed an exception still holds.

    Integers are accepted and stringified: report_subscriptions.id may be a
    bigserial depending on when the schema was applied, and PostgREST hands
    those back as JSON numbers, so app.py legitimately passes an int straight
    back in. Rejecting it would have broken unsubscribe on those deployments.
    """
    if isinstance(value, bool):        # bool is an int subclass; never an id
        return None
    if isinstance(value, int):
        value = str(value)
    if not isinstance(value, str):
        return None
    v = value.strip()
    if not _PK_RE.match(v):
        log.warning("rejected unsafe path parameter: %r", value[:80])
        return None
    return v


_health_cache: dict = {"at": 0.0, "value": None}
_HEALTH_TTL = 120


def health(force: bool = False) -> dict:
    """Report whether persistence *actually works*, not merely that it's set.

    A read-only probe is not enough: these tables are RLS deny-all, so the
    anon/publishable key returns a clean empty list rather than an error. That
    would look identical to "healthy but no data yet", and XTag would report
    itself fine while silently persisting nothing — the worst kind of failure
    for a system whose whole job is accumulating history.

    So the probe is a real write round-trip against search_cache (an expiring,
    throwaway table). Only the service role can do it. Result is cached
    briefly so /api/status stays cheap.
    """
    import time as _time
    if not DB_ENABLED:
        return {"enabled": False, "reachable": False, "writable": False,
                "reason": "SUPABASE_URL / SUPABASE_SERVICE_KEY not set"}

    if not force and _health_cache["value"] and \
            _time.time() - _health_cache["at"] < _HEALTH_TTL:
        return _health_cache["value"]

    readable = _req("GET", "watchlists?select=id&limit=1") is not None

    probe_key = "__xtag_health_probe__"
    wrote = _req(
        "POST", "search_cache",
        json={"query_key": probe_key, "query": "__health__", "payload": {"ok": True},
              "cached_at": datetime.now(timezone.utc).isoformat(),
              "expires_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat()},
        extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )
    writable = wrote is not None
    if writable:
        _req("DELETE", f"search_cache?query_key=eq.{probe_key}",
             extra_headers={"Prefer": "return=minimal"})

    if writable:
        reason = None
    elif readable:
        reason = ("connected but NOT writable — this is almost certainly the anon/"
                  "publishable key. Set SUPABASE_SERVICE_KEY to the service_role "
                  "key or nothing will persist.")
    else:
        reason = "configured but unreachable — check SUPABASE_URL and the key"

    value = {"enabled": True, "reachable": readable or writable,
             "writable": writable, "reason": reason}
    _health_cache["at"] = _time.time()
    _health_cache["value"] = value
    return value


# ── Watchlists ────────────────────────────────────────────────────────────────

def watchlists_list() -> list[dict] | None:
    return _req("GET", "watchlists?select=*&order=created_at.desc")


def watchlist_get(wl_id: str) -> dict | None:
    wl_id = _pk(wl_id)
    if not wl_id:
        return None
    rows = _req("GET", f"watchlists?id=eq.{wl_id}&select=*&limit=1")
    return rows[0] if rows else None


def watchlist_upsert(entry: dict) -> dict | None:
    """Insert or update by primary key. Used for both create and check-update."""
    rows = _req(
        "POST", "watchlists", json=entry,
        extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )
    return rows[0] if rows else None


def watchlist_delete(wl_id: str) -> bool:
    # Unvalidated, this is the worst of the interpolations: an extra filter
    # smuggled through wl_id widens the DELETE instead of narrowing it.
    wl_id = _pk(wl_id)
    if not wl_id:
        return False
    return _req("DELETE", f"watchlists?id=eq.{wl_id}") is not None


def watchlist_touch(wl_id: str, checked_at: str, snapshot: dict, alerts: list) -> None:
    """Update the denormalised 'latest state' columns after a check."""
    wl_id = _pk(wl_id)
    if not wl_id:
        return
    _req("PATCH", f"watchlists?id=eq.{wl_id}",
         json={"last_checked": checked_at, "last_snapshot": snapshot,
               "last_alerts": alerts})


# ── Snapshots (the time series) ───────────────────────────────────────────────

def snapshot_record(query: str, result: dict, watchlist_id: str | None = None,
                    sentiment: dict | None = None) -> None:
    """Persist one watchlist check as a permanent time-series row.

    Called on every check, including ad-hoc ones with no saved watchlist —
    observing something is worth recording whether or not a watchlist exists.
    """
    snap = result.get("snapshot") or {}
    sent = sentiment or {}
    _req("POST", "watchlist_snapshots", json={
        "watchlist_id": watchlist_id,
        "query": query,
        "checked_at": result.get("checked_at") or datetime.now(timezone.utc).isoformat(),
        "mentions": snap.get("mentions"),
        "coordination_score": snap.get("coordination_score"),
        "acceleration": snap.get("acceleration"),
        "state_media": snap.get("state_media"),
        "sentiment_net": sent.get("net"),
        "sentiment_positive": sent.get("positive"),
        "sentiment_negative": sent.get("negative"),
        "sentiment_neutral": sent.get("neutral"),
        "narratives": snap.get("narratives") or [],
        "alert_count": result.get("alert_count", 0),
    }, extra_headers={"Prefer": "return=minimal"})


def _like_literal(value: str) -> str:
    """Escape a value so a PostgREST `like`/`ilike` filter matches it literally.

    A4: `query` was interpolated straight into an `ilike.` filter. `%` and `_`
    are SQL LIKE wildcards and requests.utils.quote does not neutralise them —
    it is a URL encoder, not a LIKE escaper — so a query of "%" read back every
    snapshot in the table for every query, and "a_c" silently matched "abc".
    Backslash is Postgres' default LIKE escape character, so escaping `\`, `%`
    and `_` with it makes the pattern literal.

    `ilike` is kept rather than switching to `eq.` because the callers depend on
    the case-insensitivity: /api/history passes whatever the user typed, while
    the rows were written with the casing used at collection time, so "Hezbollah"
    and "hezbollah" must return one series, not two.
    """
    return (str(value or "")
            .replace("\\", "\\\\")
            .replace("%", "\\%")
            .replace("_", "\\_"))


def snapshots_history(query: str, limit: int = 200, days: int | None = None) -> list[dict] | None:
    """Chronological history for a query — oldest first, ready to plot."""
    # PostgREST additionally aliases `*` to `%` inside like/ilike patterns, and
    # that substitution happens before the SQL escape character is consulted, so
    # there is no way to escape it. A query containing `*` therefore uses an
    # exact (case-sensitive) match instead — narrower than intended, but it can
    # never widen into "every row in the table".
    if "*" in (query or ""):
        q = f"watchlist_snapshots?query=eq.{requests.utils.quote(query)}"
    else:
        q = f"watchlist_snapshots?query=ilike.{requests.utils.quote(_like_literal(query))}"
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q += f"&checked_at=gte.{since}"
    # A3: this ordered ASC with the limit applied, which truncates the RECENT
    # end — once a query had more snapshots than `limit`, history froze at the
    # oldest N rows and every new snapshot was invisible forever. Take the most
    # recent N, then reverse so callers still get oldest-first for plotting.
    q += f"&select=*&order=checked_at.desc&limit={int(limit)}"
    rows = _req("GET", q)
    if rows is None:
        return None
    return list(reversed(rows))


# ── Alerts ────────────────────────────────────────────────────────────────────

def alerts_record(query: str, alerts: list, watchlist_id: str | None = None,
                  triggered_at: str | None = None) -> None:
    if not alerts:
        return
    ts = triggered_at or datetime.now(timezone.utc).isoformat()
    _req("POST", "alerts", json=[{
        "watchlist_id": watchlist_id,
        "query": query,
        "triggered_at": ts,
        "alert_type": a.get("type"),
        "severity": a.get("severity"),
        "message": a.get("message"),
        "evidence": a.get("evidence") or [],
    } for a in alerts], extra_headers={"Prefer": "return=minimal"})


def alerts_recent(limit: int = 50, days: int | None = None,
                  unacknowledged_only: bool = False) -> list[dict] | None:
    q = "alerts?select=*"
    if days:
        since = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        q += f"&triggered_at=gte.{since}"
    if unacknowledged_only:
        q += "&acknowledged=eq.false"
    q += f"&order=triggered_at.desc&limit={int(limit)}"
    return _req("GET", q)


# ── Search cache ──────────────────────────────────────────────────────────────

def cache_get(key: str) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    rows = _req("GET",
                f"search_cache?query_key=eq.{requests.utils.quote(key)}"
                f"&expires_at=gt.{now}&select=payload&limit=1")
    return rows[0]["payload"] if rows else None


def cache_set(key: str, query: str, payload: dict, ttl_seconds: int) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)).isoformat()
    _req("POST", "search_cache",
         json={"query_key": key, "query": query, "payload": payload,
               "cached_at": datetime.now(timezone.utc).isoformat(),
               "expires_at": expires},
         extra_headers={"Prefer": "resolution=merge-duplicates,return=minimal"})
    # A6: cache_prune() existed but nothing ever called it, so search_cache only
    # ever grew — expired rows were skipped by the expires_at filter in
    # cache_get() but never deleted, and the table (and its index) carried every
    # payload the app had ever cached, forever. Pruning is hung off cache_set
    # because that is already a write path (a miss that just paid for upstream
    # calls), never off cache_get, which is the hot path.
    maybe_prune()


def cache_prune() -> None:
    now = datetime.now(timezone.utc).isoformat()
    _req("DELETE", f"search_cache?expires_at=lt.{now}",
         extra_headers={"Prefer": "return=minimal"})


# Housekeeping only needs to happen occasionally; what it must never do is add
# a round-trip to a request that would otherwise make none.
_PRUNE_INTERVAL = 3600          # at most one prune per worker per hour
_PRUNE_PROBABILITY = 0.05       # ...and only 1 write in 20 even considers it
_last_prune_at = 0.0


def maybe_prune(now: float | None = None) -> bool:
    """Prune expired cache rows occasionally. Returns True if it pruned.

    Two gates, because either alone has a failure mode. The time gate alone
    means a burst of concurrent misses right after the interval lapses all see
    a stale timestamp and every one of them fires a DELETE. The dice alone mean
    a busy instance prunes far more often than the table needs (a 5% roll on a
    write-heavy hour is dozens of DELETEs). Together: at most one prune per
    worker per hour, and the workers that hit the same moment mostly roll away.

    Both gates are pure local arithmetic — no DB round-trip is made to decide
    whether to make one — so the common case costs a comparison and a random().
    """
    global _last_prune_at
    t = _time.time() if now is None else now
    if t - _last_prune_at < _PRUNE_INTERVAL:
        return False
    if random.random() >= _PRUNE_PROBABILITY:
        return False
    # Stamp BEFORE the request, not after: if the DELETE hangs or fails, the
    # next write must not immediately retry it and turn a broken prune into a
    # per-request round-trip.
    _last_prune_at = t
    cache_prune()
    return True


# ── Report subscriptions ──────────────────────────────────────────────────────

def subscription_upsert(email: str, query: str, cadence_days: int) -> dict | None:
    """Create or update a subscription. Unique on (email, query), so changing
    the cadence for an existing subscription updates it rather than duplicating."""
    from datetime import datetime as _dt
    rows = _req(
        # on_conflict is REQUIRED here. PostgREST's merge-duplicates resolution
        # targets the PRIMARY KEY unless the request names the conflict target,
        # and no id is sent — so ON CONFLICT could never fire, and a repeat
        # subscribe hit the unique (email, query) constraint, returned 409, and
        # surfaced to the user as "could not save subscription".
        "POST", "report_subscriptions?on_conflict=email,query",
        json={"email": email, "query": query, "cadence_days": cadence_days,
              "active": True, "next_run_at": _dt.now(timezone.utc).isoformat()},
        extra_headers={"Prefer": "resolution=merge-duplicates,return=representation"},
    )
    return rows[0] if rows else None


def subscriptions_for_email(email: str) -> list[dict] | None:
    return _req("GET", f"report_subscriptions?email=eq.{requests.utils.quote(email)}"
                       f"&select=*&order=created_at.desc")


def subscriptions_due(limit: int = 25) -> list[dict] | None:
    now = datetime.now(timezone.utc).isoformat()
    return _req("GET", f"report_subscriptions?active=eq.true&next_run_at=lte.{now}"
                       f"&select=*&order=next_run_at.asc&limit={int(limit)}")


def subscription_mark_sent(sub_id: str, cadence_days: int, status: str,
                           error: str | None = None) -> None:
    sub_id = _pk(sub_id)
    if not sub_id:
        return
    now = datetime.now(timezone.utc)
    payload = {"last_sent_at": now.isoformat(), "last_status": status,
               "last_error": error,
               "next_run_at": (now + timedelta(days=cadence_days)).isoformat()}
    _req("PATCH", f"report_subscriptions?id=eq.{sub_id}", json=payload,
         extra_headers={"Prefer": "return=minimal"})
    # send_count increments separately; a failed send still reschedules so one
    # bad run doesn't silently kill a subscription forever.
    if status == "sent":
        cur = _req("GET", f"report_subscriptions?id=eq.{sub_id}&select=send_count")
        if cur:
            _req("PATCH", f"report_subscriptions?id=eq.{sub_id}",
                 json={"send_count": (cur[0].get("send_count") or 0) + 1},
                 extra_headers={"Prefer": "return=minimal"})


def subscription_by_token(token: str) -> dict | None:
    """Look a subscription up by its unsubscribe token.

    The match is made server-side by the filter, but the row that comes back is
    re-checked here with hmac.compare_digest before it is handed to the caller.
    Two reasons. The filter is only as exact as the encoding of the value put
    into it, and this token comes straight from a URL query string or a form
    post; a token that ever matched more loosely than intended would silently
    unsubscribe (or expose the query of) someone else's subscription. And an
    equality check on a secret is exactly the place a short-circuiting `==`
    leaks its answer through timing — compare_digest does not.
    """
    if not isinstance(token, str) or not token:
        return None
    rows = _req("GET", f"report_subscriptions?unsubscribe_token=eq."
                       f"{requests.utils.quote(token)}&select=*&limit=1")
    if not rows:
        return None
    row = rows[0]
    stored = row.get("unsubscribe_token")
    if not isinstance(stored, str) or not hmac.compare_digest(stored, token):
        log.warning("token lookup returned a row whose token does not match; ignoring")
        return None
    return row


def subscription_deactivate(sub_id: str) -> bool:
    sub_id = _pk(sub_id)
    if not sub_id:
        return False
    return _req("PATCH", f"report_subscriptions?id=eq.{sub_id}",
                json={"active": False},
                extra_headers={"Prefer": "return=minimal"}) is not None


def delivery_record(sub_id: str | None, email: str, query: str, status: str,
                    provider_id: str | None = None, error: str | None = None,
                    mentions: int | None = None, narrative_count: int | None = None,
                    alert_count: int | None = None) -> None:
    _req("POST", "report_deliveries",
         json={"subscription_id": sub_id, "email": email, "query": query,
               "status": status, "provider_id": provider_id, "error": error,
               "mentions": mentions, "narrative_count": narrative_count,
               "alert_count": alert_count},
         extra_headers={"Prefer": "return=minimal"})
