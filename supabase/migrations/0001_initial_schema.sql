create extension if not exists pgcrypto;

create table if not exists public.products (
    id uuid primary key default gen_random_uuid(),
    owner_handle text not null,
    brand text,
    product_name text not null,
    identifiers jsonb not null default '{}'::jsonb,
    source_type text not null check (source_type in ('receipt', 'label', 'manual')),
    monitoring_enabled boolean not null default false,
    monitoring_consented_at timestamptz,
    status text not null default 'owned' check (status in ('owned', 'consumed', 'returned', 'removed', 'no_longer_owned')),
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),
    check (monitoring_enabled = false or monitoring_consented_at is not null)
);

create table if not exists public.notices (
    id uuid primary key default gen_random_uuid(),
    agency text not null,
    external_id text not null,
    title text not null,
    official_url text not null,
    affected_criteria jsonb not null default '{}'::jsonb,
    retrieved_at timestamptz not null default now(),
    historical_fixture boolean not null default false,
    unique (agency, external_id)
);

create table if not exists public.matches (
    id uuid primary key default gen_random_uuid(),
    product_id uuid not null references public.products(id) on delete cascade,
    notice_id uuid not null references public.notices(id) on delete cascade,
    decision text not null check (decision in ('affected', 'possible_match', 'no_match')),
    evidence jsonb not null default '{}'::jsonb,
    checked_at timestamptz not null default now(),
    unique (product_id, notice_id)
);

create table if not exists public.follow_ups (
    id uuid primary key default gen_random_uuid(),
    product_id uuid not null references public.products(id) on delete cascade,
    notice_id uuid references public.notices(id) on delete set null,
    action text not null,
    due_at timestamptz,
    acknowledged_at timestamptz,
    completed_at timestamptz,
    created_at timestamptz not null default now()
);

create table if not exists public.photon_events (
    event_id text primary key,
    owner_handle text not null,
    event_type text not null,
    received_at timestamptz not null default now(),
    processed_at timestamptz,
    processing_error text
);

create table if not exists public.conversations (
    owner_handle text primary key,
    state text not null default 'idle',
    payload jsonb not null default '{}'::jsonb,
    updated_at timestamptz not null default now()
);

create index if not exists products_owner_handle_idx on public.products (owner_handle);
create index if not exists photon_events_owner_handle_idx on public.photon_events (owner_handle);
create index if not exists products_monitoring_idx on public.products (monitoring_enabled) where monitoring_enabled;
create index if not exists follow_ups_due_idx on public.follow_ups (due_at) where completed_at is null;

-- The app connects only with a Supabase service-role key kept on the backend.
-- Client/anon access remains disabled so iMessage sender checks stay in the API layer.
alter table public.products enable row level security;
alter table public.notices enable row level security;
alter table public.matches enable row level security;
alter table public.follow_ups enable row level security;
alter table public.photon_events enable row level security;
alter table public.conversations enable row level security;
