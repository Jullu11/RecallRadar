-- Receipt originals live in a private Supabase Storage bucket. The backend uses
-- its service-role credential; browser/anon clients receive no storage policy.
insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values (
    'receipt-images',
    'receipt-images',
    false,
    10485760,
    array['image/jpeg', 'image/png', 'image/webp']
)
on conflict (id) do update
set public = false,
    file_size_limit = excluded.file_size_limit,
    allowed_mime_types = excluded.allowed_mime_types;

create table if not exists public.receipt_images (
    id uuid primary key default gen_random_uuid(),
    owner_handle text not null,
    fingerprint text not null check (fingerprint ~ '^[0-9a-f]{64}$'),
    storage_path text not null unique,
    mime_type text not null check (mime_type in ('image/jpeg', 'image/png', 'image/webp')),
    created_at timestamptz not null default now(),
    unique (owner_handle, fingerprint)
);

create index if not exists receipt_images_owner_idx on public.receipt_images (owner_handle);
alter table public.receipt_images enable row level security;
