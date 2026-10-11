-- Supabase Cron calls the protected Lodestone API endpoint hourly. The API
-- selects watches whose next_check_at is due and schedules the next check.
create extension if not exists pg_cron;
create extension if not exists pg_net with schema extensions;

alter table public.price_watches
    add column if not exists next_check_at timestamptz not null default now();

create index if not exists price_watches_due_idx
    on public.price_watches (next_check_at)
    where is_active;

do $$
declare
    old_job bigint;
begin
    select jobid into old_job from cron.job where jobname = 'lodestone-price-checks-hourly' limit 1;
    if old_job is not null then
        perform cron.unschedule(old_job);
    end if;
end $$;

select cron.schedule(
    'lodestone-price-checks-hourly',
    '0 * * * *',
    $$
    select net.http_post(
        url := (select decrypted_secret from vault.decrypted_secrets where name = 'lodestone_api_url') || '/api/price-watches/run',
        headers := jsonb_build_object(
            'content-type', 'application/json',
            'x-lodestone-internal', (select decrypted_secret from vault.decrypted_secrets where name = 'lodestone_internal_token')
        ),
        body := '{}'::jsonb,
        timeout_milliseconds := 60000
    );
    $$
);
