# Lodestone: Every purchase, on record

Lodestone helps people save purchase records once and use them later for warranty claims, recalls, returns, price refunds, and other ways to recover money. **Every purchase, on record.** The current MVP starts with household product recall checks; warranty, return, and price adjustment workflows are part of the product direction, not implemented features yet.

The first demo is scoped to household products and CPSC notices. Its deterministic historical example is the myCharge POWER HUB recall (CPSC recall 24-271). It is historical demonstration data, not a current alert feed. The notice affects models AO10FK-A, AO10FK-B, and AO10FK-C with date codes ending in 21 or 22, plus 0123, 0923, 1523, and 1723. See the [official CPSC notice](https://www.cpsc.gov/Recalls/2024/myCharge-Recalls-POWER-HUB-All-In-One-Portable-Chargers-Due-to-Fire-and-Burn-Hazards-Sold-Exclusively-at-Costco).

**Verification scope:** exact model/date-code verification currently covers that labeled myCharge POWER HUB notice only. Other CPSC hits stay “possible, needs review,” which is intentional — the agent surfaces candidates and official URLs without claiming an affected/not-affected conclusion for notices that lack implemented identifier rules.

**Storage split:** iMessage writes go to Supabase (or in-memory demo storage). An optional Snowflake warehouse receives an async analytics copy of confirmed purchases, recall decisions, and recovery actions. The evidence desk’s **Purchase insights** panel reads `/api/insights` (Snowflake + Cortex when configured; otherwise local snapshot metrics).

## Architecture

```
iPhone ──iMessage──▶ Photon Spectrum Cloud ──app.messages stream──▶ gateway/ (TypeScript, spectrum-ts)
                                                                      │ flatten text + photo, sender allowlist
                                                                      ▼ POST /api/photon/message (internal token)
                                                               backend/ (Python, FastAPI)
                                                                      │ dedupe events, conversation state
                        ┌─────────────────────────────┬───────────────┼──────────────────┐
                        ▼                             ▼               ▼                  ▼
              Gemini 2.5 Flash            CPSC Recall REST API    Supabase / memory   Snowflake (optional,
              (receipt/label → JSON)      (candidate notices)     (system of record)  async analytics)
                                                                      │
                                                     frontend/ (React + Vite evidence desk, served at / and /desk)
```

- **AI extracts, rules decide.** Gemini only turns photos into structured fields; unreadable fields stay empty. Whether a product is affected is decided by deterministic identifier rules (`backend/app/matcher.py`), never by the model.
- **Verification rule (historical demo notice, CPSC recall 24-271):** affected when the model is `AO10FK-A`, `AO10FK-B`, or `AO10FK-C` **and** the 4-digit `WWYY` date code ends in `21` or `22`, or is one of `0123`, `0923`, `1523`, `1723`.
- **Three outcomes:** affected identifiers match · possible match (needs verification) · no match found in checked sources (never presented as "safe").

## Local development

Requirements: Python 3.11 or newer, Node.js 20 or newer, npm, and GNU Make. The Python API and Spectrum Cloud gateway run as two local processes. The API binds to `127.0.0.1:8000`; the gateway binds to port `8001`.

1. Install dependencies and create the local environment file:

   ```sh
   make setup
   ```

   This creates `.venv`, installs `requirements.txt`, installs the locked gateway dependencies with `npm ci`, and copies `.env.example` to `.env` if one does not exist.

2. Fill in `.env`:

   - `GEMINI_API_KEY`: Google AI Studio API key.
   - `SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY`: your Supabase project values. Keep the service-role key on the backend; do not put it in client code.
   - `SPECTRUM_PROJECT_ID` and `SPECTRUM_PROJECT_SECRET`: Spectrum Cloud project credentials.
   - `BACKEND_INTERNAL_TOKEN`: a private random string shared only by the local gateway and API.
   - `ALLOWED_SENDER_IDS`: your exact sender ID as Spectrum delivers it. Leave no blank allowlist in a working gateway; empty denies all senders. Separate multiple exact IDs with commas.

3. Optional: apply SQL migrations `0001`–`0003` in `supabase/migrations/` if you want Supabase persistence. With `DEMO_MODE=true` or Supabase unset, the API uses in-memory storage.

4. Optional Snowflake analytics: create a warehouse/database, run `snowflake/setup.sql`, then fill `SNOWFLAKE_*` in `.env`. Sync is asynchronous (iMessage never waits on Snowflake). `DELETE` removes the owner’s analytics rows too. Set `SNOWFLAKE_USE_CORTEX=true` to summarize metrics with Cortex when available.

5. Start the API and gateway in separate terminals from the repository root:

   ```sh
   make api
   ```

   ```sh
   make gateway
   ```

   Optional evidence-desk UI (Vite + React):

   ```sh
   make web
   ```

   Then open [http://127.0.0.1:5173](http://127.0.0.1:5173). For a production build served by FastAPI on port 8000, run `make web-build` and open [http://127.0.0.1:8000](http://127.0.0.1:8000). The landing hero image is served at `/hero-charger.jpg`.

6. Check the API at [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health), explore the OpenAPI interface at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs), and view the historical fixture at [http://127.0.0.1:8000/api/demo/notice](http://127.0.0.1:8000/api/demo/notice). The live inventory/evidence payload is at [http://127.0.0.1:8000/api/dashboard](http://127.0.0.1:8000/api/dashboard). Purchase insights are at [http://127.0.0.1:8000/api/insights](http://127.0.0.1:8000/api/insights).

The gateway connects outbound to Spectrum Cloud and listens to its message stream. It calls the local FastAPI service at `http://127.0.0.1:8000`; this setup does not need a public webhook or HTTPS tunnel. Keep the FastAPI port private.

Run `make local-check` to syntax-check the Python modules, type-check the gateway, and run unit tests for identifier matching and conversation flows. That command does not call Gemini, live Photon, or require Supabase.

With `DEMO_MODE=true` (default) or with Supabase unset, the API uses in-memory persistence so you can exercise the conversation without a database. Text-only simulation:

```sh
make api
curl -s -X POST 'http://127.0.0.1:8000/api/demo/chat?text=HELP'
```

If the live CPSC API times out, `CPSC_FIXTURE_FALLBACK=true` returns the labeled historical myCharge POWER HUB notice when the product query clearly matches that fixture.

## MVP behavior

- Spectrum Cloud receives and sends direct iMessages through the Node gateway; FastAPI owns product and recall behavior.
- Gemini extracts product identifiers from receipt and label images. Unknown fields remain unknown and require confirmation.
- Multi-item receipts can be reviewed together, selected by item number, and corrected with `EDIT <field> <value>` before saving. Re-uploading the identical image avoids duplicate rows for the same receipt item.
- The app stores per-field source/confirmation metadata and a one-way receipt-image fingerprint for deduplication. It does not retain uploaded receipt or label image bytes.
- Use `CHECK <number>` to check a saved item and `MONITOR <number>` to opt that item into ongoing checks. Text `STOP` to disable monitoring for the current/latest product.
- Supabase stores confirmed product details, opt-in state, match evidence, and follow-up status.
- CPSC provides recall candidates. Exact identifiers determine the result only for the included historical myCharge fixture; other CPSC hits remain possible matches that need manual review against the official notice.
- Users can record return, warranty, and price-adjustment dates against a saved product with `RETURN BY YYYY-MM-DD`, `WARRANTY UNTIL YYYY-MM-DD`, or `PRICE ADJUST BY YYYY-MM-DD`. These are user-provided dates; Lodestone does not verify retailer terms, submit claims, or send proactive deadline notifications yet.
- `REMINDERS` lists open actions that are overdue or due in the next 30 days. The evidence desk shows all open dates and links each reminder to its saved product. These are in-app/on-demand reminders; scheduled outbound messages are not configured.
- `PACKET <action number> FOR <merchant>` drafts a copyable support message from the saved product details and the date the user entered. Missing order, payment, and issue details remain placeholders; the packet does not assert merchant policy or eligibility and is not submitted or saved as a separate record.

Do not treat “no match found in checked sources” as proof that a product is safe. The demo will identify historical fixture data wherever it appears.

Product records are saved only after the user confirms the extracted details. Monitoring is a separate opt-in (and `STOP` opts out). Receipt and label image bytes are passed to Gemini for extraction and are not written to Supabase. The MVP does not yet run scheduled repeat checks; it performs an initial check and records the user's follow-up response. Text `delete` to remove saved product records, conversation state, and message deduplication records.

## Demo script (about 2 minutes)

Use the images in `fixtures/` (for example `demo_mycharge_receipt.png`). Send each line as its own iMessage:

1. `HELLO`: welcome message.
2. Send the receipt photo, then `YES`: Lodestone shows what it read and saves it after you confirm.
3. `MONITOR`: CPSC search finds a possible match and asks for the identifiers (a receipt can't prove a match).
4. `MODEL AO10FK-A DATE 1521`: **affected identifiers match**, with evidence, the notice's remedy, and the official CPSC link.
5. `MODEL AO10FK-Z DATE 1521`: the model does not match (shows the non-matching case).
6. `DONE`, then `RETURNED`: the follow-up and ownership state are recorded.
7. Open the evidence desk (`/desk?owner=<your sender id>`) to show the saved product, evidence, and actions.

The date code is the 4-digit week/year number printed on the charger's wall prong, not the purchase date. If someone types a calendar date, Lodestone explains this.

## Tests

```sh
make local-check   # compile Python, type-check gateway + frontend, run unit tests
make test          # unit tests only
```

22 unit tests cover identifier matching (exact code, year suffix, wrong model, wrong lot, missing identifiers) and conversation flows: confirmation copy, multi-item receipts, identifiers merged across messages, calendar-date guidance, follow-up commands after a match (`RETURNED`, `LABEL`), context kept after unrecognized text, `STOP`, and `DONE`. Tests use in-memory storage and do not call Gemini, Photon, or Supabase. These are behavior tests on labeled examples, not an accuracy evaluation; we make no accuracy claim.

## Limitations

- Exact verification covers only the labeled historical myCharge notice. Other CPSC hits stay "possible match" with the official link.
- One live source (CPSC household products). Food (FDA/USDA), medication, and vehicles are not covered.
- No scheduled re-checks or proactive alerts yet; checks run on `MONITOR` or `CHECK`.
- In-memory demo mode loses data on restart; use Supabase for persistence.
- The evidence desk link uses `PUBLIC_WEB_URL`; with the local default (`127.0.0.1`) it only opens on the machine running the API.
- The Gemini free tier is limited (about 20 image requests per day per model); text commands don't use Gemini.

## Local setup troubleshooting

- If the gateway exits at startup, check `SPECTRUM_PROJECT_ID`, `SPECTRUM_PROJECT_SECRET`, and `ALLOWED_SENDER_IDS` in `.env`.
- If the API starts but image extraction fails, check `GEMINI_API_KEY`.
- If product saves fail, confirm the SQL migration was applied and the Supabase URL/service-role key are correct.
- If Photon messages do not arrive, check the gateway output, Spectrum project credentials, and the exact sender ID in `ALLOWED_SENDER_IDS`.
- The API health route does not need external credentials; Gemini, Supabase, Spectrum, and the CPSC service do.
- If port 8000 is busy or hangs, run the API on another port with `make api API_PORT=8765` and set `BACKEND_URL=http://127.0.0.1:8765` (and `PUBLIC_WEB_URL`) in `.env`. `make web API_PORT=8765` points the dev proxy at it.
- The gateway waits at most `BACKEND_TIMEOUT_MS` (default 60000) for each backend call, then asks the user to resend, so one stuck request can't block later messages.
- If photo extraction fails with `429 RESOURCE_EXHAUSTED`, the Gemini key hit its quota; wait for the reset or use another key.

## Built with

- [Photon Spectrum](https://photon.codes) (`spectrum-ts`): iMessage send/receive
- [Google Gemini API](https://ai.google.dev) (`google-genai`, `gemini-2.5-flash`): receipt and label extraction
- [CPSC Recalls API](https://www.cpsc.gov/Recalls/CPSC-Recalls-Application-Program-Interface-API-Information): official recall data
- [Supabase](https://supabase.com) / PostgreSQL: persistence
- [Snowflake](https://www.snowflake.com) (optional): analytics copy and Purchase insights
- Python, FastAPI, Uvicorn, Pydantic, httpx
- TypeScript, Node.js, tsx, Hono
- React, Vite

## AI usage

- **At runtime:** Gemini 2.5 Flash reads receipt and label photos into a strict JSON schema. Text inside images is treated as data, not instructions. Recall decisions are made by deterministic rules, not by AI.
- **During development:** AI coding assistants, including Claude (Anthropic), were used to help write, debug, and test parts of this codebase during the hackathon.

## Team

- **TODO name**: TODO (role)
- **TODO teammate name**: TODO (role)

