create table if not exists public.account_links (
    user_id uuid primary key,
    owner_handle text not null unique,
    linked_at timestamptz not null default now()
);

create table if not exists public.account_link_codes (
    code_hash text primary key check (code_hash ~ '^[0-9a-f]{64}$'),
    user_id uuid not null,
    expires_at timestamptz not null,
    consumed_at timestamptz,
    created_at timestamptz not null default now()
);

create index if not exists account_link_codes_expiration_idx
    on public.account_link_codes (expires_at);

alter table public.account_links enable row level security;
alter table public.account_link_codes enable row level security;
