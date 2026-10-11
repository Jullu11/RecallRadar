from __future__ import annotations

import base64
import hashlib
import re
import sys
from datetime import date, datetime, timedelta, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .cpsc import search_recalls
from .extraction import extract_products
from .matcher import NOTICE, ProductCheck, match_product
from .prices import amazon_asin_from_url, fetch_amazon_price
from .storage import (
    claim_photon_event,
    complete_latest_follow_up,
    create_receipt_image_link,
    create_follow_up,
    create_price_watch,
    create_recovery_case,
    delete_owner_products,
    enable_monitoring,
    get_conversation,
    list_owner_products,
    list_owner_price_watches,
    list_owner_recovery_cases,
    mark_photon_event_failed,
    mark_photon_event_processed,
    pause_price_watch,
    record_price_check,
    save_confirmed_product,
    save_receipt_image,
    save_match_evidence,
    set_conversation,
    set_recovery_case_status,
    update_product_identifiers,
    update_product_status,
)


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


IDENTIFIER_HINT = "MODEL <model on label> DATE <code on label>"

HELP_TEXT = (
    "Lodestone: Every purchase, on record. Save receipts for the moments they matter.\n"
    "I can check recalls and track return, warranty, and price-adjustment dates you provide.\n"
    "Commands:\n"
    "• Send a receipt photo to add a product\n"
    "• YES / NO — confirm extracted details\n"
    "• MONITOR [number] / NO — opt in or skip monitoring\n"
    "• LIST — show saved products\n"
    "• CHECK [number] — check the latest product or a numbered item\n"
    "• LABEL — request a label photo for identifiers\n"
    "• RETURN BY YYYY-MM-DD — track a return deadline you confirm\n"
    "• WARRANTY UNTIL YYYY-MM-DD — track a warranty end date you confirm\n"
    "• PRICE ADJUST BY YYYY-MM-DD — track a price deadline you confirm\n"
    "  Add FOR ITEM 2 to choose an item from LIST\n"
    "• ACTIONS — list return, warranty, and price-adjustment cases\n"
    "• REMINDERS — show overdue actions and dates due in the next 30 days\n"
    "• PACKET <action number> FOR <merchant> — draft a claim message to review and copy\n"
    "• WATCH PRICE <item> <Amazon URL> BELOW <$amount> — set a price target\n"
    "• PRICE WATCHES / PRICE CHECK <watch> / STOP PRICE <watch>\n"
    "• CLOSE ACTION <number> / DISMISS ACTION <number> — update a case\n"
    "• EDIT <field> <value> — correct extracted fields before confirming\n"
    f"• {IDENTIFIER_HINT} — type identifiers from the package\n"
    "• DONE — acknowledge a follow-up\n"
    "• CONSUMED / RETURNED / REMOVED — update ownership\n"
    "• DELETE — remove your saved data\n"
    "• HELP — show this message\n"
    "A 'no match' result is not a guarantee of safety."
)


def _yes(text: str) -> bool:
    return text.strip().casefold() in {"yes", "y", "yeah", "yep", "confirm", "correct", "monitor"}


def _no(text: str) -> bool:
    return text.strip().casefold() in {"no", "n", "nope", "wrong", "cancel"}


def _checked_at() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _field_line(label: str, value: Any) -> str:
    return f"{label}: {value if value else 'not visible'}"


def _looks_abbreviated(item: dict[str, Any]) -> bool:
    """Receipt SKU-style names that are not useful for CPSC product search."""
    name = str(item.get("product_name") or "").strip()
    brand = str(item.get("brand") or "").strip()
    uncertain = {str(field).casefold() for field in (item.get("uncertain_fields") or [])}
    if "product_name" in uncertain:
        return True
    if not name:
        return True
    if re.search(r"\b\d+\s*(CT|EA|PK)\b", name, re.IGNORECASE) and not brand:
        return True
    tokens = name.split()
    if not brand and len(name) <= 28 and name.upper() == name and any(character.isdigit() for character in name):
        return True
    if not brand and len(tokens) <= 5 and re.fullmatch(r"[A-Z0-9 ./\-]{3,28}", name):
        return True
    return False


def _usable_for_cpsc_search(brand: str | None, product_name: str, identifiers: dict[str, Any]) -> bool:
    if identifiers.get("model_number") or identifiers.get("upc") or identifiers.get("date_code"):
        return True
    item = {"brand": brand, "product_name": product_name, "uncertain_fields": []}
    if _looks_abbreviated(item):
        return False
    if brand and product_name and len(product_name.strip()) >= 6:
        return True
    if product_name and len(product_name.strip()) >= 12 and " " in product_name.strip():
        return True
    return False


def _rank_item_for_selection(item: dict[str, Any]) -> tuple[int, str]:
    """Prefer clearer / more CPSC-relevant looking items when listing a receipt."""
    score = 0
    if item.get("brand"):
        score += 2
    if item.get("model_number"):
        score += 3
    if not _looks_abbreviated(item):
        score += 2
    name = str(item.get("product_name") or "")
    return (-score, name.casefold())


def _selection_prompt(items: list[dict[str, Any]]) -> str:
    lines = []
    for index, item in enumerate(items, start=1):
        brand = item.get("brand") or "Unknown brand"
        name = item.get("product_name") or "Unknown product"
        lines.append(f"{index}. {brand} — {name}")
    return (
        "I found these items on the receipt:\n"
        + "\n".join(lines)
        + "\nReply ALL to review every item, or choose one / several with numbers like 1,3."
    )


def _product_confirmation(source_type: str, products: list[dict[str, Any]]) -> str:
    if not products:
        return "I couldn't identify a product in that image. Please send a clearer photo with the item name and label visible."
    item = products[0]
    fields = [
        _field_line("Brand", item.get("brand")),
        _field_line("Product", item.get("product_name")),
        _field_line("Model", item.get("model_number")),
        _field_line("Date/lot code", item.get("date_code") or item.get("lot_number")),
    ]
    if source_type == "receipt" and _looks_abbreviated(item):
        extra = (
            " This receipt name looks abbreviated. After you confirm, send a photo of the package label "
            "before I run a CPSC search."
        )
    elif item.get("uncertain_fields"):
        extra = " Some details were hard to read — please confirm carefully."
    else:
        extra = ""
    return (
        f"I read this {source_type} item:\n"
        + "\n".join(fields)
        + extra
        + "\nReply YES to save/update it, NO to try another photo, or EDIT PRODUCT <name>, "
        "EDIT MODEL <model>, EDIT DATE <code>, EDIT BRAND <brand>, EDIT LOT <code>, or EDIT UPC <code>."
    )


