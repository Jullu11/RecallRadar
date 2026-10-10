from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

import httpx


CPSC_RECALLS_URL = "https://www.saferproducts.gov/RestWebServices/Recall"


def _record_list(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, list):
        return [record for record in payload if isinstance(record, dict)]
    if isinstance(payload, dict):
        # The CPSC service returns an object for one record and an array for several.
        for key in ("Recalls", "Recall", "results", "data"):
            nested = payload.get(key)
            if isinstance(nested, list):
                return [record for record in nested if isinstance(record, dict)]
            if isinstance(nested, dict):
                return [nested]
        if "RecallNumber" in payload or "RecallID" in payload:
            return [payload]
    raise ValueError("CPSC returned an unrecognized response format.")


def _search_text(record: dict[str, Any]) -> str:
    pieces = [record.get("Title", ""), record.get("Description", "")]
    products = record.get("Products", [])
    if isinstance(products, dict):
        products = products.get("Product", [])
    if isinstance(products, dict):
        products = [products]
    if isinstance(products, list):
        for product in products:
            if isinstance(product, dict):
                pieces.extend([product.get("Name", ""), product.get("Model", ""), product.get("Description", "")])
    return " ".join(str(piece) for piece in pieces if piece)


def _rank(query: str, record: dict[str, Any]) -> float:
    query_normalized = " ".join(query.casefold().split())
    title = str(record.get("Title", "")).casefold()
    product_text = _search_text(record).casefold()
    exact_phrase = 1.0 if query_normalized and query_normalized in product_text else 0.0
    token_coverage = sum(token in product_text for token in query_normalized.split()) / max(len(query_normalized.split()), 1)
    return round(max(exact_phrase, token_coverage, SequenceMatcher(None, query_normalized, title).ratio()), 3)


async def search_recalls(query: str) -> tuple[list[dict[str, Any]], str]:
    async with httpx.AsyncClient(timeout=httpx.Timeout(12.0, connect=5.0)) as client:
        response = await client.get(
            CPSC_RECALLS_URL,
            params={"ProductName": query, "format": "JSON"},
            headers={"Accept": "application/json"},
        )
        response.raise_for_status()
        records = _record_list(response.json())

    candidates = []
    for record in records:
        candidates.append(
            {
                "recall_id": record.get("RecallID"),
                "recall_number": str(record.get("RecallNumber", "")),
                "recall_date": record.get("RecallDate"),
                "title": record.get("Title"),
                "description": record.get("Description"),
                "official_url": record.get("URL"),
                "consumer_contact": record.get("ConsumerContact"),
                "retrieval_rank": _rank(query, record),
                "source": "CPSC Recalls API",
            }
        )
    candidates.sort(key=lambda item: item["retrieval_rank"], reverse=True)
    return candidates[:20], response.headers.get("date", "")
