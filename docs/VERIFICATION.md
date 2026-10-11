# Verification record

Local checks completed on 2026-10-10:

- `make local-check`: Python compilation, gateway TypeScript, frontend TypeScript, and 21 Python tests passed.
- `npm --prefix frontend run build`: production browser bundle built successfully.
- FastAPI TestClient: demo seed works and is idempotent; manual purchase, rebate action, claim packet, recovered amount, export, and explicit deletion work.
- Isolation tests: another owner cannot list records, archive another owner's purchase, or download another owner's document/claim packet.
- Receipt import: original `.eml` bytes remain downloadable when extraction fails; HTML email parsing excludes script text.
- Persistence regression: production confirmed-product insert is invoked (mock Supabase client).
- Notification leases prevent a second immediate claim; incorrect acknowledgement token is rejected.
- Production authentication requires a token, demo chat is hidden, and missing credentials cannot trigger implicit memory storage.
- Local demo Uvicorn startup succeeded. Built assets are served through FastAPI.
- `git diff --check`: passed.

Not verified here:

- Visual/browser interactions: Chrome automation returned an invalid page ID and no tabs; the alternate browser tool reported no available browsers. A real desktop/mobile smoke check remains in the handoff acceptance list.
- Docker image build or SQL execution: Docker daemon is unavailable. Migrations were reviewed but not applied to a live Supabase instance.
- Real Supabase authentication/SMTP, private bucket access, pairing migration/RPCs, live Gemini extraction, Keepa prices, CPSC calls, cron, and iMessage delivery require the deployer's credentials and acceptance checks.
- No service was provisioned, no production migration was run, and no real claims/messages were sent.

Tests use memory or mocks. Passing them is not proof of a completed production deployment. See `HANDOFF.md` for the live acceptance sequence and known pilot-scale limits.