EDIT_FIELDS = {
    "brand": "brand",
    "product": "product_name",
    "name": "product_name",
    "model": "model_number",
    "date": "date_code",
    "date code": "date_code",
    "lot": "lot_number",
    "upc": "upc",
    "purchase date": "purchase_date",
}


def _parse_edit(text: str) -> tuple[int | None, str, str] | None:
    match = re.fullmatch(
        r"EDIT\s+(?:(\d{1,2})\s+)?(BRAND|PRODUCT|NAME|MODEL|DATE\s+CODE|DATE|LOT|UPC|PURCHASE\s+DATE)\s+(.+)",
        text.strip(),
        re.IGNORECASE,
    )
    if not match:
        return None
    field = " ".join(match.group(2).casefold().split())
    value = match.group(3).strip()
    if not value:
        return None
    return (int(match.group(1)) if match.group(1) else None, EDIT_FIELDS[field], value)


def _field_provenance(
    item: dict[str, Any], source_type: str, edited_fields: list[str] | None = None
) -> dict[str, dict[str, Any]]:
    edited = set(edited_fields or [])
    confirmed_at = datetime.now(timezone.utc).isoformat()
    provenance = {}
    fields = (
        "brand",
        "product_name",
        "size_or_capacity",
        "purchase_date",
        "upc",
        "lot_number",
        "expiration_date",
        "model_number",
        "date_code",
    )
    for field in fields:
        if not item.get(field):
            continue
        was_edited = field in edited
        provenance[field] = {
            "source": "user" if was_edited else "gemini",
            "source_type": source_type,
            "user_confirmed": True,
            "user_corrected": was_edited,
            "confirmed_at": confirmed_at,
        }
    return provenance


def _batch_confirmation(items: list[dict[str, Any]]) -> str:
    lines = []
    for index, item in enumerate(items, start=1):
        brand = item.get("brand") or "Unknown brand"
        name = item.get("product_name") or "Unknown product"
        model = item.get("model_number") or "model not visible"
        date_code = item.get("date_code") or item.get("lot_number") or "date/lot not visible"
        lines.append(f"{index}. {brand} — {name} · {model} · {date_code}")
    return (
        "I read these receipt items:\n"
        + "\n".join(lines)
        + "\nReply YES to save all, or NO to discard them. To correct a row first, send "
        "EDIT 2 PRODUCT <name>, EDIT 2 MODEL <model>, or another EDIT <number> <field> <value>."
    )


def _parse_identifiers(text: str) -> dict[str, str]:
    """Parse typed model / date-code identifiers from free text."""
    found: dict[str, str] = {}
    # Stop the model token before DATE/LOT keywords; do not allow spaces inside the model value.
    model_cmd = re.search(
        r"\bMODEL\s+([A-Z0-9][A-Z0-9\-]{0,40})(?=\s+(?:DATE|LOT)\b|\s*$|[,\n])",
        text,
        re.IGNORECASE,
    )
    date_cmd = re.search(r"\b(?:DATE(?:\s*CODE)?|LOT)\s*[:=]?\s*(\d{4})\b", text, re.IGNORECASE)
    mycharge = re.search(r"\bAO\s*10\s*FK\s*[- ]?\s*([ABC])\b", text, re.IGNORECASE)
    bare_date = re.search(r"\b(\d{4})\b", text)

    if model_cmd:
        found["model_number"] = model_cmd.group(1).upper()
    elif mycharge:
        found["model_number"] = f"AO10FK-{mycharge.group(1).upper()}"

    if date_cmd:
        found["date_code"] = date_cmd.group(1)
    elif bare_date and found.get("model_number"):
        found["date_code"] = bare_date.group(1)
    return found


def _format_list(products: list[dict[str, Any]]) -> str:
    if not products:
        return "You have no saved products yet. Send a receipt photo to add one."
    lines = []
    for index, product in enumerate(products, start=1):
        brand = product.get("brand") or "Unknown brand"
        name = product.get("product_name") or "Unknown product"
        status = product.get("status") or "owned"
        monitoring = "monitoring on" if product.get("monitoring_enabled") else "monitoring off"
        lines.append(f"{index}. {brand} — {name} ({status}, {monitoring})")
    return "Saved products:\n" + "\n".join(lines)


def _format_recovery_cases(cases: list[dict[str, Any]], products: list[dict[str, Any]]) -> str:
    if not cases:
        return (
            "No recovery actions are being tracked. Add one with `RETURN BY YYYY-MM-DD`, "
            "`WARRANTY UNTIL YYYY-MM-DD`, or `PRICE ADJUST BY YYYY-MM-DD`."
        )
    products_by_id = {product["id"]: product for product in products}
    lines = []
    for index, case in enumerate(cases, start=1):
        product = products_by_id.get(case["product_id"], {})
        name = product.get("product_name") or "Saved product"
        case_type = str(case.get("case_type", "recovery")).replace("_", " ")
        due = case.get("due_date") or "date not set"
        lines.append(f"{index}. {case_type.title()} — {name} · {due} · {case.get('status', 'open')}")
    return "Purchase recovery actions (dates you confirmed):\n" + "\n".join(lines)


def _format_reminders(cases: list[dict[str, Any]], products: list[dict[str, Any]]) -> str:
    today = datetime.now(timezone.utc).date()
    horizon = today + timedelta(days=30)
    products_by_id = {product["id"]: product for product in products}
    relevant = []
    for index, case in enumerate(cases, start=1):
        if case.get("status") != "open":
            continue
        try:
            due = date.fromisoformat(str(case.get("due_date")))
        except (TypeError, ValueError):
            continue
        if due <= horizon:
            relevant.append((index, due, case))
    if not relevant:
        return "No open purchase actions are due in the next 30 days. Text ACTIONS to see all tracked dates."
    lines = []
    for index, due, case in relevant:
        product = products_by_id.get(case.get("product_id"), {})
        name = product.get("product_name") or "Saved product"
        label = "Overdue" if due < today else "Due today" if due == today else "Due"
        lines.append(
            f"Action {index}: {label} {due.isoformat()} — "
            f"{str(case.get('case_type', 'recovery')).replace('_', ' ').title()} · {name}"
        )
    return (
        "Purchase reminders (dates you confirmed; no retailer terms were verified):\n"
        + "\n".join(lines)
        + "\nUse CLOSE ACTION <number> or DISMISS ACTION <number> when an action no longer needs tracking."
    )


