from __future__ import annotations

import base64
import re
import sys
from typing import Any

from starlette.concurrency import run_in_threadpool

from .cpsc import search_recalls
from .main import NOTICE, ProductCheck, match_product
from .storage import (
    claim_photon_event,
    complete_latest_follow_up,
    create_follow_up,
    enable_monitoring,
    get_conversation,
    mark_photon_event_failed,
    mark_photon_event_processed,
    save_confirmed_product,
    save_match_evidence,
    set_conversation,
    update_product_identifiers,
    update_product_status,
)
from .extraction import extract_products


def _yes(text: str) -> bool:
    return text.strip().casefold() in {"yes", "y", "yeah", "yep", "confirm", "correct", "monitor"}


def _no(text: str) -> bool:
    return text.strip().casefold() in {"no", "n", "nope", "wrong", "cancel"}


def _field_line(label: str, value: Any) -> str:
    return f"{label}: {value if value else 'not visible'}"


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
    uncertain = item.get("uncertain_fields") or []
    extra = f" Please check: {', '.join(uncertain)}." if uncertain else ""
    return f"I read this {source_type} item:\n" + "\n".join(fields) + extra + "\nReply YES if this is right, or NO to try again."


async def _check_saved_product(owner: str, product_id: str, product_name: str, brand: str | None, identifiers: dict[str, Any]) -> str:
    query = " ".join(part for part in (brand, product_name) if part)
    try:
        candidates, _ = await search_recalls(query)
    except Exception:
        set_conversation(owner_handle=owner, state="awaiting_label", payload={"product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers})
        return "Your product is saved. I couldn't reach the CPSC recall source, so the check could not be completed. Try again by sending `label` with a clear product-label photo."

    candidate = next((item for item in candidates if item["recall_number"] == NOTICE["recall_number"]), None)
    if candidate:
        model = identifiers.get("model_number")
        date_code = identifiers.get("date_code")
        if model and date_code:
            return _verify_mycharge(owner, product_id, str(model), str(date_code), candidate["retrieval_rank"])
        set_conversation(owner_handle=owner, state="awaiting_label", payload={"product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers})
        return (
            "I found a possible match in the CPSC database, but the receipt doesn't show the identifiers needed to verify it. "
            "Send `label` with a clear photo of the product label so I can check its model and WWYY date code.\n"
            f"Official CPSC notice: {NOTICE['url']}"
        )

    if candidates:
        candidate_lines = [f"• {item['title']}\n  {item['official_url']}" for item in candidates[:3]]
        set_conversation(owner_handle=owner, state="awaiting_label", payload={"product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers})
        return (
            "I found possible CPSC notices by product name, but their exact affected identifiers need review. "
            "Send `label` with a clear product-label photo so I can check the model and lot/date code.\n\n"
            + "\n".join(candidate_lines)
        )

    set_conversation(owner_handle=owner, state="idle", payload={"last_product_id": product_id, "brand": brand, "product_name": product_name, "identifiers": identifiers})
    return "No matching notice was returned by the CPSC API for this product-name search. That result does not establish that the product is safe."


def _verify_mycharge(owner: str, product_id: str, model: str, date_code: str, retrieval_rank: float | None = None) -> str:
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
        create_follow_up(product_id=product_id, notice_id=None, action="Contact myCharge for a free replacement")
        set_conversation(owner_handle=owner, state="awaiting_follow_up", payload={"product_id": product_id})
        return (
            "⚠ This is an affected product in the historical CPSC recall (June 20, 2024).\n"
            f"Evidence: model {model} and date code {date_code} meet the notice criteria.\n"
            "The CPSC notice says to stop using it and contact myCharge for a free replacement.\n"
            f"{NOTICE['url']}\nReply DONE after you've handled it."
        )
    if result.outcome == "possible_match":
        set_conversation(owner_handle=owner, state="awaiting_label", payload={"product_id": product_id})
        return f"This is still a possible match because an identifier needs checking. {result.summary}\n{NOTICE['url']}"
    set_conversation(owner_handle=owner, state="idle", payload={"last_product_id": product_id})
    return f"{result.summary}\nHistorical CPSC notice: {NOTICE['url']}"


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
            if source_type == "label" and not payload.get("product_id"):
                return {"reply": "Please send a receipt first so I know which household product this label belongs to."}
            raw = base64.b64decode(image["base64"], validate=True)
            extracted = await run_in_threadpool(extract_products, raw, image["mime_type"], source_type)
            set_conversation(
                owner_handle=owner,
                state="awaiting_product_confirmation",
                payload={
                    "source_type": source_type,
                    "items": [item.model_dump() for item in extracted.items],
                    "product_id": payload.get("product_id"),
                },
            )
            return {"reply": _product_confirmation(source_type, [item.model_dump() for item in extracted.items])}

        text = str(event.get("text") or "").strip()
        lowered = text.casefold()
        if not text:
            return {"reply": "Send a receipt photo to start, then confirm the extracted item."}

        if lowered in {"delete", "delete my data", "forget me", "forget"}:
            # The user handle is scoped by the Spectrum sender ID from this direct conversation.
            from .storage import delete_owner_products

            deleted = delete_owner_products(owner_handle=owner)
            set_conversation(owner_handle=owner, state="idle", payload={})
            return {"reply": f"Deleted {deleted} saved product record(s), linked match evidence, conversation state, and message deduplication records."}

        if state == "awaiting_product_confirmation":
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
                updated = update_product_identifiers(product_id=product_id, owner_handle=owner, updates=identifier_updates)
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
            name = item.get("product_name")
            if not name:
                set_conversation(owner_handle=owner, state="awaiting_receipt", payload={})
                return {"reply": "I couldn't save an item because its product name was unreadable. Please send a clearer receipt photo."}
            identifiers = {
                key: item.get(key)
                for key in ("size_or_capacity", "purchase_date", "upc", "lot_number", "expiration_date", "model_number", "date_code")
                if item.get(key)
            }
            saved = save_confirmed_product(
                owner_handle=owner,
                brand=item.get("brand"),
                product_name=name,
                identifiers=identifiers,
                source_type="receipt",
            )
            product_payload = {
                "product_id": saved["id"],
                "brand": item.get("brand"),
                "product_name": name,
                "identifiers": identifiers,
            }
            set_conversation(owner_handle=owner, state="awaiting_monitor_optin", payload=product_payload)
            suffix = " I found more than one item; send another receipt photo later to add another." if len(pending_items) > 1 else ""
            return {"reply": "Saved the confirmed product details." + suffix + " Should I monitor CPSC recall notices for it? Reply MONITOR to opt in or NO to skip."}

        if state == "awaiting_monitor_optin":
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
                + await _check_saved_product(owner, product_id, payload["product_name"], payload.get("brand"), payload.get("identifiers") or {})
            }

        if state == "awaiting_label":
            if lowered == "label":
                return {"reply": "Send a clear photo of the product label, especially the model and date/lot code."}
            model_match = re.search(r"\bAO\s*10\s*FK\s*[- ]?\s*([ABC])\b", text, re.IGNORECASE)
            date_match = re.search(r"\b(\d{4})\b", text)
            if model_match and date_match:
                model = f"AO10FK-{model_match.group(1).upper()}"
                return {"reply": _verify_mycharge(owner, payload["product_id"], model, date_match.group(1))}
            return {"reply": "Please send a clear product-label photo, or type the model number and four-digit WWYY date code."}

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
            return {"reply": "Send a clear photo of the product label, especially the model and date/lot code."}
        if lowered in {"receipt", "add product", "add"}:
            set_conversation(owner_handle=owner, state="awaiting_receipt", payload={})
            return {"reply": "Send a clear receipt photo and I'll extract the product details for you to confirm."}
        if lowered == "monitor":
            product_id = payload.get("product_id") or payload.get("last_product_id")
            if product_id:
                enable_monitoring(product_id=product_id, owner_handle=owner)
                set_conversation(owner_handle=owner, state="awaiting_label", payload={**payload, "product_id": product_id})
                return {"reply": "Your monitoring opt-in is saved. Checking CPSC now. " + await _check_saved_product(owner, product_id, payload.get("product_name", ""), payload.get("brand"), payload.get("identifiers") or {})}

        set_conversation(owner_handle=owner, state="awaiting_receipt", payload={})
        return {"reply": "Hi, I'm RecallRadar. Send a receipt photo to add a household product. I'll show what I read for confirmation before saving it, then ask before monitoring CPSC notices."}
    except Exception as error:
        mark_photon_event_failed(event_id=message_id, error=type(error).__name__)
        raise
    finally:
        if sys.exc_info()[0] is None:
            mark_photon_event_processed(event_id=message_id)
