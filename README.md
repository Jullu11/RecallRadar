# Lodestone: Every purchase, on record

Lodestone helps people save purchase records once and use them later for warranty claims, recalls, returns, price refunds, and other ways to recover money. **Every purchase, on record.** The current MVP starts with household product recall checks; warranty, return, and price adjustment workflows are part of the product direction, not implemented features yet.

The first demo is scoped to household products and CPSC notices. Its deterministic historical example is the myCharge POWER HUB recall (CPSC recall 24-271). It is historical demonstration data, not a current alert feed. The notice affects models AO10FK-A, AO10FK-B, and AO10FK-C with date codes ending in 21 or 22, plus 0123, 0923, 1523, and 1723. See the [official CPSC notice](https://www.cpsc.gov/Recalls/2024/myCharge-Recalls-POWER-HUB-All-In-One-Portable-Chargers-Due-to-Fire-and-Burn-Hazards-Sold-Exclusively-at-Costco).

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

3. Apply the SQL migrations in `supabase/migrations/` to your Supabase project in numeric order using its SQL editor.

4. Start the API and gateway in separate terminals from the repository root:

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

   Then open [http://127.0.0.1:5173](http://127.0.0.1:5173). For a production build served by FastAPI on port 8000, run `make web-build` and open [http://127.0.0.1:8000](http://127.0.0.1:8000).

5. Check the API at [http://127.0.0.1:8000/health](http://127.0.0.1:8000/health), explore the OpenAPI interface at [http://127.0.0.1:8000/docs](http://127.0.0.1:8000/docs), and view the historical fixture at [http://127.0.0.1:8000/api/demo/notice](http://127.0.0.1:8000/api/demo/notice). The live inventory/evidence payload is at [http://127.0.0.1:8000/api/dashboard](http://127.0.0.1:8000/api/dashboard).

The gateway connects outbound to Spectrum Cloud and listens to its message stream. It calls the local FastAPI service at `http://127.0.0.1:8000`; this setup does not need a public webhook or HTTPS tunnel. Keep the FastAPI port private.

Run `make local-check` to syntax-check the Python modules, type-check the gateway, and run unit tests for identifier matching and conversation flows. That command does not call Gemini, live Photon, or require Supabase.

With `DEMO_MODE=true` (default in `.env.example`) or with Supabase unset, the API uses in-memory persistence so you can exercise the conversation without a database. Text-only simulation:

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
- Use `CHECK <number>` to check a saved item and `MONITOR <number>` to opt that item into ongoing checks.
- Supabase stores confirmed product details, opt-in state, match evidence, and follow-up status.
- CPSC provides recall candidates. Exact identifiers determine the result for the included historical fixture; fuzzy similarity only finds candidates.
- Users can record return, warranty, and price-adjustment dates against a saved product with `RETURN BY YYYY-MM-DD`, `WARRANTY UNTIL YYYY-MM-DD`, or `PRICE ADJUST BY YYYY-MM-DD`. These are user-provided dates; Lodestone does not verify retailer terms, submit claims, or send proactive deadline notifications yet.

Do not treat “no match found in checked sources” as proof that a product is safe. The demo will identify historical fixture data wherever it appears.

Product records are saved only after the user confirms the extracted details. Monitoring is a separate opt-in. Receipt and label image bytes are passed to Gemini for extraction and are not written to Supabase. The MVP does not yet run scheduled repeat checks; it performs an initial check and records the user's follow-up response. Text `delete` to remove saved product records, conversation state, and message deduplication records.

## Local setup troubleshooting

- If the gateway exits at startup, check `SPECTRUM_PROJECT_ID`, `SPECTRUM_PROJECT_SECRET`, and `ALLOWED_SENDER_IDS` in `.env`.
- If the API starts but image extraction fails, check `GEMINI_API_KEY`.
- If product saves fail, confirm the SQL migration was applied and the Supabase URL/service-role key are correct.
- If Photon messages do not arrive, check the gateway output, Spectrum project credentials, and the exact sender ID in `ALLOWED_SENDER_IDS`.
- The API health route does not need external credentials; Gemini, Supabase, Spectrum, and the CPSC service do.
