from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import httpx

from .cpsc import search_recalls
from .extraction import extract_products
from .matcher import NOTICE, MatchResult, ProductCheck, match_product, query_matches_demo_notice
from .settings import get_settings
from .auth import authenticated_user_id as _authenticated_user_id, owner as current_owner
from .storage import (
    get_dashboard_snapshot,
    sender_owner,
    notification_recipient,
    list_active_price_watches,
    list_pending_notifications,
    acknowledge_notification,
    enqueue_notification,
    list_owner_products,
    list_owner_price_watches,
    list_owner_recovery_cases,
    list_linked_sender_handles,
    account_owner_handle,
    create_account_link_code,
    is_linked_sender,
    redeem_account_link_code,
    record_price_check,
    uses_memory_store,
)


@asynccontextmanager
async def lifespan(app):
    settings = get_settings()
    if not settings.demo_mode:
        missing = [name for name in ('supabase_url', 'supabase_service_role_key', 'supabase_anon_key') if not getattr(settings, name)]
        if missing:
            raise RuntimeError('Production configuration missing: ' + ', '.join(missing))
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Lodestone",
    description="A source-backed household product recall assistant. Historical demo fixture only.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[url.strip() for url in get_settings().cors_origins.split(',') if url.strip()],
    allow_methods=["*"],
    allow_headers=["*"],
)

FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"


@app.get("/health")
def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "demo_mode": settings.demo_mode or uses_memory_store(),
        "persistence": "memory" if uses_memory_store() else "supabase",
    }


@app.get("/api/demo/notice")
def demo_notice() -> dict:
    return NOTICE


@app.get("/api/account")
def account(authorization: str | None = Header(default=None)) -> dict[str, object]:
    user_id = _authenticated_user_id(authorization)
    sender = account_owner_handle(user_id=user_id)
    return {"linked": sender is not None, "sender_id": sender}


@app.post("/api/account/link-code")
def account_link_code(authorization: str | None = Header(default=None)) -> dict[str, object]:
    user_id = _authenticated_user_id(authorization)
    if account_owner_handle(user_id=user_id):
        raise HTTPException(status_code=409, detail="This account already has a linked phone.")
    return {"code": create_account_link_code(user_id=user_id), "expires_in_minutes": 10}


