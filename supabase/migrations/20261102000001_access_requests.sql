-- ═══════════════════════════════════════════════════════════════════════════
-- XTag access requests.
--
-- The public landing page (xtag.up.railway.app) no longer opens the dashboard
-- directly. "Open dashboard" now routes a prospective user to a request-access
-- form. Each submission lands here, and Tej reviews it from the admin console
-- on the private console instance.
--
-- Access is service-role only (the Flask backend), same as every other XTag
-- table. RLS is enabled with NO permissive policies, so the anon/publishable
-- key can read nothing even if it leaks — these rows are PII (names, emails,
-- LinkedIn URLs of people who asked for access) and must never reach a browser
-- except through the service-key-backed admin console.
-- ═══════════════════════════════════════════════════════════════════════════

create table if not exists public.access_requests (
    id          uuid primary key default gen_random_uuid(),
    name        text not null,
    email       text not null,
    org         text,
    role        text,
    use_case    text,
    heard       text,              -- how they heard / LinkedIn or website URL
    status      text not null default 'pending',   -- pending | approved | rejected
    note        text,              -- Tej's private note on the request
    created_at  timestamptz not null default now(),
    decided_at  timestamptz,
    -- Light request context, useful for spotting spam/duplicates. Not PII
    -- beyond what the submitter already provided.
    source_ip   text,
    user_agent  text
);

-- Admin console lists newest first, and filters by status.
create index if not exists access_requests_created_idx
    on public.access_requests (created_at desc);
create index if not exists access_requests_status_idx
    on public.access_requests (status, created_at desc);

-- RLS: deny-all. Service role bypasses; nobody else reads or writes.
alter table public.access_requests enable row level security;
