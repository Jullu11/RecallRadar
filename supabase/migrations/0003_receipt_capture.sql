alter table public.products
    add column if not exists field_provenance jsonb not null default '{}'::jsonb,
    add column if not exists receipt_fingerprint text,
    add column if not exists receipt_item_index integer;

do $$
begin
    if not exists (
        select 1 from pg_constraint
        where conname = 'products_receipt_fingerprint_pair_check'
          and conrelid = 'public.products'::regclass
    ) then
        alter table public.products
            add constraint products_receipt_fingerprint_pair_check
            check (
                (receipt_fingerprint is null and receipt_item_index is null)
                or (receipt_fingerprint is not null and receipt_item_index is not null and receipt_item_index > 0)
            );
    end if;
end $$;

create unique index if not exists products_receipt_item_unique_idx
    on public.products (owner_handle, receipt_fingerprint, receipt_item_index)
    where receipt_fingerprint is not null and receipt_item_index is not null;
