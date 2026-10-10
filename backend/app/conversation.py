from __future__ import annotations

import base64
import re
import sys
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .cpsc import search_recalls
from .extraction import extract_products
from .matcher import NOTICE, ProductCheck, match_product
from .storage import (
    claim_photon_event,
    complete_latest_follow_up,
    create_follow_up,
    delete_owner_products,
    enable_monitoring,
    get_conversation,
    list_owner_products,
    mark_photon_event_failed,
    mark_photon_event_processed,
    save_confirmed_product,
    save_match_evidence,
    set_conversation,
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
    "RecallRadar checks household products against CPSC recall notices.\n"
    "Commands:\n"
    "• Send a receipt photo to add a product\n"
    "• YES / NO — confirm extracted details\n"
    "• MONITOR / NO — opt in or skip monitoring\n"
    "• LIST — show saved products\n"
    "• CHECK — recheck the latest product\n"
    "• LABEL — request a label photo for identifiers\n"
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
        + f"\nWhich should I check? Reply 1–{len(items)}."
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
    return f"I read this {source_type} item:\n" + "\n".join(fields) + extra + "\nReply YES if this is right, or NO to try again."


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


def _save_receipt_item(owner: str, item: dict[str, Any]) -> dict[str, str]:
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
            if state == "awaiting_product_selection":
                return {"reply": "Please reply with the item number first (for example, 1)."}
            if source_type == "label" and not payload.get("product_id"):
                return {"reply": "Please send a receipt first so I know which household product this label belongs to."}
            raw = base64.b64decode(image["base64"], validate=True)
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
                    payload={"source_type": "receipt", "items": ranked},
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
                    f"Deleted {deleted} saved product record(s), linked match evidence, "
                    "conversation state, and message deduplication records."
                )
            }

        if lowered == "list":
            return {"reply": _format_list(list_owner_products(owner_handle=owner))}

        if state == "awaiting_product_selection":
            pending_items = payload.get("items") or []
            if not re.fullmatch(r"\d{1,2}", text.strip()):
                return {"reply": f"Reply with a number from 1 to {len(pending_items)} to choose which item to check."}
            choice = int(text.strip())
            if choice < 1 or choice > len(pending_items):
                return {"reply": f"That number is out of range. Reply 1–{len(pending_items)}."}
            selected = pending_items[choice - 1]
            set_conversation(
                owner_handle=owner,
                state="awaiting_product_confirmation",
                payload={"source_type": "receipt", "items": [selected]},
            )
            return {"reply": _product_confirmation("receipt", [selected])}

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
            return _save_receipt_item(owner, item)

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
                + await _check_saved_product(
                    owner,
                    product_id,
                    payload["product_name"],
                    payload.get("brand"),
                    payload.get("identifiers") or {},
                )
            }

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
                "Hi, I'm RecallRadar. Send a receipt photo to add a household product. "
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