@app.get("/api/dashboard")
def dashboard(owner_handle: str = Depends(current_owner)) -> dict:
    return {
        **get_dashboard_snapshot(owner_handle=owner_handle),
        "demo_notice": NOTICE,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.post("/api/price-watches/run")
async def run_price_watches(x_lodestone_internal: str | None = Header(default=None)) -> dict[str, int]:
    """Internal scheduler hook; checks a bounded batch and updates dashboard status."""
    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="The internal scheduler token is not configured.")
    if not x_lodestone_internal or not hmac.compare_digest(x_lodestone_internal, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")

    from .prices import fetch_amazon_price

    watches = list_active_price_watches(limit=25)
    checked = 0
    failed = 0
    for watch in watches:
        try:
            price = await run_in_threadpool(fetch_amazon_price, watch["asin"])
            record_price_check(
                owner_handle=watch["owner_handle"],
                watch_id=watch["id"],
                current_price=price["price"],
                provider_title=price.get("title"),
            )
            checked += 1
        except (RuntimeError, LookupError) as error:
            record_price_check(
                owner_handle=watch["owner_handle"], watch_id=watch["id"],
                current_price=None, error=str(error),
            )
            failed += 1
        except Exception as error:
            print(f"Scheduled Keepa check failed: {type(error).__name__}", flush=True)
            record_price_check(
                owner_handle=watch["owner_handle"], watch_id=watch["id"],
                current_price=None, error="Price provider unavailable; try again later.",
            )
            failed += 1
    return {"checked": checked, "failed": failed}


@app.post("/api/notifications/pending")
def pending_notifications(x_lodestone_internal: str | None = Header(default=None)) -> dict[str, list[dict[str, str]]]:
    """Queue due deadline and price-target alerts, then return pending gateway deliveries."""
    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="The internal gateway token is not configured.")
    if not x_lodestone_internal or not hmac.compare_digest(x_lodestone_internal, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")

    today = datetime.now(timezone.utc).date()
    owners = {sender_owner(sender) for sender in get_settings().allowed_sender_id_set | set(list_linked_sender_handles())}
    for owner in owners:
        products = list_owner_products(owner_handle=owner)
        product_names = {row["id"]: row.get("product_name") or "your product" for row in products}
        for case in list_owner_recovery_cases(owner_handle=owner):
            if case.get("status") != "open" or not case.get("due_date"):
                continue
            try:
                days = (date.fromisoformat(str(case["due_date"])) - today).days
            except ValueError:
                continue
            if -30 <= days <= 7:
                item = product_names.get(case.get("product_id"), "your product")
                if days < 0:
                    deadline = f"was due {-days} day(s) ago"
                elif days == 0:
                    deadline = "is due today"
                elif days == 1:
                    deadline = "is due tomorrow"
                else:
                    deadline = f"is due in {days} days"
                enqueue_notification(
                    owner_handle=owner,
                    dedupe_key=f"deadline:{case['id']}:{case['due_date']}",
                    message=(
                        f"Lodestone reminder: your {str(case.get('case_type', 'purchase')).replace('_', ' ')} "
                        f"date for {item} {deadline} ({case['due_date']}). These dates are user-provided; "
                        "check the merchant's terms. Text REMINDERS to review your dates."
                    ),
                )

        for watch in list_owner_price_watches(owner_handle=owner):
            current = watch.get("current_price")
            target = watch.get("target_price")
            if not watch.get("check_error") and watch.get("last_checked_at") and watch.get("is_active") and current is not None and target is not None and float(current) <= float(target):
                enqueue_notification(
                    owner_handle=owner,
                    dedupe_key=f"price-target:{watch['id']}",
                    message=(
                        f"Lodestone price alert: {watch.get('provider_title') or 'Your Amazon watch'} is at "
                        f"${float(current):.2f}, at or below your ${float(target):.2f} target, based on the latest Keepa data. "
                        f"Open the listing: {watch['product_url']}"
                    ),
                )

    notifications = []
    for item in list_pending_notifications(limit=10):
        recipient = notification_recipient(item['owner_handle'])
        if recipient:
            notifications.append({**item, 'recipient': recipient})
    return {"notifications": notifications}


@app.post("/api/notifications/{notification_id}/sent")
def notification_sent(
    notification_id: str,
    claim_token: str,
    x_lodestone_internal: str | None = Header(default=None),
) -> dict[str, bool]:
    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="The internal gateway token is not configured.")
    if not x_lodestone_internal or not hmac.compare_digest(x_lodestone_internal, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")
    return {"acknowledged": acknowledge_notification(notification_id=notification_id, claim_token=claim_token)}


@app.post("/api/senders/linked")
def sender_is_linked(
    event: dict[str, str],
    x_lodestone_internal: str | None = Header(default=None),
) -> dict[str, bool]:
    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="The internal gateway token is not configured.")
    if not x_lodestone_internal or not hmac.compare_digest(x_lodestone_internal, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")
    sender_id = event.get("sender_id", "")
    return {"linked": bool(sender_id and is_linked_sender(owner_handle=sender_id))}


@app.post("/api/extract")
async def extract_image(
    source_type: Literal["receipt", "label"] = Query(...),
    image: UploadFile = File(...),
    owner_handle: str = Depends(current_owner),
) -> dict:
    if image.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, or WebP image.")
    image_bytes = await image.read(10 * 1024 * 1024 + 1)
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image must be 10 MB or smaller.")
    if not image_bytes:
        raise HTTPException(status_code=400, detail="The uploaded image is empty.")
    from .storage import save_receipt_image
    import hashlib
    await run_in_threadpool(save_receipt_image, owner_handle=owner_handle,
                            fingerprint=hashlib.sha256(image_bytes).hexdigest(),
                            image_bytes=image_bytes, mime_type=image.content_type)
    try:
        extracted = await run_in_threadpool(extract_products, image_bytes, image.content_type, source_type)
    except RuntimeError as error:
        raise HTTPException(status_code=503, detail=str(error)) from error
    except Exception as error:
        raise HTTPException(status_code=502, detail="Image extraction failed; please try again.") from error
    return {"source_type": source_type, **extracted.model_dump(), "requires_user_confirmation": True}


@app.get("/api/recalls/search")
async def search_cpsc_recalls(
    q: str = Query(min_length=2, max_length=100),
    model: str | None = Query(default=None, max_length=80),
    date_code: str | None = Query(default=None, max_length=40),
) -> dict:
    try:
        candidates, source_date, used_fixture = await search_recalls(q)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="The CPSC recall check could not be completed. Try again later; no safety conclusion was made.",
        ) from error

    checked_at = datetime.now(timezone.utc)
    if not candidates:
        return {
            "outcome": "no_match",
            "summary": (
                f"No matching notice was found in the CPSC sources checked for this search, "
                f"checked at {checked_at.strftime('%Y-%m-%d %H:%M UTC')}. "
                "This does not establish that the product is safe."
            ),
            "candidates": [],
            "checked_at": checked_at,
            "source_response_date": source_date or None,
            "used_historical_fixture": used_fixture,
        }
    demo_candidate = next((item for item in candidates if item["recall_number"] == NOTICE["recall_number"]), None)
    if demo_candidate:
        if model or date_code:
            exact_result = match_product(ProductCheck(model=model, date_code=date_code))
            return {
                **exact_result.model_dump(),
                "source_response_date": source_date or None,
                "retrieval_rank": demo_candidate["retrieval_rank"],
                "verification_scope": "Exact model and date-code rules from the linked official notice.",
                "used_historical_fixture": used_fixture or True,
                "fixture_note": (
                    "Labeled historical CPSC fixture (June 20, 2024); historical demonstration data, "
                    "not a claim of a current active recall."
                ),
            }
        return {
            "outcome": "possible_match",
            "summary": (
                "A CPSC notice may match this product. Send a package-label photo, or type "
                "MODEL <model on label> DATE <code on label> to verify it. "
                "If a labeled historical fixture is used, it is historical demonstration data, "
                "not a claim of a current active recall."
            ),
            "notice": NOTICE,
            "missing_identifiers": ["model number", "date code (WWYY)"],
            "checked_at": checked_at,
            "source_response_date": source_date or None,
            "used_historical_fixture": used_fixture or True,
        }
    return {
        "outcome": "possible_match",
        "summary": "These CPSC notices are ranked by name similarity only. Compare the identifiers in each official notice before deciding whether a product is affected.",
        "candidates": candidates,
        "checked_at": datetime.now(timezone.utc),
        "source_response_date": source_date or None,
        "used_historical_fixture": used_fixture,
    }