def _claim_packet(case: dict[str, Any], product: dict[str, Any], merchant: str) -> str:
    case_type = case.get("case_type")
    product_name = product.get("product_name") or "[product name]"
    brand = product.get("brand")
    item_name = f"{brand} {product_name}" if brand else product_name
    identifiers = product.get("identifiers") or {}
    purchase_date = identifiers.get("purchase_date") or "[purchase date]"
    order_type = {
        "return": "return",
        "warranty": "warranty",
        "price_adjustment": "price adjustment",
    }.get(case_type, "purchase")
    deadline_label = {
        "return": "return deadline",
        "warranty": "warranty end date",
        "price_adjustment": "price-adjustment deadline",
    }.get(case_type, "action date")
    subject = {
        "return": f"Return request: {item_name}",
        "warranty": f"Warranty support request: {item_name}",
        "price_adjustment": f"Price adjustment request: {item_name}",
    }.get(case_type, f"Purchase support request: {item_name}")
    basis = {
        "return": f"I am contacting you about a return for {item_name}.",
        "warranty": f"I am contacting you about warranty support for {item_name}.",
        "price_adjustment": f"I am asking whether this purchase may qualify for a price adjustment.",
    }.get(case_type, f"I am contacting you about {item_name}.")
    identifier_lines = [
        f"- {label}: {identifiers[key]}"
        for key, label in (("model_number", "Model"), ("upc", "UPC"), ("date_code", "Date code"), ("lot_number", "Lot number"))
        if identifiers.get(key)
    ]
    identifier_text = "\n".join(identifier_lines) if identifier_lines else "- Product identifiers: [add if available]"
    return (
        f"Draft for {merchant} — {order_type} request\n"
        f"Subject: {subject}\n\n"
        f"Hello {merchant} Support,\n\n"
        f"{basis} My record shows a {deadline_label} of {case.get('due_date')} "
        "(a date I entered in Lodestone; please confirm the applicable terms).\n\n"
        "Purchase details:\n"
        f"- Item: {item_name}\n"
        f"- Purchase date: {purchase_date}\n"
        "- Order or receipt number: [add before sending]\n"
        "- Amount paid: [add if relevant]\n"
        f"{identifier_text}\n\n"
        "What happened / what I am requesting: [describe the issue and requested next step]\n\n"
        "I can provide a copy of my receipt and any other information needed. Please let me know the next steps.\n\n"
        "Thank you,\n[Your name]\n\n"
        "Review and fill in bracketed details before sending. This draft does not verify "
        f"{merchant}'s current policy or confirm eligibility."
    )


def _format_price_watches(watches: list[dict[str, Any]], products: list[dict[str, Any]]) -> str:
    if not watches:
        return "No price watches yet. Text WATCH PRICE <item> <Amazon URL> BELOW <$amount> to add one."
    products_by_id = {product["id"]: product for product in products}
    lines = []
    for index, watch in enumerate(watches, start=1):
        product = products_by_id.get(watch.get("product_id"), {})
        title = watch.get("provider_title") or product.get("product_name") or f"Amazon {watch.get('asin')}"
        target = float(watch.get("target_price") or 0)
        current_value = watch.get("current_price")
        current = float(current_value) if current_value is not None else None
        state = "paused" if not watch.get("is_active") else (
            "not checked yet" if current is None
            else "target reached" if current <= target
            else "target not reached"
        )
        checked = watch.get("last_checked_at")
        current_text = f"latest ${current:.2f}" if current is not None else "no successful check yet"
        lines.append(
            f"{index}. {title} · target ${target:.2f} · {current_text} · {state}"
            + (f" · checked {checked}" if checked else "")
        )
        if watch.get("check_error") and watch.get("is_active"):
            lines.append(f"   Latest check issue: {watch['check_error']}")
    return "Amazon price watches (provider data may lag the live listing):\n" + "\n".join(lines)


def _fixture_transparency(used_fixture: bool) -> str:
    if not used_fixture:
        return ""
    return (
        " Using a labeled historical CPSC fixture (notice dated June 20, 2024) — "
        "this is a historical notice for demonstration, not a claim of a current active recall."
    )


