from __future__ import annotations

import hmac
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool
import httpx

from .cpsc import search_recalls
from .extraction import extract_products
from .matcher import NOTICE, MatchResult, ProductCheck, match_product, query_matches_demo_notice
from .settings import get_settings
from .storage import (
    get_dashboard_snapshot,
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


app = FastAPI(
    title="Lodestone",
    description="A source-backed household product recall assistant. Historical demo fixture only.",
    version="0.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://127.0.0.1:5173",
        "http://localhost:5173",
        "http://127.0.0.1:8000",
        "http://localhost:8000",
    ],
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


def _authenticated_user_id(authorization: str | None) -> str:
    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise HTTPException(status_code=503, detail="Supabase Auth is not configured.")
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Sign in to view your evidence desk.")
    try:
        response = httpx.get(
            f"{settings.supabase_url.rstrip('/')}/auth/v1/user",
            headers={"apikey": settings.supabase_service_role_key, "Authorization": authorization},
            timeout=8,
        )
        response.raise_for_status()
        user_id = response.json().get("id")
    except Exception as error:
        raise HTTPException(status_code=401, detail="Your sign-in has expired. Please sign in again.") from error
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid sign-in session.")
    return str(user_id)


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
def dashboard(authorization: str | None = Header(default=None)) -> dict:
    settings = get_settings()
    if settings.demo_mode or uses_memory_store():
        snapshot = get_dashboard_snapshot()
    else:
        user_id = _authenticated_user_id(authorization)
        sender = account_owner_handle(user_id=user_id)
        if not sender:
            raise HTTPException(status_code=403, detail="Link your iMessage sender to view your records.")
        snapshot = get_dashboard_snapshot(owner_handle=sender)
    return {
        **snapshot,
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
    owners = get_settings().allowed_sender_id_set | set(list_linked_sender_handles())
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
            if watch.get("is_active") and current is not None and target is not None and float(current) <= float(target):
                enqueue_notification(
                    owner_handle=owner,
                    dedupe_key=f"price-target:{watch['id']}",
                    message=(
                        f"Lodestone price alert: {watch.get('provider_title') or 'Your Amazon watch'} is at "
                        f"${float(current):.2f}, at or below your ${float(target):.2f} target, based on the latest Keepa data. "
                        f"Open the listing: {watch['product_url']}"
                    ),
                )

    return {"notifications": list_pending_notifications()}


@app.post("/api/notifications/{notification_id}/sent")
def notification_sent(
    notification_id: str,
    x_lodestone_internal: str | None = Header(default=None),
) -> dict[str, bool]:
    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="The internal gateway token is not configured.")
    if not x_lodestone_internal or not hmac.compare_digest(x_lodestone_internal, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")
    return {"acknowledged": acknowledge_notification(notification_id=notification_id)}


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
) -> dict:
    if image.content_type not in {"image/jpeg", "image/png", "image/webp"}:
        raise HTTPException(status_code=415, detail="Upload a JPEG, PNG, or WebP image.")
    image_bytes = await image.read(10 * 1024 * 1024 + 1)
    if len(image_bytes) > 10 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="Image must be 10 MB or smaller.")
    if not image_bytes:
        raise HTTPException(status_code=400, detail="The uploaded image is empty.")
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
        return await handle_photon_message(parsed.model_dump())
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


if FRONTEND_DIST.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIST / "assets"), name="frontend-assets")

    @app.get("/")
    def frontend_index() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")

    @app.get("/desk")
    def frontend_desk() -> FileResponse:
        return FileResponse(FRONTEND_DIST / "index.html")


# Keep query helper available for other modules/tests.
__all__ = ["app", "match_product", "NOTICE", "query_matches_demo_notice"]
