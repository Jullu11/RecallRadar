# Lodestone deployment and handoff

**Every purchase, on record.** Save the original once; use it for returns, warranties, recalls, price adjustments, rebates, and card benefit requests.

## What is delivered

A React browser workspace served by a FastAPI backend. Supabase provides email authentication, Postgres records, and private original-document storage. The browser is usable independently of iMessage. The optional Node Spectrum gateway handles iMessage capture, pairing, and reminders.

Users can upload JPEG/PNG/WebP/PDF receipts or exported `.eml` emails, paste receipt text, review extracted items, enter purchases manually, retain/download originals, edit and archive purchases, check CPSC recall candidates, record deadlines, prepare claim packets, track submissions/outcomes and money recovered, save benefit terms, and monitor Amazon.com prices with Keepa. Extraction failure preserves the original and offers manual entry. Images are retained until the user explicitly deletes their purchase data.

Claim packets are downloaded for the user to submit through the merchant's own channel. Lodestone does not automatically file claims, guarantee eligibility, connect to inboxes, or infer warranty/return policies. Benefit matches compare user-entered merchant, date, and card terms; review other restrictions. Prices are USD/Amazon.com, based on the latest provider data, not guaranteed checkout prices. Recall name matches require identifier verification. Historical demo evidence is labelled.

## Quick local presentation (no services required)

Prerequisites: Python 3.12+, Node 22, npm.

```sh
make setup
make demo
```

Open http://127.0.0.1:8000/desk and click **Load sample purchase**. Demo mode uses shared process memory and resets when the process stops. Keep it on localhost; never use it for public users or real private receipts. `make demo` blanks Gemini and Keepa credentials. The sample uses a historical myCharge fixture, not a live safety determination.

Presentation: load the sample, inspect Purchases and Documents, download the original, add a return date in Actions, prepare/download a claim packet, mark a successful claim and record the amount recovered. Add a manual purchase and saved benefit terms. Price checking and extraction need their respective providers; the manual workflow does not.

For development, run `make api` and `make web` in separate terminals; Vite proxies API requests to port 8000. Production serves the built browser from FastAPI, avoiding a separate frontend hosting configuration.

## Recommended deployment: Supabase + one Docker web service

### 1. Supabase database and storage

Create a Supabase project. Save its project URL, public anon/publishable key, and backend service-role key in the host's secret manager. Never put the service-role key into browser code, a `VITE_` variable, or source control.

Apply every file under `supabase/migrations/` **in numeric order, 0001 through 0009**, once, using Supabase SQL Editor or the Supabase migration CLI. For an existing installation, apply only unapplied migrations; back up first. Migration 0006 needs Supabase Cron (`pg_cron`), `pg_net`, and Vault; enable the corresponding Supabase extensions if necessary. Migration 0009 intentionally disables the price cron until configuration is complete.

The migrations create private tables with RLS and the private `receipt-images` bucket, with a 10 MB object limit. The backend uses the service role and explicitly scopes every workspace operation by authenticated account. Do not make the bucket public or add anonymous read policies. Migration 0009 migrates paired phone records to stable account ownership and adds transactional pairing and job leases.

