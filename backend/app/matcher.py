from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field


DATA_PATH = Path(__file__).parent / "data" / "mycharge_power_hub.json"
NOTICE = json.loads(DATA_PATH.read_text())


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


def normalize_model(value: str | None) -> str | None:
    if not value:
        return None
    return "".join(character for character in value.upper() if character.isalnum())


def model_matches(value: str | None) -> bool:
    model = normalize_model(value)
    return model in {normalize_model(item) for item in NOTICE["affected_models"]}


def date_code_matches(value: str | None) -> bool:
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


def is_valid_date_code(value: str | None) -> bool:
    if value is None:
        return False
    code = "".join(character for character in value if character.isdigit())
    return len(code) == 4 and 1 <= int(code[:2]) <= 53


def match_product(product: ProductCheck) -> MatchResult:
    model_known = bool(product.model and product.model.strip())
    date_known = bool(product.date_code and product.date_code.strip())
    model_ok = model_matches(product.model)
    date_ok = date_code_matches(product.date_code)
    matching: list[str] = []
    missing: list[str] = []

    if model_ok:
        matching.append(f"Model {product.model.strip().upper()} is listed in CPSC recall {NOTICE['recall_number']}.")
    if model_known and not model_ok:
        outcome = "no_match"
        summary = "The model does not match this historical CPSC notice. This is not a general safety determination."
    elif not model_known or not date_known or (model_ok and not is_valid_date_code(product.date_code)):
        outcome = "possible_match"
        if not model_known:
            missing.append("model number")
        if not date_known or (model_ok and not is_valid_date_code(product.date_code)):
            missing.append("readable date code (WWYY)")
        summary = "This product may match the notice. Check the missing identifier(s) on the product label."
    elif date_ok:
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


def query_matches_demo_notice(query: str) -> bool:
    tokens = " ".join(query.casefold().split())
    needles = ("mycharge", "power hub", "ao10fk", "portable charger")
    return any(needle in tokens for needle in needles)
