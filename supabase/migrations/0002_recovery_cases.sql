create table if not exists public.recovery_cases (
    id uuid primary key default gen_random_uuid(),
    product_id uuid not null references public.products(id) on delete cascade,
    case_type text not null check (case_type in ('return', 'warranty', 'price_adjustment')),
    due_date date not null,
    status text not null default 'open' check (status in ('open', 'completed', 'not_applicable')),
    date_source text not null default 'user_confirmed' check (date_source in ('user_confirmed')),
    created_at timestamptz not null default now(),
    completed_at timestamptz,
    check ((status = 'completed') = (completed_at is not null))
);

create index if not exists recovery_cases_product_due_idx
    on public.recovery_cases (product_id, due_date)
    where status = 'open';

alter table public.recovery_cases enable row level security;