References: [Supabase migrations](https://supabase.com/docs/guides/local-development/database-migrations), [Cron](https://supabase.com/docs/guides/cron/quickstart).

### 2. Configure authentication

Enable Supabase email authentication and public signup as appropriate. Configure production SMTP with a verified sending domain. Set the Site URL to your final HTTPS app URL. In the **Magic Link email template**, include the six-digit `{{ .Token }}` code: the app asks users to type their email code, rather than handling a link callback. Verify code delivery to a real external address before inviting users. Configure provider rate limits and signup abuse protection for your audience.

Reference: [Supabase email OTP](https://supabase.com/docs/guides/auth/auth-email-passwordless).

### 3. Build and host

Use the root `Dockerfile` on Render or any Docker-capable HTTPS host. `render.yaml` is an optional web-service blueprint; importing it provisions services, so this handoff does not apply it for you. Use an always-on service when enabling scheduled checks. The container runs as a non-root user and serves both browser and API. Set a custom domain and HTTPS at the host.

```sh
docker build -t lodestone .
docker run --rm --env-file .env -p 8000:8000 lodestone
```

Set these runtime variables in the host, **not** in a committed file:

| Variable | Requirement |
| --- | --- |
| `DEMO_MODE=false` | Required for real accounts and persistence |
| `CPSC_FIXTURE_FALLBACK=false` | Required for production: no demo fallback |
| `SUPABASE_URL` | Project URL |
| `SUPABASE_ANON_KEY` | Public browser key, returned by `/api/config` |
| `SUPABASE_SERVICE_ROLE_KEY` | Backend-only secret |
| `BACKEND_INTERNAL_TOKEN` | Strong random shared secret for scheduled jobs/gateway |
| `GEMINI_API_KEY` | Enables image/PDF/email extraction; manual entry works without it |
| `GEMINI_MODEL` | Optional; default `gemini-2.5-flash` |
| `KEEPA_API_KEY` | Enables Amazon price checks; requires provider plan |
| `CORS_ORIGINS` | Exact browser origins, comma separated; same-origin hosting needs no extra origin |
| `IMESSAGE_NUMBER` | Optional actual service number for the browser's iMessage link |
| `PORT` | Set by hosting provider; default 8000 |

The browser obtains its public configuration at runtime; no `VITE_SUPABASE_*` variables are needed. `/health` is liveness; `/ready` checks database availability/schema. It is not a complete provider or storage health test. Apply migrations before deploying new application code. Production startup refuses missing Supabase settings instead of silently losing records in memory.

Reference: [Render Docker deployment](https://render.com/docs/deploying-an-image).

### 4. Enable automated price checks

After a successful manual price check, create **two Vault secrets** in Supabase: `lodestone_api_url` (HTTPS app origin, without trailing slash), and `lodestone_internal_token` (exact backend shared secret). Then in SQL Editor:

```sql
select cron.alter_job(jobid, active := true)
from cron.job where jobname = 'lodestone-price-checks-hourly';
```

Cron posts hourly to `/api/price-watches/run`. Each run claims at most five due watches; successful or failed checks are scheduled 24 hours later, with a short lease for interrupted work. A five-watch batch may exceed the cron HTTP response timeout under slow provider responses; check backend logs and saved timestamps before retrying. At larger scale, move this loop to a durable worker and increase scheduling frequency within provider quotas. Do not expose the shared secret in browser requests.

### 5. Optional iMessage integration

Provision Spectrum/Photon with a working iMessage identity according to the provider account instructions. The gateway uses the Spectrum message stream; no inbound public webhook is required by this code. Deploy `gateway/Dockerfile` as a separate always-on **background worker**, building from the repository root. It includes the TypeScript runtime used by `npm start`.

Set `SPECTRUM_PROJECT_ID`, `SPECTRUM_PROJECT_SECRET`, `BACKEND_URL` (public HTTPS app origin), and the same `BACKEND_INTERNAL_TOKEN`. Optional `ALLOWED_SENDER_IDS` must match on backend and gateway for trusted legacy users; otherwise users sign in, generate a pairing code in Account, and text `LINK <code>` to the service number. Codes expire after ten minutes. Browser records work before pairing.

Run one gateway initially. It polls deadline/price notifications every minute. Database leases reduce duplicate delivery across workers; a crash between sending a message and recording its acknowledgement can still produce a retry. Price alerts are deduplicated once per watch, deadline alerts once per case/date. Browser users without linked iMessage see their records and dates in-app; there is no email/push reminder delivery. There is no scheduled comprehensive recall crawler: recall checks are user initiated.

Reference: [Render background workers](https://render.com/docs/background-workers).

## Required acceptance checks before inviting users

1. `/ready` returns 200; homepage and `/desk` load over HTTPS on desktop and mobile.
2. Sign up two independent users using real email codes; sign out/in and confirm saved records persist.
3. User A uploads a real receipt, confirms its fields, reloads, and downloads byte-identical original data. Repeat with PDF and `.eml`. Confirm extraction failure still retains the original.
4. User B cannot list/download/edit User A's purchases, documents, actions, or claim packets, even with copied IDs. Unauthenticated workspace requests return 401.
5. Create a deadline and claim, download its packet, record a win, and confirm the recovered total. Export account data. Test explicit data deletion using a disposable account; auth identity and phone link are intentionally retained.
6. With Keepa configured, check a valid Amazon listing, observe its timestamp/error status, then verify one scheduled run updates a due watch.
7. If iMessage enabled, pair a disposable account, capture a receipt, confirm it appears in the browser, and verify a deadline alert and acknowledgement. Check that pairing merges existing phone records.
8. Test CPSC outage behavior with fixture fallback off; do not interpret unavailable/no candidate results as a safety guarantee.

## Operations and limitations

- Back up Postgres **and Storage objects**; database backups alone do not restore receipt bytes. Test restoration. Rotate service-role, provider, and internal secrets if exposed.
- Do not log receipt contents, OTPs, auth tokens, or provider keys. Monitor failed requests, extraction errors, queue age, cron runs, and Keepa quotas. Restrict access to logs and provider dashboards.
- Originals are retained privately; extraction sends submitted documents to Gemini when configured. Disclose this in the privacy notice before public launch. Account data deletion removes originals and purchase records; full auth-account deletion is a separate operator task.
- The current workspace is intended for a small pilot. Some list queries cap results (dashboard 100, workspace metadata 1,000). Add pagination and durable bulk exports before accounts approach these limits; current JSON export reflects the workspace and does not bundle original files.
- Deadline dates and benefit terms are user supplied. Claim submission, merchant policy ingestion, mailbox OAuth, non-Amazon price providers, scheduled broad recall monitoring, and automatic refunds are separate future integrations.
- Before a wide public launch, add operational alerting, provider-budget limits, abuse/rate controls, and review retention/privacy terms. These are operator/product launch tasks, not services this repository provisions.

## Verification and ownership

See [docs/VERIFICATION.md](docs/VERIFICATION.md) for what was actually checked. Local tests use an in-memory store or mocks; they cannot establish that your Supabase project, SQL migrations, SMTP, provider credentials, or hosted network configuration work. The deployer must complete the live acceptance checklist above. No cloud services were provisioned by this implementation.
