create table if not exists public.price_watches (
    id uuid primary key default gen_random_uuid(),
    product_id uuid not null references public.products(id) on delete cascade,
    owner_handle text not null,
    provider text not null default 'keepa' check (provider in ('keepa')),
    asin text not null check (asin ~ '^[A-Z0-9]{10}$'),
    product_url text not null,
    target_price numeric(10,2) not null check (target_price > 0),
    current_price numeric(10,2) check (current_price is null or current_price >= 0),
    provider_title text,
    last_checked_at timestamptz,
    check_error text,
    is_active boolean not null default true,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);

create index if not exists price_watches_owner_active_idx
    on public.price_watches (owner_handle, is_active, created_at);

alter table public.price_watches enable row level security;
