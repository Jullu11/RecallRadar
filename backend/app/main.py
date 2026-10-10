from __future__ import annotations

import json
import hmac
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, File, Header, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .extraction import extract_products
from .cpsc import search_recalls
from .settings import get_settings


DATA_PATH = Path(__file__).parent / "data" / "mycharge_power_hub.json"
NOTICE = json.loads(DATA_PATH.read_text())

app = FastAPI(
    title="RecallRadar",
    description="A source-backed household product recall assistant. Historical demo fixture only.",
    version="0.1.0",
)


class ProductCheck(BaseModel):
    brand: str | None = None
    product_name: str | None = None
    model: str | None = None
    date_code: str | None = None


class MatchResult(BaseModel):
    outcome: Literal["affected", "possible_match", "no_match"]
    summary: str
    notice: dict
    matching_identifiers: list[str] = Field(default_factory=list)
    missing_identifiers: list[str] = Field(default_factory=list)
    checked_at: datetime
    historical_fixture: bool = True


class PhotonImage(BaseModel):
    base64: str = Field(max_length=14_000_000)
    mime_type: Literal["image/jpeg", "image/png", "image/webp"]
    name: str | None = None


class PhotonMessage(BaseModel):
    message_id: str = Field(min_length=1, max_length=200)
    sender_id: str = Field(min_length=1, max_length=300)
    space_id: str = Field(min_length=1, max_length=300)
    content_type: Literal["text", "attachment"]
    text: str | None = Field(default=None, max_length=4000)
    image: PhotonImage | None = None


def _normalize_model(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(character for character in value.upper() if character.isalnum())


def _model_matches(value: str | None) -> bool:
    model = _normalize_model(value)
    return model in {_normalize_model(item) for item in NOTICE["affected_models"]}


def _date_code_matches(value: str | None) -> bool:
    if value is None:
        return False
    code = "".join(character for character in value if character.isdigit())
    rule = NOTICE["date_code_rule"]
    if code in rule["matching_exact_codes"]:
        return True
    if len(code) != 4 or not code.endswith(tuple(rule["matching_year_suffixes"])):
        return False
    week = int(code[:2])
    return 1 <= week <= 53


def _is_valid_date_code(value: str | None) -> bool:
    if value is None:
        return False
    code = "".join(character for character in value if character.isdigit())
    return len(code) == 4 and 1 <= int(code[:2]) <= 53


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
        candidates, source_date = await search_recalls(q)
    except Exception as error:
        raise HTTPException(
            status_code=503,
            detail="The CPSC recall check could not be completed. Try again later; no safety conclusion was made.",
        ) from error

    if not candidates:
        return {
            "outcome": "no_match",
            "summary": "No matching notice was returned from the CPSC API for this search. This does not establish that the product is safe.",
            "candidates": [],
            "checked_at": datetime.now(timezone.utc),
            "source_response_date": source_date or None,
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
            }
        return {
            "outcome": "possible_match",
            "summary": "A CPSC notice may match this product. Send or type the model number and WWYY date code from the label to verify it.",
            "notice": NOTICE,
            "missing_identifiers": ["model number", "date code (WWYY)"],
            "checked_at": datetime.now(timezone.utc),
            "source_response_date": source_date or None,
        }
    return {
        "outcome": "possible_match",
        "summary": "These CPSC notices are ranked by name similarity only. Compare the identifiers in each official notice before deciding whether a product is affected.",
        "candidates": candidates,
        "checked_at": datetime.now(timezone.utc),
        "source_response_date": source_date or None,
    }


@app.post("/api/photon/message")
async def photon_message(
    event: PhotonMessage,
    x_recall_radar_internal: str | None = Header(default=None),
) -> dict[str, str]:
    expected = get_settings().backend_internal_token
    if not expected:
        raise HTTPException(status_code=503, detail="The Spectrum gateway shared secret is not configured.")
    if not x_recall_radar_internal or not hmac.compare_digest(x_recall_radar_internal, expected):
        raise HTTPException(status_code=401, detail="Unauthorized.")
    if event.sender_id not in get_settings().allowed_sender_id_set:
        raise HTTPException(status_code=403, detail="This sender is not allowed to use RecallRadar.")
    if event.content_type == "attachment" and event.image is None:
        raise HTTPException(status_code=400, detail="Attachment data is missing.")
    if event.content_type == "text" and event.image is not None:
        raise HTTPException(status_code=400, detail="Unexpected attachment data.")

    try:
        from .conversation import handle_photon_message

        return await handle_photon_message(event.model_dump())
    except Exception as error:
        raise HTTPException(status_code=503, detail="Message processing failed. Please retry.") from error


@app.post("/api/match", response_model=MatchResult)
def match_product(product: ProductCheck) -> MatchResult:
    model_known = bool(product.model and product.model.strip())
    date_known = bool(product.date_code and product.date_code.strip())
    model_matches = _model_matches(product.model)
    date_matches = _date_code_matches(product.date_code)
    matching: list[str] = []
    missing: list[str] = []

    if model_matches:
        matching.append(f"Model {product.model.strip().upper()} is listed in CPSC recall {NOTICE['recall_number']}.")
    if model_known and not model_matches:
        outcome = "no_match"
        summary = "The model does not match this historical CPSC notice. This is not a general safety determination."
    elif not model_known or not date_known or (model_matches and not _is_valid_date_code(product.date_code)):
        outcome = "possible_match"
        if not model_known:
            missing.append("model number")
        if not date_known or (model_matches and not _is_valid_date_code(product.date_code)):
            missing.append("readable date code (WWYY)")
        summary = "This product may match the notice. Check the missing identifier(s) on the product label."
    elif date_matches:
        matching.append(f"Date code {product.date_code.strip()} is included in the affected codes.")
        outcome = "affected"
        summary = "The model and date code match the affected identifiers in this historical CPSC notice."
    else:
        matching.append(f"Model {product.model.strip().upper()} is listed in the notice.")
        summary = "The model is listed, but its date code is outside the affected codes stated in this notice."
        outcome = "no_match"

    return MatchResult(
        outcome=outcome,
        summary=summary,
        notice=NOTICE,
        matching_identifiers=matching,
        missing_identifiers=missing,
        checked_at=datetime.now(timezone.utc),
    )
