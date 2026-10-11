create table if not exists public.notification_outbox (
    id uuid primary key default gen_random_uuid(),
    owner_handle text not null,
    dedupe_key text not null unique,
    message text not null,
    created_at timestamptz not null default now(),
    sent_at timestamptz
);

create index if not exists notification_outbox_pending_idx
    on public.notification_outbox (created_at)
    where sent_at is null;

create index if not exists notification_outbox_owner_idx
    on public.notification_outbox (owner_handle);

alter table public.notification_outbox enable row level security;
