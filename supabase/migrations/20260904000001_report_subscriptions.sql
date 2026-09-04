-- ═══════════════════════════════════════════════════════════════════════════
-- Scheduled email reports: the two tables the code has always assumed existed.
--
-- WHY THIS MIGRATION EXISTS
-- db.py has talked to `report_subscriptions` and `report_deliveries` since the
-- feature was written, and app.py exposes /api/subscribe, /api/subscriptions,
-- /api/subscribe/<id>, /unsubscribe and /api/reports/run on top of them — but
-- neither table was ever created. PostgREST answers 404, db._req logs and
-- returns None, and every one of those paths degrades quietly: /api/subscribe
-- reports "could not save subscription", /api/reports/run finds nothing due and
-- silently sends nothing, forever, on every deployment. The feature has been
-- dead in production for its whole life. This is the missing half.
--
-- The schema below is derived from the callers, not from a design document:
--
--   report_subscriptions
--     db.subscription_upsert         email, query, cadence_days, active,
--                                    next_run_at  (+ returns the whole row)
--     db.subscriptions_for_email     filters email, orders created_at desc
--     db.subscriptions_due           filters active + next_run_at, orders
--                                    next_run_at asc
--     db.subscription_mark_sent      last_sent_at, last_status, last_error,
--                                    next_run_at, send_count
--     db.subscription_by_token       filters unsubscribe_token
--     db.subscription_deactivate     sets active=false by id
--     app._run_one_subscription      reads id, email, query, cadence_days,
--                                    unsubscribe_token
--
--   report_deliveries
--     db.delivery_record             subscription_id, email, query, status,
--                                    provider_id, error, mentions,
--                                    narrative_count, alert_count
--
-- Access is service-role only, exactly as in the persistence-core migration:
-- RLS on with NO policies, so the anon/publishable key can read nothing even
-- if it leaks. The Flask service role bypasses RLS by design.
-- ═══════════════════════════════════════════════════════════════════════════

-- ── Subscriptions ──────────────────────────────────────────────────────────
-- "Email me an intelligence report on this query every N days."
--
-- id is a uuid, not a bigserial: db._pk() documents subscription ids as uuids,
-- and these ids travel in URLs (/api/subscribe/<sub_id>) where a sequential
-- integer would let anyone enumerate and deactivate other people's
-- subscriptions. A uuid text form passes db._pk()'s [A-Za-z0-9_-]{1,64} filter
-- unchanged.
create table if not exists public.report_subscriptions (
    id            uuid primary key default gen_random_uuid(),
    email         text not null,
    query         text not null,
    -- Drives next_run_at arithmetic in subscription_mark_sent. Bounded because
    -- a zero or negative cadence would reschedule every subscription into the
    -- past and turn /api/reports/run into a send loop; app.py validates against
    -- {2,7,14,30} but nothing stops a hand-written row.
    cadence_days  integer not null default 7 check (cadence_days between 1 and 365),
    active        boolean not null default true,
    created_at    timestamptz not null default now(),
    next_run_at   timestamptz not null default now(),
    last_sent_at  timestamptz,
    -- Written as 'sent' | 'failed' today. Deliberately NOT a check constraint:
    -- db._req swallows a 4xx and returns None, so a future status value would
    -- fail invisibly and lose the send record rather than raising anywhere.
    last_status   text,
    last_error    text,
    send_count    integer not null default 0,
    -- The unsubscribe link in every email. NOTHING in the application generates
    -- this — mailer.render_report is handed sub["unsubscribe_token"] straight
    -- from the row and db.subscription_by_token looks rows up by it — so the
    -- default here is the only thing that ever creates one. Two uuids with the
    -- dashes stripped give 64 hex chars / 244 bits, url-safe, and avoid making
    -- this migration depend on the pgcrypto extension for gen_random_bytes().
    unsubscribe_token text not null unique
        default (replace(gen_random_uuid()::text, '-', '')
              || replace(gen_random_uuid()::text, '-', '')),

    -- subscription_upsert() is documented as "unique on (email, query), so
    -- changing the cadence for an existing subscription updates it rather than
    -- duplicating". This is that constraint. Without it, re-subscribing creates
    -- a second row and the recipient gets the same report twice per cadence
    -- while unsubscribing only ever deactivates one of them.
    --
    -- CAVEAT FOR THE db.py SIDE: PostgREST's `Prefer: resolution=merge-duplicates`
    -- resolves against the PRIMARY KEY unless the request also passes
    -- `on_conflict=`. subscription_upsert() posts to "report_subscriptions"
    -- with no such parameter and sends no id, so the ON CONFLICT target is
    -- report_subscriptions_pkey, which can never conflict — a repeat subscribe
    -- hits THIS constraint and returns HTTP 409, which db._req turns into None
    -- and app.py into "could not save subscription" (500). The fix is one
    -- query parameter in db.py: POST "report_subscriptions?on_conflict=email,query".
    -- The constraint is kept because silently duplicating a recurring email is
    -- the worse failure of the two.
    constraint report_subscriptions_email_query_key unique (email, query)
);

-- subscriptions_for_email(): email=eq.<addr> ordered created_at desc.
create index if not exists report_subscriptions_email_idx
    on public.report_subscriptions (email, created_at desc);

-- subscriptions_due(): active=eq.true & next_run_at=lte.<now> ordered
-- next_run_at asc. Partial on active because the scheduler only ever looks at
-- live subscriptions and unsubscribed rows are kept forever.
create index if not exists report_subscriptions_due_idx
    on public.report_subscriptions (next_run_at)
    where active = true;

-- ── Delivery log ───────────────────────────────────────────────────────────
-- One row per send attempt, successful or not. Written by db.delivery_record()
-- from both the scheduled path and the ad-hoc test send (which passes
-- subscription_id = None), and never read back by the application today — it
-- exists so "did this address actually receive anything, and what did the
-- provider say" is answerable after the fact, which the subscription row's
-- last_status cannot do because it only remembers the most recent attempt.
--
-- subscription_id is intentionally NOT a foreign key, for the same reason
-- watchlist_snapshots.watchlist_id is not one: an ad-hoc send has no
-- subscription at all, and deleting a subscription must not erase the record
-- of the mail that was already sent to a real person.
create table if not exists public.report_deliveries (
    id              bigserial primary key,
    subscription_id uuid,
    email           text not null,
    query           text not null,
    created_at      timestamptz not null default now(),
    status          text not null,
    -- Resend's message id, for reconciling against the provider's own log.
    provider_id     text,
    error           text,
    -- Enough of the report's shape to see what was sent without regenerating it.
    mentions         integer,
    narrative_count  integer,
    alert_count      integer
);

-- No code filters this table yet; these are the two access paths any read of a
-- delivery log takes ("what happened to this subscription" / "what went out
-- recently"), and adding them now costs nothing on a table this narrow.
create index if not exists report_deliveries_sub_time_idx
    on public.report_deliveries (subscription_id, created_at desc);
create index if not exists report_deliveries_time_idx
    on public.report_deliveries (created_at desc);

-- ── Lock everything down ───────────────────────────────────────────────────
-- RLS on with zero policies = deny-all for anon/authenticated keys. These rows
-- hold email addresses and working unsubscribe tokens, so this matters more
-- here than anywhere else in the schema.
alter table public.report_subscriptions enable row level security;
alter table public.report_deliveries    enable row level security;
