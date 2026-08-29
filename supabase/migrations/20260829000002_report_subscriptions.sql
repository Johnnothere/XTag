-- ═══════════════════════════════════════════════════════════════════════════
-- Scheduled email reports.
--
-- A subscription = "send me an intelligence report on this query every N days".
-- The scheduler asks for rows where next_run_at <= now(), generates a fresh
-- search + narrative analysis for each, emails it, then advances next_run_at.
-- ═══════════════════════════════════════════════════════════════════════════

create table if not exists public.report_subscriptions (
    id              uuid primary key default gen_random_uuid(),
    email           text not null,
    query           text not null,
    -- Cadence in days: 2 = every two days, 7 = weekly, 30 = monthly.
    -- Stored as an integer rather than an enum so an arbitrary cadence is
    -- possible later without a migration.
    cadence_days    integer not null default 7 check (cadence_days between 1 and 365),
    active          boolean not null default true,
    created_at      timestamptz not null default now(),
    last_sent_at    timestamptz,
    -- Driven by the scheduler; indexed because that's the only query it runs.
    next_run_at     timestamptz not null default now(),
    last_status     text,
    last_error      text,
    send_count      integer not null default 0,
    -- Lets a recipient unsubscribe without authenticating. Random, unguessable.
    unsubscribe_token text not null default encode(gen_random_bytes(18), 'hex'),
    -- One subscription per (email, query); changing cadence updates in place.
    unique (email, query)
);

create index if not exists subs_due_idx
    on public.report_subscriptions (next_run_at)
    where active = true;
create index if not exists subs_email_idx on public.report_subscriptions (email);
create unique index if not exists subs_unsub_token_idx
    on public.report_subscriptions (unsubscribe_token);

-- Delivery log: keeps a record even if a subscription is later deleted, so
-- "did we actually send that report?" is answerable.
create table if not exists public.report_deliveries (
    id              bigserial primary key,
    subscription_id uuid,
    email           text not null,
    query           text not null,
    sent_at         timestamptz not null default now(),
    status          text not null,
    provider_id     text,
    error           text,
    mentions        integer,
    narrative_count integer,
    alert_count     integer
);

create index if not exists deliveries_time_idx on public.report_deliveries (sent_at desc);
create index if not exists deliveries_sub_idx on public.report_deliveries (subscription_id, sent_at desc);

-- Service-role only, same posture as the rest of the schema.
alter table public.report_subscriptions enable row level security;
alter table public.report_deliveries    enable row level security;