async def _check_saved_product(
    owner: str,
    product_id: str,
    product_name: str,
    brand: str | None,
    identifiers: dict[str, Any],
) -> str:
    if not _usable_for_cpsc_search(brand, product_name, identifiers):
        set_conversation(
            owner_handle=owner,
            state="awaiting_label",
            payload={"product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers},
        )
        return (
            "This receipt name looks abbreviated, so I'm not running a CPSC name search yet. "
            f"Send a photo of the package label, or type {IDENTIFIER_HINT}."
        )

    query = " ".join(part for part in (brand, product_name) if part)
    try:
        candidates, _, used_fixture = await search_recalls(query)
    except Exception:
        set_conversation(
            owner_handle=owner,
            state="awaiting_label",
            payload={"product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers},
        )
        return (
            "Your product is saved. I couldn't reach the CPSC recall source, so the check could not be completed. "
            f"Try again by sending `label` with a clear product-label photo, or type {IDENTIFIER_HINT}."
        )

    candidate = next((item for item in candidates if item["recall_number"] == NOTICE["recall_number"]), None)
    if candidate:
        model = identifiers.get("model_number")
        date_code = identifiers.get("date_code")
        if model and date_code:
            return _verify_mycharge(owner, product_id, str(model), str(date_code), candidate["retrieval_rank"])
        set_conversation(
            owner_handle=owner,
            state="awaiting_label",
            payload={"product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers},
        )
        return (
            f"I found a possible match in the CPSC database.{_fixture_transparency(used_fixture)} "
            "The receipt doesn't show the identifiers needed to verify it. "
            f"Send `label` with a clear photo of the product label, or type {IDENTIFIER_HINT}.\n"
            f"Official CPSC notice: {NOTICE['url']}"
        )

    if candidates:
        candidate_lines = [f"• {item['title']}\n  {item['official_url']}" for item in candidates[:3]]
        set_conversation(
            owner_handle=owner,
            state="awaiting_label",
            payload={"product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers},
        )
        return (
            "I found possible CPSC notices by product name, but their exact affected identifiers need review. "
            f"Send `label` with a clear product-label photo, or type {IDENTIFIER_HINT}.\n\n"
            + "\n".join(candidate_lines)
        )

    set_conversation(
        owner_handle=owner,
        state="idle",
        payload={"last_product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers},
    )
    return (
        f"No matching notice was found in the CPSC sources checked for this product-name search, "
        f"checked at {_checked_at()}. That result does not establish that the product is safe."
    )


def _verify_mycharge(
    owner: str,
    product_id: str,
    model: str,
    date_code: str,
    retrieval_rank: float | None = None,
) -> str:
    result = match_product(ProductCheck(model=model, date_code=date_code))
    save_match_evidence(
        product_id=product_id,
        notice=NOTICE,
        decision=result.outcome,
        evidence={
            "summary": result.summary,
            "matching_identifiers": result.matching_identifiers,
            "missing_identifiers": result.missing_identifiers,
            "model": model,
            "date_code": date_code,
            "retrieval_rank": retrieval_rank,
            "official_url": NOTICE["url"],
            "checked_at": result.checked_at.isoformat(),
            "historical_fixture": True,
        },
    )
    if result.outcome == "affected":
        create_follow_up(product_id=product_id, notice_id=None, action=NOTICE["remedy"])
        set_conversation(owner_handle=owner, state="awaiting_follow_up", payload={"product_id": product_id})
        return (
            "⚠ This product matches a labeled historical CPSC notice (June 20, 2024) — "
            "historical demonstration data, not a claim of a current active recall.\n"
            f"Evidence: model {model} and date code {date_code} meet the notice criteria.\n"
            f"The CPSC notice says: {NOTICE['remedy']}\n"
            f"{NOTICE['url']}\nReply DONE after you've handled it."
        )
    if result.outcome == "possible_match":
        set_conversation(owner_handle=owner, state="awaiting_label", payload={"product_id": product_id})
        return (
            f"This is still a possible match because an identifier needs checking. {result.summary}\n"
            f"{NOTICE['url']}"
        )
    set_conversation(owner_handle=owner, state="idle", payload={"last_product_id": product_id})
    return (
        f"{result.summary}\n"
        f"Checked against a labeled historical CPSC notice at {_checked_at()}: {NOTICE['url']}"
    )


def _product_save_data(
    item: dict[str, Any], source_type: str, edited_fields: list[str] | None = None
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    identifiers = {
        key: item.get(key)
        for key in ("size_or_capacity", "purchase_date", "upc", "lot_number", "expiration_date", "model_number", "date_code")
        if item.get(key)
    }
    return identifiers, _field_provenance(item, source_type, edited_fields)


def _save_receipt_item(
    owner: str,
    item: dict[str, Any],
    *,
    receipt_fingerprint: str | None = None,
    receipt_item_index: int = 1,
    edited_fields: list[str] | None = None,
) -> dict[str, str]:
    name = item.get("product_name")
    if not name:
        set_conversation(owner_handle=owner, state="awaiting_receipt", payload={})
        return {"reply": "I couldn't save an item because its product name was unreadable. Please send a clearer receipt photo."}
    identifiers, provenance = _product_save_data(item, "receipt", edited_fields)
    saved = save_confirmed_product(
        owner_handle=owner,
        brand=item.get("brand"),
        product_name=name,
        identifiers=identifiers,
        source_type="receipt",
        field_provenance=provenance,
        receipt_fingerprint=receipt_fingerprint,
        receipt_item_index=receipt_item_index if receipt_fingerprint else None,
    )
    if saved.get("_duplicate"):
        set_conversation(
            owner_handle=owner,
            state="idle",
            payload={"last_product_id": saved["id"]},
        )
        return {"reply": "That exact receipt item is already saved. I left the existing record unchanged."}
    product_payload = {
        "product_id": saved["id"],
        "brand": item.get("brand"),
        "product_name": name,
        "identifiers": identifiers,
    }
    if _looks_abbreviated(item) and not _usable_for_cpsc_search(item.get("brand"), name, identifiers):
        set_conversation(owner_handle=owner, state="awaiting_label", payload=product_payload)
        return {
            "reply": (
                "Saved the confirmed product details. This receipt name looks abbreviated. "
                "Send a photo of the package label before I run a CPSC search. "
                f"Or type {IDENTIFIER_HINT}."
            )
        }
    set_conversation(owner_handle=owner, state="awaiting_monitor_optin", payload=product_payload)
    return {
        "reply": (
            "Saved the confirmed product details. Should I monitor CPSC recall notices for it? "
            "Reply MONITOR to opt in or NO to skip."
        )
    }


async def handle_photon_message(event: dict[str, Any]) -> dict[str, str]:
    message_id = str(event["message_id"])
    owner = str(event["sender_id"])
    if not claim_photon_event(event_id=message_id, owner_handle=owner):
        return {"reply": ""}

    try:
        conversation = get_conversation(owner_handle=owner)
        state = conversation["state"]
        payload = conversation.get("payload") or {}

        if event.get("image"):
            image = event["image"]
            source_type = "label" if state == "awaiting_label" else "receipt"
            if state == "awaiting_product_confirmation":
                return {"reply": "Please reply YES or NO to confirm the previous photo before sending another one."}
            if state == "awaiting_batch_confirmation":
                return {"reply": "Please reply YES or NO to finish reviewing the receipt before sending another photo."}
            if state == "awaiting_product_selection":
                return {"reply": "Please reply with the item number first (for example, 1)."}
            if source_type == "label" and not payload.get("product_id"):
                return {"reply": "Please send a receipt first so I know which household product this label belongs to."}
            raw = base64.b64decode(image["base64"], validate=True)
            receipt_fingerprint = hashlib.sha256(raw).hexdigest() if source_type == "receipt" else None
            if receipt_fingerprint:
                try:
                    await run_in_threadpool(
                        save_receipt_image,
                        owner_handle=owner,
                        fingerprint=receipt_fingerprint,
                        image_bytes=raw,
                        mime_type=image["mime_type"],
                    )
                except Exception as error:
                    print(f"Receipt image storage failed: {type(error).__name__}", flush=True)
                    return {
                        "reply": (
                            "I couldn't securely save that receipt photo, so I haven't processed it. "
                            "Please try again later."
                        )
                    }
            try:
                extracted = await run_in_threadpool(extract_products, raw, image["mime_type"], source_type)
            except Exception as error:
                print(f"Gemini extraction failed: {type(error).__name__}: {error}", flush=True)
                return {
                    "reply": (
                        "I couldn't read that photo with Gemini just now. "
                        "Please resend a clear JPEG/PNG receipt or label photo, or try again in a moment."
                    )
                }
            items = [item.model_dump() for item in extracted.items]
            if source_type == "receipt" and len(items) > 1:
                ranked = sorted(items, key=_rank_item_for_selection)
                set_conversation(
                    owner_handle=owner,
                    state="awaiting_product_selection",
                    payload={"source_type": "receipt", "items": ranked, "receipt_fingerprint": receipt_fingerprint},
                )
                return {"reply": _selection_prompt(ranked)}

            set_conversation(
                owner_handle=owner,
                state="awaiting_product_confirmation",
                payload={
                    "source_type": source_type,
                    "items": items,
                    "product_id": payload.get("product_id"),
                    "brand": payload.get("brand"),
                    "product_name": payload.get("product_name"),
                    "identifiers": payload.get("identifiers") or {},
                    "receipt_fingerprint": receipt_fingerprint,
                    "receipt_item_index": 1 if source_type == "receipt" else None,
                    "edited_fields": [],
                },
            )
            return {"reply": _product_confirmation(source_type, items)}

        text = str(event.get("text") or "").strip()
        lowered = text.casefold()
        if not text:
            return {"reply": "Send a receipt photo to start, then confirm the extracted item. Text HELP for commands."}

        if lowered in {"help", "commands", "?"}:
            return {"reply": HELP_TEXT}

        if lowered in {"delete", "delete my data", "forget me", "forget"}:
            deleted = delete_owner_products(owner_handle=owner)
            set_conversation(owner_handle=owner, state="idle", payload={})
            return {
                "reply": (
                    f"Deleted {deleted} saved product record(s), linked match evidence, recovery actions, and price watches, "
                    "conversation state, and message deduplication records."
                )
            }

        if lowered == "list":
            return {"reply": _format_list(list_owner_products(owner_handle=owner))}

        if lowered in {"actions", "my actions", "cases"}:
            return {
                "reply": _format_recovery_cases(
                    list_owner_recovery_cases(owner_handle=owner),
                    list_owner_products(owner_handle=owner),
                )
            }

        if lowered in {"reminders", "upcoming", "what's due", "whats due"}:
            return {
                "reply": _format_reminders(
                    list_owner_recovery_cases(owner_handle=owner),
                    list_owner_products(owner_handle=owner),
                )
            }

        if lowered in {"price watches", "price watch", "my price watches"}:
            return {"reply": _format_price_watches(
                list_owner_price_watches(owner_handle=owner), list_owner_products(owner_handle=owner)
            )}

        watch_command = re.fullmatch(
            r"WATCH PRICE\s+(\d{1,2})\s+(https://\S+)\s+BELOW\s+\$?(\d+(?:\.\d{1,2})?)",
            text,
            re.IGNORECASE,
        )
        if watch_command:
            products = list_owner_products(owner_handle=owner)
            item_number = int(watch_command.group(1))
            if item_number < 1 or item_number > len(products):
                return {"reply": f"That item number is out of range. Text LIST to see items 1–{len(products)}."}
            try:
                asin = amazon_asin_from_url(watch_command.group(2))
            except ValueError as error:
                return {"reply": str(error)}
            target_price = float(watch_command.group(3))
            if target_price <= 0 or target_price > 100_000:
                return {"reply": "Set a target price greater than $0 and no more than $100,000."}
            product = products[item_number - 1]
            created_watch = create_price_watch(
                owner_handle=owner, product_id=product["id"], asin=asin,
                product_url=f"https://www.amazon.com/dp/{asin}", target_price=target_price,
            )
            watch_number = next(
                (index for index, watch in enumerate(list_owner_price_watches(owner_handle=owner), start=1)
                 if watch["id"] == created_watch["id"]),
                1,
            )
            return {
                "reply": (
                    f"Added an Amazon.com price watch for {product.get('product_name') or 'that product'}, "
                    f"target ${target_price:.2f}. Text PRICE CHECK {watch_number} to check it now or PRICE WATCHES to see all watches. "
                    "Checks are manual in this version; no background checks or price-drop messages are running."
                )
            }

        stop_price = re.fullmatch(r"STOP PRICE\s+(\d{1,2})", text, re.IGNORECASE)
        if stop_price:
            watches = list_owner_price_watches(owner_handle=owner)
            watch_number = int(stop_price.group(1))
            if watch_number < 1 or watch_number > len(watches):
                return {"reply": "That watch number is out of range. Text PRICE WATCHES to see your watches."}
            if not pause_price_watch(owner_handle=owner, watch_id=watches[watch_number - 1]["id"]):
                return {"reply": "I couldn't stop that price watch. Text PRICE WATCHES and try again."}
            return {"reply": f"Price watch {watch_number} is paused. Text PRICE WATCHES to review the list."}

        price_check = re.fullmatch(r"PRICE CHECK(?:\s+(\d{1,2}))?", text, re.IGNORECASE)
        if price_check:
            watches = list_owner_price_watches(owner_handle=owner)
            if not watches:
                return {"reply": "No price watches yet. Text WATCH PRICE <item> <Amazon URL> BELOW <$amount> to add one."}
            watch_number = int(price_check.group(1)) if price_check.group(1) else 1
            if watch_number < 1 or watch_number > len(watches):
                return {"reply": f"That watch number is out of range. Text PRICE WATCHES to see watches 1–{len(watches)}."}
            watch = watches[watch_number - 1]
            if not watch.get("is_active"):
                return {"reply": "That price watch is paused. Text PRICE WATCHES to select an active watch."}
            try:
                current = await run_in_threadpool(fetch_amazon_price, watch["asin"])
            except (RuntimeError, LookupError) as error:
                record_price_check(owner_handle=owner, watch_id=watch["id"], current_price=None, error=str(error))
                return {"reply": str(error)}
            except Exception as error:
                print(f"Keepa price check failed: {type(error).__name__}", flush=True)
                record_price_check(
                    owner_handle=owner, watch_id=watch["id"], current_price=None,
                    error="Price provider unavailable; try again later.",
                )
                return {"reply": "The price provider is unavailable right now. No current price was recorded; try again later."}
            updated = record_price_check(
                owner_handle=owner, watch_id=watch["id"], current_price=current["price"],
                provider_title=current.get("title"),
            )
            if not updated:
                return {"reply": "I couldn't find that saved price watch."}
            state_text = "Your target is reached." if current["price"] <= float(watch["target_price"]) else "Your target has not been reached."
            return {
                "reply": (
                    f"Keepa's latest recorded Amazon price for {current.get('title') or watch['asin']} is "
                    f"${current['price']:.2f} USD. Target: ${float(watch['target_price']):.2f}. {state_text} "
                    "Provider data can lag the listing. Text PRICE WATCHES to review saved checks."
                )
            }

        packet_command = re.fullmatch(r"PACKET\s+(\d{1,2})\s+FOR\s+(.{1,60})", text, re.IGNORECASE)
        if packet_command:
            merchant = packet_command.group(2).strip()
            if not re.fullmatch(r"[\w][\w .&'’()-]{0,59}", merchant, re.UNICODE):
                return {"reply": "Enter a merchant name using letters, numbers, spaces, or basic punctuation."}
            cases = list_owner_recovery_cases(owner_handle=owner)
            action_number = int(packet_command.group(1))
            if action_number < 1 or action_number > len(cases):
                return {"reply": "That action number is out of range. Text ACTIONS to see your tracked dates."}
            case = cases[action_number - 1]
            if case.get("status") != "open":
                return {"reply": "That action is closed. Text ACTIONS and choose an open action to draft a packet."}
            products_by_id = {product["id"]: product for product in list_owner_products(owner_handle=owner)}
            product = products_by_id.get(case.get("product_id"))
            if not product:
                return {"reply": "I couldn't find the saved product for that action. Text ACTIONS and try another one."}
            packet = _claim_packet(case, product, merchant)
            try:
                receipt_link = await run_in_threadpool(
                    create_receipt_image_link, owner_handle=owner, product_id=product["id"],
                )
            except Exception as error:
                print(f"Receipt link generation failed: {type(error).__name__}", flush=True)
                receipt_link = None
            if receipt_link:
                packet += (
                    "\n\nOptional receipt attachment (private link expires in 30 minutes): "
                    f"{receipt_link}\nOnly share this link if you want the merchant to see the receipt image."
                )
            return {"reply": packet}

        recovery_command = re.fullmatch(
            r"(RETURN BY|WARRANTY UNTIL|PRICE ADJUST BY)\s+(\d{4}-\d{2}-\d{2})(?:\s+FOR ITEM\s+(\d{1,2}))?",
            text,
            re.IGNORECASE,
        )
        if recovery_command:
            command = recovery_command.group(1).upper()
            date_text = recovery_command.group(2)
            item_number = int(recovery_command.group(3)) if recovery_command.group(3) else None
            try:
                confirmed_date = date.fromisoformat(date_text).isoformat()
            except ValueError:
                return {"reply": "Please use a valid calendar date in YYYY-MM-DD format."}

            products = list_owner_products(owner_handle=owner)
            if not products:
                return {"reply": "Save a product first, then add a recovery action for it."}
            selected_product = None
            if item_number is not None:
                if item_number < 1 or item_number > len(products):
                    return {"reply": f"That item number is out of range. Text LIST to see items 1–{len(products)}."}
                selected_product = products[item_number - 1]
            else:
                context_product_id = payload.get("product_id") or payload.get("last_product_id")
                selected_product = next(
                    (product for product in products if product["id"] == context_product_id),
                    None,
                )
                if selected_product is None and len(products) == 1:
                    selected_product = products[0]
                if selected_product is None and len(products) > 1:
                    return {
                        "reply": (
                            "Which saved product is this for? Text LIST, then use the item number, "
                            f"for example `{command} {date_text} FOR ITEM 2`."
                        )
                    }
            if selected_product is None:
                return {"reply": "Save a product first, then add a recovery action for it."}

            case_type = {
                "RETURN BY": "return",
                "WARRANTY UNTIL": "warranty",
                "PRICE ADJUST BY": "price_adjustment",
            }[command]
            create_recovery_case(
                owner_handle=owner,
                product_id=selected_product["id"],
                case_type=case_type,
                due_date=confirmed_date,
            )
            date_meaning = {
                "return": "return deadline",
                "warranty": "warranty end date",
                "price_adjustment": "price-adjustment deadline",
            }[case_type]
            return {
                "reply": (
                    f"Tracked {date_meaning} {confirmed_date} for "
                    f"{selected_product.get('product_name') or 'your saved product'}. "
                    "This date is user-confirmed, not checked against the retailer or warranty terms. "
                    "Text ACTIONS to review tracked actions."
                )
            }

        action_update = re.fullmatch(r"(CLOSE|DISMISS) ACTION\s+(\d{1,2})", text, re.IGNORECASE)
        if action_update:
            cases = list_owner_recovery_cases(owner_handle=owner)
            action_number = int(action_update.group(2))
            if action_number < 1 or action_number > len(cases):
                return {"reply": f"That action number is out of range. Text ACTIONS to see your actions."}
            selected_case = cases[action_number - 1]
            if selected_case.get("status") != "open":
                return {"reply": "That action is already closed. Text ACTIONS to review the list."}
            new_status = "completed" if action_update.group(1).upper() == "CLOSE" else "not_applicable"
            if not set_recovery_case_status(
                owner_handle=owner,
                case_id=selected_case["id"],
                status=new_status,
            ):
                return {"reply": "I couldn't update that action. Text ACTIONS and try again."}
            result_text = "marked complete" if new_status == "completed" else "marked not applicable"
            return {"reply": f"Action {action_number} {result_text}. Text ACTIONS to review the list."}

        if state == "awaiting_product_selection":
            pending_items = payload.get("items") or []
            if text.strip().casefold() == "all":
                choices = list(range(1, len(pending_items) + 1))
            elif re.fullmatch(r"\d{1,2}(?:\s*,\s*\d{1,2})*", text.strip()):
                choices = [int(value.strip()) for value in text.split(",")]
                if len(set(choices)) != len(choices):
                    return {"reply": "Please list each item number only once."}
                if any(choice < 1 or choice > len(pending_items) for choice in choices):
                    return {"reply": f"That number is out of range. Reply ALL or choose 1–{len(pending_items)}."}
            else:
                return {"reply": f"Reply ALL or choose one / several item numbers from 1 to {len(pending_items)}, like 1,3."}
            selected = [pending_items[choice - 1] for choice in choices]
            if len(selected) > 1:
                set_conversation(
                    owner_handle=owner,
                    state="awaiting_batch_confirmation",
                    payload={
                        "source_type": "receipt",
                        "items": selected,
                        "receipt_item_indices": choices,
                        "receipt_fingerprint": payload.get("receipt_fingerprint"),
                        "edited_fields_by_item": [[] for _ in selected],
                    },
                )
                return {"reply": _batch_confirmation(selected)}
            set_conversation(
                owner_handle=owner,
                state="awaiting_product_confirmation",
                payload={
                    "source_type": "receipt", "items": selected,
                    "receipt_fingerprint": payload.get("receipt_fingerprint"),
                    "receipt_item_index": choices[0], "edited_fields": [],
                },
            )
            return {"reply": _product_confirmation("receipt", selected)}

        if state == "awaiting_batch_confirmation":
            pending_items = payload.get("items") or []
            edit = _parse_edit(text)
            if edit:
                item_number, field, value = edit
                if item_number is None or item_number < 1 or item_number > len(pending_items):
                    return {"reply": f"Include a valid row number from 1 to {len(pending_items)}, e.g. EDIT 2 PRODUCT Lamp."}
                item = dict(pending_items[item_number - 1])
                item[field] = value
                item["uncertain_fields"] = [key for key in item.get("uncertain_fields", []) if key != field]
                pending_items[item_number - 1] = item
                edited = payload.get("edited_fields_by_item") or [[] for _ in pending_items]
                edited[item_number - 1] = list(dict.fromkeys([*edited[item_number - 1], field]))
                payload.update({"items": pending_items, "edited_fields_by_item": edited})
                set_conversation(owner_handle=owner, state=state, payload=payload)
                return {"reply": _batch_confirmation(pending_items)}
            if _no(text):
                set_conversation(owner_handle=owner, state="awaiting_receipt", payload={})
                return {"reply": "Okay, I discarded those receipt details. Send another receipt photo when ready."}
            if not _yes(text):
                return {"reply": "Reply YES to save all listed items, NO to discard, or EDIT <row> <field> <value> first."}
            saved_ids = []
            duplicates = 0
            unreadable = 0
            edited = payload.get("edited_fields_by_item") or [[] for _ in pending_items]
            for index, item in enumerate(pending_items):
                if not item.get("product_name"):
                    unreadable += 1
                    continue
                identifiers, provenance = _product_save_data(item, "receipt", edited[index])
                row = save_confirmed_product(
                    owner_handle=owner, brand=item.get("brand"), product_name=item["product_name"],
                    identifiers=identifiers, source_type="receipt", field_provenance=provenance,
                    receipt_fingerprint=payload.get("receipt_fingerprint"),
                    receipt_item_index=(payload.get("receipt_item_indices") or [])[index],
                )
                saved_ids.append(row["id"])
                duplicates += int(bool(row.get("_duplicate")))
            set_conversation(
                owner_handle=owner, state="idle",
                payload={"last_product_id": saved_ids[0]} if saved_ids else {},
            )
            saved_count = len(saved_ids) - duplicates
            return {"reply": (
                f"Saved {saved_count} new item(s); {duplicates} exact receipt item(s) were already saved; "
                f"{unreadable} item(s) had no readable product name and were skipped. The receipt image is retained securely. "
                "Text LIST, then CHECK 2 to check an item or MONITOR 2 to opt in to monitoring."
            )}

        if state == "awaiting_product_confirmation":
            edit = _parse_edit(text)
            if edit:
                item_number, field, value = edit
                if item_number not in (None, 1):
                    return {"reply": "This confirmation contains one item; use EDIT <field> <value>."}
                pending_items = payload.get("items") or [{}]
                item = dict(pending_items[0])
                item[field] = value
                item["uncertain_fields"] = [key for key in item.get("uncertain_fields", []) if key != field]
                payload.update({"items": [item], "edited_fields": list(dict.fromkeys([*(payload.get("edited_fields") or []), field]))})
                set_conversation(owner_handle=owner, state=state, payload=payload)
                return {"reply": _product_confirmation(payload.get("source_type", "receipt"), [item])}
            if _no(text):
                set_conversation(owner_handle=owner, state="awaiting_receipt", payload={})
                return {"reply": "Okay. Send a clearer receipt or product-label photo and I'll read it again."}
            if not _yes(text):
                return {"reply": "Reply YES if the extracted item is correct, or NO to try another photo."}
            source_type = payload.get("source_type", "receipt")
            pending_items = payload.get("items") or []
            product_id = payload.get("product_id")
            if source_type == "label" and product_id:
                item = pending_items[0] if pending_items else {}
                identifier_updates = {
                    key: item[key]
                    for key in ("model_number", "date_code", "lot_number", "upc", "expiration_date")
                    if item.get(key)
                }
                provenance = _field_provenance(item, "label", payload.get("edited_fields"))
                updated = update_product_identifiers(
                    product_id=product_id, owner_handle=owner, updates=identifier_updates,
                    provenance={key: provenance[key] for key in identifier_updates if key in provenance},
                )
                return {
                    "reply": await _check_saved_product(
                        owner,
                        product_id,
                        updated["product_name"],
                        updated.get("brand"),
                        updated.get("identifiers") or {},
                    )
                }

            item = pending_items[0] if pending_items else {}
            return _save_receipt_item(
                owner, item, receipt_fingerprint=payload.get("receipt_fingerprint"),
                receipt_item_index=payload.get("receipt_item_index") or 1,
                edited_fields=payload.get("edited_fields"),
            )

        indexed_product_command = re.fullmatch(r"(?:MONITOR|CHECK|RECHECK)\s+\d{1,2}", text, re.IGNORECASE)
        if state == "awaiting_monitor_optin" and not indexed_product_command:
            if _no(text):
                set_conversation(owner_handle=owner, state="idle", payload=payload)
                return {"reply": "Okay, I saved the product without monitoring. You can opt in any time by texting MONITOR."}
            if not _yes(text):
                return {"reply": "Reply MONITOR to opt in to CPSC recall checks, or NO to skip monitoring."}
            product_id = payload["product_id"]
            enable_monitoring(product_id=product_id, owner_handle=owner)
            set_conversation(owner_handle=owner, state="awaiting_label", payload=payload)
            return {
                "reply": "Your monitoring opt-in is saved. Checking CPSC now. "
                + await _check_saved_product(
                    owner,
                    product_id,
                    payload["product_name"],
                    payload.get("brand"),
                    payload.get("identifiers") or {},
                )
            }

        monitor_number = re.fullmatch(r"MONITOR\s+(\d{1,2})", text, re.IGNORECASE)
        if monitor_number:
            products = list_owner_products(owner_handle=owner)
            choice = int(monitor_number.group(1))
            if choice < 1 or choice > len(products):
                return {"reply": f"That item number is out of range. Text LIST to see items 1–{len(products)}."}
            product = products[choice - 1]
            enable_monitoring(product_id=product["id"], owner_handle=owner)
            product_payload = {
                "product_id": product["id"], "last_product_id": product["id"],
                "product_name": product.get("product_name") or "", "brand": product.get("brand"),
                "identifiers": product.get("identifiers") or {},
            }
            set_conversation(owner_handle=owner, state="idle", payload=product_payload)
            return {"reply": "Monitoring is enabled for that item. " + await _check_saved_product(
                owner, product["id"], product_payload["product_name"], product_payload["brand"], product_payload["identifiers"]
            )}

        check_number = re.fullmatch(r"(?:CHECK|RECHECK)\s+(\d{1,2})", text, re.IGNORECASE)
        if check_number:
            products = list_owner_products(owner_handle=owner)
            choice = int(check_number.group(1))
            if choice < 1 or choice > len(products):
                return {"reply": f"That item number is out of range. Text LIST to see items 1–{len(products)}."}
            product = products[choice - 1]
            return {"reply": await _check_saved_product(
                owner, product["id"], product.get("product_name") or "", product.get("brand"), product.get("identifiers") or {}
            )}

        if lowered in {"check", "recheck"}:
            product_id = payload.get("product_id") or payload.get("last_product_id")
            if not product_id:
                return {"reply": "No saved product to check yet. Send a receipt photo first."}
            return {
                "reply": await _check_saved_product(
                    owner,
                    product_id,
                    payload.get("product_name") or "",
                    payload.get("brand"),
                    payload.get("identifiers") or {},
                )
            }

        if state == "awaiting_label":
            if lowered == "label":
                return {
                    "reply": (
                        "Send a clear photo of the product label, especially the model and date/lot code. "
                        f"Or type {IDENTIFIER_HINT}."
                    )
                }
            parsed = _parse_identifiers(text)
            if parsed.get("model_number") and parsed.get("date_code"):
                updates = update_product_identifiers(
                    product_id=payload["product_id"],
                    owner_handle=owner,
                    updates=parsed,
                )
                return {
                    "reply": _verify_mycharge(
                        owner,
                        payload["product_id"],
                        str(updates["identifiers"]["model_number"]),
                        str(updates["identifiers"]["date_code"]),
                    )
                }
            if parsed.get("model_number") or parsed.get("date_code"):
                update_product_identifiers(product_id=payload["product_id"], owner_handle=owner, updates=parsed)
                missing = []
                if not parsed.get("model_number") and not (payload.get("identifiers") or {}).get("model_number"):
                    missing.append("MODEL <model on label>")
                if not parsed.get("date_code") and not (payload.get("identifiers") or {}).get("date_code"):
                    missing.append("DATE <code on label>")
                return {"reply": "Got it. Still need " + " and ".join(missing) + f". Example format: {IDENTIFIER_HINT}."}
            return {
                "reply": (
                    "Please send a clear product-label photo, or type identifiers from the package as "
                    f"{IDENTIFIER_HINT}."
                )
            }

        if state == "awaiting_follow_up":
            if lowered in {"done", "complete", "completed"}:
                complete_latest_follow_up(product_id=payload["product_id"])
                set_conversation(owner_handle=owner, state="idle", payload={"last_product_id": payload["product_id"]})
                return {"reply": "Thanks, I recorded that follow-up as complete."}
            return {"reply": "When you've handled the remedy, reply DONE and I'll record it."}

        status_commands = {
            "consumed": "consumed",
            "returned": "returned",
            "removed": "removed",
            "no longer own": "no_longer_owned",
            "no longer owned": "no_longer_owned",
        }
        if lowered in status_commands:
            product_id = payload.get("product_id") or payload.get("last_product_id")
            if not product_id or not update_product_status(
                product_id=product_id,
                owner_handle=owner,
                status=status_commands[lowered],
            ):
                return {"reply": "I couldn't find a saved product to update. Send a receipt photo to add one."}
            set_conversation(owner_handle=owner, state="idle", payload={**payload, "last_product_id": product_id})
            return {"reply": f"Marked the saved product as {lowered}."}

        if lowered == "label" and payload.get("last_product_id"):
            current_payload = {**payload, "product_id": payload["last_product_id"]}
            set_conversation(owner_handle=owner, state="awaiting_label", payload=current_payload)
            return {
                "reply": (
                    "Send a clear photo of the product label, especially the model and date/lot code. "
                    f"Or type {IDENTIFIER_HINT}."
                )
            }
        if lowered in {"receipt", "add product", "add"}:
            set_conversation(owner_handle=owner, state="awaiting_receipt", payload={})
            return {"reply": "Send a clear receipt photo and I'll extract the product details for you to confirm."}
        if lowered == "monitor":
            product_id = payload.get("product_id") or payload.get("last_product_id")
            if product_id:
                enable_monitoring(product_id=product_id, owner_handle=owner)
                set_conversation(owner_handle=owner, state="awaiting_label", payload={**payload, "product_id": product_id})
                return {
                    "reply": "Your monitoring opt-in is saved. Checking CPSC now. "
                    + await _check_saved_product(
                        owner,
                        product_id,
                        payload.get("product_name", ""),
                        payload.get("brand"),
                        payload.get("identifiers") or {},
                    )
                }

        # Typed identifiers while idle, if a product is pending verification.
        parsed = _parse_identifiers(text)
        product_id = payload.get("product_id") or payload.get("last_product_id")
        if product_id and parsed.get("model_number") and parsed.get("date_code"):
            updates = update_product_identifiers(product_id=product_id, owner_handle=owner, updates=parsed)
            return {
                "reply": _verify_mycharge(
                    owner,
                    product_id,
                    str(updates["identifiers"]["model_number"]),
                    str(updates["identifiers"]["date_code"]),
                )
            }

        set_conversation(owner_handle=owner, state="awaiting_receipt", payload={})
        return {
            "reply": (
                "Hi, I'm Lodestone: Every purchase, on record. Send a receipt photo to save a household product. "
                "I'll show what I read for confirmation before saving it, then ask before monitoring CPSC notices. "
                "Text HELP for commands."
            )
        }
    except Exception as error:
        mark_photon_event_failed(event_id=message_id, error=type(error).__name__)
        raise
    finally:
        if sys.exc_info()[0] is None:
            mark_photon_event_processed(event_id=message_id)
