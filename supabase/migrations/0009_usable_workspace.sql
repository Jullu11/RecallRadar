-- Complete browser workspace. Originals remain private; only the backend accesses them.
create table if not exists public.workspace_records (
    id uuid primary key default gen_random_uuid(),
    owner_handle text not null,
    kind text not null check (kind in ('benefit', 'claim', 'recall')),
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now()
);
alter table public.workspace_records enable row level security;
create index if not exists workspace_records_owner_kind_idx on public.workspace_records(owner_handle, kind);

alter table public.receipt_images add column if not exists filename text not null default 'Purchase document';
alter table public.receipt_images drop constraint if exists receipt_images_mime_type_check;
alter table public.receipt_images add constraint receipt_images_mime_type_check
    check (mime_type in ('image/jpeg','image/png','image/webp','application/pdf','message/rfc822'));
update storage.buckets set allowed_mime_types = array['image/jpeg','image/png','image/webp','application/pdf','message/rfc822']
where id = 'receipt-images';

-- Account identity is stable even when a phone is paired. Existing phone records
-- are merged into the account once. Object paths do not change or become public.
create or replace function public.merge_lodestone_owner(old_owner text, new_owner text)
returns void language plpgsql set search_path = public as $$
begin
    -- Preserve both confirmed products if the same receipt was captured on both surfaces.
    update products p set receipt_fingerprint = null, receipt_item_index = null
    where p.owner_handle = old_owner and exists (
        select 1 from products q where q.owner_handle = new_owner
        and q.receipt_fingerprint = p.receipt_fingerprint and q.receipt_item_index = p.receipt_item_index);
    update products set owner_handle = new_owner where owner_handle = old_owner;
    -- Identical bytes at two object paths are retained. Keep the existing account metadata.
    update receipt_images a set owner_handle = new_owner where a.owner_handle = old_owner
      and not exists (select 1 from receipt_images b where b.owner_handle = new_owner and b.fingerprint = a.fingerprint);
    update price_watches set owner_handle = new_owner where owner_handle = old_owner;
    update notification_outbox set owner_handle = new_owner where owner_handle = old_owner;
    update workspace_records set owner_handle = new_owner where owner_handle = old_owner;
    -- Reset transient extraction state at pairing; originals and confirmed records are preserved.
    delete from conversations where owner_handle = old_owner;
    update photon_events set owner_handle = new_owner where owner_handle = old_owner;
end $$;
revoke all on function public.merge_lodestone_owner(text,text) from public, anon, authenticated;
grant execute on function public.merge_lodestone_owner(text,text) to service_role;

create or replace function public.redeem_lodestone_link(p_code_hash text, p_sender text)
returns boolean language plpgsql set search_path = public as $$
declare token_row account_link_codes%rowtype;
begin
    select * into token_row from account_link_codes where code_hash = p_code_hash
        and consumed_at is null and expires_at > now() for update;
    if not found then return false; end if;
    if exists (select 1 from account_links where owner_handle = p_sender and user_id <> token_row.user_id)
       or exists (select 1 from account_links where user_id = token_row.user_id and owner_handle <> p_sender) then
        return false;
    end if;
    insert into account_links(user_id, owner_handle) values(token_row.user_id, p_sender)
        on conflict(user_id) do nothing;
    perform merge_lodestone_owner(p_sender, 'account:' || token_row.user_id::text);
    update account_link_codes set consumed_at = now() where code_hash = p_code_hash;
    return true;
exception when unique_violation then return false;
end $$;
revoke all on function public.redeem_lodestone_link(text,text) from public, anon, authenticated;
grant execute on function public.redeem_lodestone_link(text,text) to service_role;

do $$ declare link record; begin
    for link in select * from account_links loop
        perform merge_lodestone_owner(link.owner_handle, 'account:' || link.user_id::text);
    end loop;
end $$;

-- Leases prevent two gateway/cron workers from delivering/checking the same item.
alter table notification_outbox add column if not exists lease_until timestamptz;
alter table notification_outbox add column if not exists claim_token uuid;
create or replace function public.claim_lodestone_notifications(batch_size integer default 10)
returns setof notification_outbox language sql set search_path = public as $$
    update notification_outbox set lease_until = now() + interval '5 minutes', claim_token = gen_random_uuid()
    where id in (select id from notification_outbox where sent_at is null
        and (lease_until is null or lease_until < now()) order by created_at
        for update skip locked limit least(greatest(batch_size,1),10))
    returning *;
$$;
revoke all on function public.claim_lodestone_notifications(integer) from public, anon, authenticated;
grant execute on function public.claim_lodestone_notifications(integer) to service_role;

create or replace function public.claim_lodestone_prices(batch_size integer default 5)
returns setof price_watches language sql set search_path = public as $$
    update price_watches set next_check_at = now() + interval '10 minutes'
    where id in (select id from price_watches where is_active and next_check_at <= now()
        order by next_check_at for update skip locked limit least(greatest(batch_size,1),5))
    returning *;
$$;
revoke all on function public.claim_lodestone_prices(integer) from public, anon, authenticated;
grant execute on function public.claim_lodestone_prices(integer) to service_role;

-- Disable the scheduled job until deployment credentials are configured explicitly.
select cron.alter_job(jobid, active := false) from cron.job where jobname = 'lodestone-price-checks-hourly';

alter table public.recovery_cases drop constraint if exists recovery_cases_case_type_check;
alter table public.recovery_cases add constraint recovery_cases_case_type_check
check (case_type in ('return','warranty','price_adjustment','rebate','card_benefit'));
