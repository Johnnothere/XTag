-- ═══════════════════════════════════════════════════════════════════════════
-- XTag persistence layer.
--
-- Everything XTag knew used to live in process memory: watchlists, the search
-- cache, and every narrative snapshot. A Railway redeploy wiped all of it,
-- which meant "track narratives over time" could never actually work — there
-- was no "over time" to speak of.
--
-- Access is service-role only (the Flask backend). RLS is enabled with NO
-- permissive policies, so the anon/publishable key can read nothing even if
-- it leaks. The service key bypasses RLS by design.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── Watchlists ─────────────────────────────────────────────────────────────
-- A saved query plus its alert thresholds. id is the sha256[:12] of the
-- lowercased query, matching what app.py already generates, so existing
-- client-side watchlists keep their identity when they sync up.
create table if not exists public.watchlists (
    id           text primary key,
    query        text not null,
    rules        jsonb not null default '{}'::jsonb,
    created_at   timestamptz not null default now(),
    last_checked timestamptz,
    -- Denormalised copy of the most recent snapshot + alerts, so the
    -- watchlist list renders without a join or a second round trip.
    last_snapshot jsonb,
    last_alerts   jsonb not null default '[]'::jsonb
);

create index if not exists watchlists_created_idx on public.watchlists (created_at desc);

-- ── Snapshots: the actual time series ──────────────────────────────────────
-- One row per watchlist check, kept forever. This is the table that makes
-- longitudinal narrative intelligence possible — velocity trends, sentiment
-- drift, when a narrative first appeared and how it accelerated.
--
-- watchlist_id is intentionally NOT a foreign key: ad-hoc checks (a query
-- checked without saving a watchlist) still deserve history, and deleting a
-- watchlist should not erase the record of what was observed.
create table if not exists public.watchlist_snapshots (
    id                 bigserial primary key,
    watchlist_id       text,
    query              text not null,
    checked_at         timestamptz not null default now(),
    mentions           integer,
    coordination_score integer,
    acceleration       text,
    state_media        integer,
    sentiment_net      numeric,
    sentiment_positive integer,
    sentiment_negative integer,
    sentiment_neutral  integer,
    narratives         jsonb not null default '[]'::jsonb,
    alert_count        integer not null default 0,
    raw                jsonb
);

-- Primary access pattern: "give me this query's history, newest first."
create index if not exists snapshots_query_time_idx
    on public.watchlist_snapshots (lower(query), checked_at desc);
create index if not exists snapshots_watchlist_time_idx
    on public.watchlist_snapshots (watchlist_id, checked_at desc);
create index if not exists snapshots_time_idx
    on public.watchlist_snapshots (checked_at desc);

-- ── Alert history ──────────────────────────────────────────────────────────
-- Every triggered alert, kept separately from snapshots so "show me every
-- coordination spike this month" is a cheap indexed query rather than a
-- jsonb scan across every snapshot ever taken.
create table if not exists public.alerts (
    id           bigserial primary key,
    watchlist_id text,
    query        text not null,
    triggered_at timestamptz not null default now(),
    alert_type   text not null,
    severity     text,
    message      text,
    evidence     jsonb not null default '[]'::jsonb,
    acknowledged boolean not null default false
);

create index if not exists alerts_time_idx on public.alerts (triggered_at desc);
create index if not exists alerts_query_time_idx on public.alerts (lower(query), triggered_at desc);
create index if not exists alerts_unack_idx on public.alerts (acknowledged, triggered_at desc)
    where acknowledged = false;

-- ── Search cache ───────────────────────────────────────────────────────────
-- Survives redeploys, so a restart no longer means re-paying for every
-- upstream API call that had already been made. expires_at drives eviction.
create table if not exists public.search_cache (
    query_key  text primary key,
    query      text not null,
    payload    jsonb not null,
    cached_at  timestamptz not null default now(),
    expires_at timestamptz not null
);

create index if not exists search_cache_expiry_idx on public.search_cache (expires_at);

-- Housekeeping for expired cache rows. Called opportunistically by the app;
-- safe to run at any time.
create or replace function public.prune_expired_cache()
returns integer
language plpgsql
security invoker
set search_path = ''
as $$
declare
    removed integer;
begin
    delete from public.search_cache where expires_at < now();
    get diagnostics removed = row_count;
    return removed;
end;
$$;

-- ── Lock everything down ───────────────────────────────────────────────────
-- RLS on with zero policies = deny-all for anon/authenticated keys.
-- The Flask service role bypasses RLS, which is the only intended access path.
alter table public.watchlists          enable row level security;
alter table public.watchlist_snapshots enable row level security;
alter table public.alerts              enable row level security;
alter table public.search_cache        enable row level security;
