from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from .settings import get_settings


KEEPA_API = "https://api.keepa.com/product"
ASIN_PATTERN = re.compile(r"^[A-Z0-9]{10}$")


def amazon_asin_from_url(url: str) -> str:
    parsed = urlparse(url.strip())
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"amazon.com", "www.amazon.com"}
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in (None, 443)
    ):
        raise ValueError("For now, price watches support Amazon.com product links only.")
    match = re.search(r"/(?:dp|gp/product)/([A-Z0-9]{10})(?:[/?]|$)", parsed.path, re.IGNORECASE)
    if not match:
        raise ValueError("Use an Amazon product URL containing its 10-character ASIN, such as amazon.com/dp/ASIN.")
    asin = match.group(1).upper()
    if not ASIN_PATTERN.fullmatch(asin):
        raise ValueError("That Amazon product identifier is not valid.")
    return asin


def fetch_amazon_price(asin: str) -> dict[str, Any]:
    """Fetch the newest recorded Amazon price (in USD) using Keepa's product API."""
    api_key = get_settings().keepa_api_key
    if not api_key:
        raise RuntimeError("Price checks need a Keepa API key. Add KEEPA_API_KEY to the backend environment.")
    response = httpx.get(
        KEEPA_API,
        params={"key": api_key, "domain": 1, "asin": asin},
        timeout=20,
    )
    response.raise_for_status()
    data = response.json()
    products = data.get("products") or []
    if not products:
        raise LookupError("Keepa did not return a product for that Amazon link.")
    product = products[0]
    if product.get("productType") not in (None, 0):
        raise LookupError("Keepa has no standard product price data for that listing.")
    history = (product.get("csv") or [None])[0]
    if not isinstance(history, list) or len(history) < 2:
        raise LookupError("No Amazon price history is available for that listing yet.")
    value_cents = history[-1]
    if not isinstance(value_cents, int) or value_cents < 0:
        raise LookupError("The Amazon listing has no current price in the latest provider data.")
    keepa_minutes = history[-2] if len(history) >= 2 else None
    return {
        "asin": asin,
        "title": product.get("title"),
        "price": value_cents / 100,
        "currency": "USD",
        "provider": "Keepa",
        "provider_checked_at": keepa_minutes,
    }
