# Lodestone

**Every purchase, on record.** Save receipts and use them for returns, warranties, recalls, price adjustments, and other recovery opportunities.

## Try the app locally

Requires Python 3.12+ and Node 22.

```sh
make setup
make demo
```

Open http://127.0.0.1:8000/desk. Load the sample workspace to demonstrate purchases, original documents, deadlines, and claim tracking. Demo data is shared in memory and resets on restart; use localhost only.

For real accounts, configure Supabase and set `DEMO_MODE=false`. Gemini enables extraction, Keepa enables Amazon.com price checks, and the optional Spectrum gateway enables iMessage capture/reminders.

## Handoff

**[Complete setup, deployment, and acceptance guide](HANDOFF.md)**

[Verification results](docs/VERIFICATION.md)

```sh
make local-check
make web-build
```

The root Dockerfile serves the browser and API together. `gateway/Dockerfile` runs the optional messaging worker. No services are provisioned automatically.

Original images are retained privately. Claim packets are prepared for users to submit; Lodestone does not automatically submit claims or guarantee refunds.