@app.post("/api/photon/message")
async def photon_message(
    event: dict,
    x_lodestone_internal: str | None = Header(default=None),
) -> dict[str, str]:
    from .conversation import PhotonMessage, handle_photon_message

    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="The Spectrum gateway shared secret is not configured.")
    if not x_lodestone_internal or not hmac.compare_digest(x_lodestone_internal, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")

    try:
        parsed = PhotonMessage.model_validate(event)
    except Exception as error:
        raise HTTPException(status_code=422, detail="Invalid Photon message payload.") from error

    link_match = re.fullmatch(r"LINK\s+(\d{10})", parsed.text or "", re.IGNORECASE)
    if link_match and not parsed.image:
        if redeem_account_link_code(owner_handle=parsed.sender_id, code=link_match.group(1)):
            return {"reply": "Your Lodestone account is linked. Your saved products and receipts will appear in your evidence desk."}
        return {"reply": "That link code is invalid or expired. Sign in to Lodestone and request a new code."}

    allowed = get_settings().allowed_sender_id_set
    if parsed.sender_id not in allowed and not is_linked_sender(owner_handle=parsed.sender_id) and not (get_settings().demo_mode or uses_memory_store()):
        raise HTTPException(status_code=403, detail="This sender is not allowed to use Lodestone.")
    if parsed.content_type == "attachment" and parsed.image is None:
        raise HTTPException(status_code=400, detail="Attachment data is missing.")
    if parsed.content_type == "text" and parsed.image is not None:
        raise HTTPException(status_code=400, detail="Unexpected attachment data.")

    try:
        payload = parsed.model_dump()
        payload['sender_id'] = sender_owner(parsed.sender_id)
        return await handle_photon_message(payload)
    except Exception as error:
        import logging

        logging.getLogger("lodestone").exception("Photon message processing failed: %s", error)
        raise HTTPException(status_code=503, detail="Message processing failed. Please retry.") from error


@app.post("/api/match", response_model=MatchResult)
def match_product_route(product: ProductCheck) -> MatchResult:
    return match_product(product)


@app.post("/api/demo/chat")
async def demo_chat(
    text: str | None = None,
    sender_id: str = "demo-user",
) -> dict[str, str]:
    """Local text-only conversation simulator (no Photon credentials required)."""
    if not get_settings().demo_mode:
        raise HTTPException(404, "Not found.")
    from .conversation import handle_photon_message

    if not text or not text.strip():
        raise HTTPException(status_code=400, detail="Provide text.")
    event = {
        "message_id": f"demo-{datetime.now(timezone.utc).timestamp()}",
        "sender_id": sender_id,
        "space_id": f"space-{sender_id}",
        "content_type": "text",
        "text": text,
        "image": None,
    }
    return await handle_photon_message(event)


from .workspace import router as workspace_router
app.include_router(workspace_router)


@app.get('/api/config')
def public_config():
    settings = get_settings()
    return {'demo': settings.demo_mode,
            'supabase_url': settings.supabase_url if not settings.demo_mode else None,
            'supabase_anon_key': settings.supabase_anon_key if not settings.demo_mode else None,
            'imessage_number': settings.imessage_number,
            'extraction_enabled': bool(settings.gemini_api_key),
            'prices_enabled': bool(settings.keepa_api_key)}


@app.get('/ready')
def ready():
    if get_settings().demo_mode:
        return {'ready': True, 'mode': 'demo'}
    from .storage import get_supabase
    try:
        get_supabase().table('workspace_records').select('id').limit(1).execute()
        get_supabase().table('receipt_images').select('filename').limit(1).execute()
    except Exception:
        raise HTTPException(503, 'Database migrations or connectivity need attention.')
    return {'ready': True, 'mode': 'production'}


@app.exception_handler(LookupError)
async def not_found(request, error):
    return JSONResponse(status_code=404, content={'detail': 'Record not found.'})


@app.exception_handler(Exception)
async def server_error(request, error):
    logging.getLogger('lodestone').error('Request failed: %s', type(error).__name__)
    return JSONResponse(status_code=500, content={'detail': 'The request could not be completed. Please retry.'})


@app.middleware('http')
async def response_headers(request, call_next):
    try:
        size = int(request.headers.get('content-length', '0'))
    except ValueError:
        return JSONResponse(status_code=400, content={'detail': 'Invalid content length.'})
    if size > 15 * 1024 * 1024:
        return JSONResponse(status_code=413, content={'detail': 'Request is too large.'})
    response = await call_next(request)
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['Referrer-Policy'] = 'strict-origin-when-cross-origin'
    response.headers['X-Frame-Options'] = 'DENY'
    if request.url.path.startswith('/api/'):
        response.headers['Cache-Control'] = 'no-store'
    return response


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")

    @app.get("/")
    def frontend_index() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")

    @app.get('/hero-charger.jpg')
    def hero_image():
        return FileResponse(FRONTEND_DIST / 'hero-charger.jpg')

    @app.get("/desk")
    def frontend_desk() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")


# Keep query helper available for other modules/tests.
__all__ = ["app", "match_product", "NOTICE", "query_matches_demo_notice"]
