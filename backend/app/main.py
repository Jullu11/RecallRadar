from __future__ import annotations

import hmac
from datetime import datetime, timezone
from typing import Literal

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from starlette.concurrency import run_in_threadpool

from .cpsc import search_recalls
from .extraction import extract_products
from .matcher import NOTICE, MatchResult, ProductCheck, match_product, query_matches_demo_notice
from .settings import get_settings
from .storage import uses_memory_store


app = FastAPI(
    title="RecallRadar",
    description="A source-backed household product recall assistant. Historical demo fixture only.",
    version="0.1.0",
)


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
    x_recall_radar_internal: str | None = Header(default=None),
) -> dict[str, str]:
    from .conversation import PhotonMessage, handle_photon_message

    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="The Spectrum gateway shared secret is not configured.")
    if not x_recall_radar_internal or not hmac.compare_digest(x_recall_radar_internal, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")

    try:
        parsed = PhotonMessage.model_validate(event)
    except Exception as error:
        raise HTTPException(status_code=422, detail="Invalid Photon message payload.") from error

    allowed = get_settings().allowed_sender_id_set
    if parsed.sender_id not in allowed and not (get_settings().demo_mode or uses_memory_store()):
        raise HTTPException(status_code=403, detail="This sender is not allowed to use RecallRadar.")
    if parsed.content_type == "attachment" and parsed.image is None:
        raise HTTPException(status_code=400, detail="Attachment data is missing.")
    if parsed.content_type == "text" and parsed.image is not None:
        raise HTTPException(status_code=400, detail="Unexpected attachment data.")

    try:
        return await handle_photon_message(parsed.model_dump())
    except Exception as error:
        import logging

        logging.getLogger("recallradar").exception("Photon message processing failed: %s", error)
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


# Keep query helper available for other modules/tests.
__all__ = ["app", "match_product", "NOTICE", "query_matches_demo_notice"]
