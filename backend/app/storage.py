from __future__ import annotations

import copy
import hashlib
import secrets
import threading
import uuid
from datetime import datetime, timedelta, timezone
from functools import lru_cache
from typing import Any

from .settings import get_settings


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def uses_memory_store() -> bool:
    settings = get_settings()
    if settings.demo_mode:
        return True
    return not (settings.supabase_url and settings.supabase_service_role_key)


# Backward-compatible alias used by older call sites.
_use_memory = uses_memory_store


class _MemoryStore:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.products: dict[str, dict[str, Any]] = {}
        self.notices: dict[tuple[str, str], dict[str, Any]] = {}
        self.matches: dict[tuple[str, str], dict[str, Any]] = {}
        self.follow_ups: list[dict[str, Any]] = []
        self.recovery_cases: dict[str, dict[str, Any]] = {}
        self.price_watches: dict[str, dict[str, Any]] = {}
        self.receipt_images: dict[tuple[str, str], dict[str, Any]] = {}
        self.notification_outbox: dict[str, dict[str, Any]] = {}
        self.account_links: dict[str, str] = {}
        self.account_link_codes: dict[str, dict[str, Any]] = {}
        self.photon_events: dict[str, dict[str, Any]] = {}
        self.conversations: dict[str, dict[str, Any]] = {}


_MEMORY = _MemoryStore()


@lru_cache
def get_supabase():
    from supabase import create_client

    settings = get_settings()
    if not settings.supabase_url or not settings.supabase_service_role_key:
        raise RuntimeError("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY are required for persistence.")
    return create_client(settings.supabase_url, settings.supabase_service_role_key)


def save_confirmed_product(
    *,
    owner_handle: str,
    brand: str | None,
    product_name: str,
    identifiers: dict[str, Any],
    source_type: str,
    field_provenance: dict[str, Any] | None = None,
    receipt_fingerprint: str | None = None,
    receipt_item_index: int | None = None,
) -> dict[str, Any]:
    """Store only user-confirmed structured product fields."""
    if uses_memory_store():
        row = {
            "id": str(uuid.uuid4()),
            "owner_handle": owner_handle,
            "brand": brand,
            "product_name": product_name,
            "identifiers": copy.deepcopy(identifiers),
            "field_provenance": copy.deepcopy(field_provenance or {}),
            "source_type": source_type,
            "receipt_fingerprint": receipt_fingerprint,
            "receipt_item_index": receipt_item_index,
            "monitoring_enabled": False,
            "monitoring_consented_at": None,
            "status": "owned",
            "created_at": _now(),
            "updated_at": _now(),
        }
        with _MEMORY.lock:
            if receipt_fingerprint is not None and receipt_item_index is not None:
                duplicate = next(
                    (
                        existing
                        for existing in _MEMORY.products.values()
                        if existing["owner_handle"] == owner_handle
                        and existing.get("receipt_fingerprint") == receipt_fingerprint
                        and existing.get("receipt_item_index") == receipt_item_index
                    ),
                    None,
                )
                if duplicate:
                    return {**copy.deepcopy(duplicate), "_duplicate": True}
            _MEMORY.products[row["id"]] = row
            return {**copy.deepcopy(row), "_duplicate": False}

    client = get_supabase()
    if receipt_fingerprint is not None and receipt_item_index is not None:
        existing = (
            client.table("products")
            .select("id,owner_handle,brand,product_name,identifiers,field_provenance,source_type,monitoring_enabled,status,created_at")
            .eq("owner_handle", owner_handle)
            .eq("receipt_fingerprint", receipt_fingerprint)
            .eq("receipt_item_index", receipt_item_index)
            .limit(1)
            .execute()
        )
        if existing.data:
            return {**existing.data[0], "_duplicate": True}


def save_receipt_image(*, owner_handle: str, fingerprint: str, image_bytes: bytes, mime_type: str) -> None:
    """Retain a receipt image in private storage, deduplicated per sender and image."""
    suffix = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}.get(mime_type)
    if not suffix:
        raise ValueError("Unsupported receipt image type.")
    owner_key = hashlib.sha256(owner_handle.encode("utf-8")).hexdigest()
    path = f"{owner_key}/{fingerprint}.{suffix}"
    if uses_memory_store():
        with _MEMORY.lock:
            _MEMORY.receipt_images[(owner_handle, fingerprint)] = {
                "bytes": bytes(image_bytes), "mime_type": mime_type, "path": path,
            }
        return

    client = get_supabase()
    try:
        client.storage.from_("receipt-images").upload(
            path, image_bytes,
            file_options={"content-type": mime_type, "upsert": "true"},
        )
    except Exception as error:
        if "already exists" not in str(error).casefold() and "duplicate" not in str(error).casefold():
            raise
    client.table("receipt_images").upsert(
        {"owner_handle": owner_handle, "fingerprint": fingerprint, "storage_path": path, "mime_type": mime_type},
        on_conflict="owner_handle,fingerprint",
    ).execute()


def create_receipt_image_link(*, owner_handle: str, product_id: str, expires_in: int = 1800) -> str | None:
    """Create a short-lived signed URL for a receipt attached to an owned product."""
    if uses_memory_store():
        return None
    client = get_supabase()
    product = (
        client.table("products").select("receipt_fingerprint")
        .eq("id", product_id).eq("owner_handle", owner_handle).limit(1).execute()
    )
    if not product.data or not product.data[0].get("receipt_fingerprint"):
        return None
    asset = (
        client.table("receipt_images").select("storage_path")
        .eq("owner_handle", owner_handle)
        .eq("fingerprint", product.data[0]["receipt_fingerprint"]).limit(1).execute()
    )
    if not asset.data:
        return None
    result = client.storage.from_("receipt-images").create_signed_url(asset.data[0]["storage_path"], expires_in)
    signed_url = result.get("signedURL") or result.get("signedUrl")
    if not signed_url:
        return None
    base_url = (get_settings().supabase_url or "").rstrip("/")
    if str(signed_url).startswith("http"):
        return str(signed_url)
    if str(signed_url).startswith("/storage/"):
        return f"{base_url}{signed_url}"
    return f"{base_url}/storage/v1{signed_url}"


def create_account_link_code(*, user_id: str, ttl_minutes: int = 10) -> str:
    code = f"{secrets.randbelow(10_000_000_000):010d}"
    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
    expires = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat()
    if uses_memory_store():
        with _MEMORY.lock:
            for key, row in list(_MEMORY.account_link_codes.items()):
                if row["user_id"] == user_id:
                    del _MEMORY.account_link_codes[key]
            _MEMORY.account_link_codes[code_hash] = {"user_id": user_id, "expires_at": expires}
        return code
    client = get_supabase()
    client.table("account_link_codes").delete().eq("user_id", user_id).execute()
    client.table("account_link_codes").insert({"code_hash": code_hash, "user_id": user_id, "expires_at": expires}).execute()
    return code


def redeem_account_link_code(*, owner_handle: str, code: str) -> bool:
    code_hash = hashlib.sha256(code.encode("ascii")).hexdigest()
    now = _now()
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.account_link_codes.get(code_hash)
            if not row or row["expires_at"] <= now:
                return False
            _MEMORY.account_links[row["user_id"]] = owner_handle
            del _MEMORY.account_link_codes[code_hash]
            return True
    client = get_supabase()
    rows = (
        client.table("account_link_codes").update({"consumed_at": now})
        .eq("code_hash", code_hash).is_("consumed_at", "null").gt("expires_at", now)
        .select("user_id").execute().data
    )
    if not rows:
        return False
    try:
        client.table("account_links").upsert(
            {"user_id": rows[0]["user_id"], "owner_handle": owner_handle}, on_conflict="user_id",
        ).execute()
    except Exception:
        return False
    return True


def account_owner_handle(*, user_id: str) -> str | None:
    if uses_memory_store():
        with _MEMORY.lock:
            return _MEMORY.account_links.get(user_id)
    result = get_supabase().table("account_links").select("owner_handle").eq("user_id", user_id).limit(1).execute()
    return result.data[0]["owner_handle"] if result.data else None


def is_linked_sender(*, owner_handle: str) -> bool:
    if uses_memory_store():
        with _MEMORY.lock:
            return owner_handle in _MEMORY.account_links.values()
    result = get_supabase().table("account_links").select("user_id").eq("owner_handle", owner_handle).limit(1).execute()
    return bool(result.data)


def list_linked_sender_handles() -> list[str]:
    if uses_memory_store():
        with _MEMORY.lock:
            return list(set(_MEMORY.account_links.values()))
    result = get_supabase().table("account_links").select("owner_handle").limit(1000).execute()
    return [row["owner_handle"] for row in result.data or []]

    try:
        result = client.table("products").insert(
            {
                "owner_handle": owner_handle,
                "brand": brand,
                "product_name": product_name,
                "identifiers": identifiers,
                "field_provenance": field_provenance or {},
                "source_type": source_type,
                "receipt_fingerprint": receipt_fingerprint,
                "receipt_item_index": receipt_item_index,
                "monitoring_enabled": False,
            }
        ).execute()
        return {**result.data[0], "_duplicate": False}
    except Exception as error:
        # The unique receipt-item index closes the race between a duplicate lookup and insert.
        if receipt_fingerprint is None or receipt_item_index is None or not (
            "23505" in str(error) or "duplicate key" in str(error).casefold()
        ):
            raise
        existing = (
            client.table("products")
            .select("id,owner_handle,brand,product_name,identifiers,field_provenance,source_type,monitoring_enabled,status,created_at")
            .eq("owner_handle", owner_handle)
            .eq("receipt_fingerprint", receipt_fingerprint)
            .eq("receipt_item_index", receipt_item_index)
            .limit(1)
            .execute()
        )
        if not existing.data:
            raise
        return {**existing.data[0], "_duplicate": True}


def enable_monitoring(*, product_id: str, owner_handle: str) -> dict[str, Any]:
    """Monitoring is opt-in and can only be enabled by an explicit user action."""
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.products.get(product_id)
            if not row or row["owner_handle"] != owner_handle:
                raise LookupError("Product was not found for this message sender.")
            row["monitoring_enabled"] = True
            row["monitoring_consented_at"] = _now()
            row["updated_at"] = _now()
            return copy.deepcopy(row)

    result = (
        get_supabase()
        .table("products")
        .update(
            {
                "monitoring_enabled": True,
                "monitoring_consented_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", product_id)
        .eq("owner_handle", owner_handle)
        .execute()
    )
    if not result.data:
        raise LookupError("Product was not found for this message sender.")
    return result.data[0]


def list_owner_products(*, owner_handle: str) -> list[dict[str, Any]]:
    if uses_memory_store():
        with _MEMORY.lock:
            rows = [copy.deepcopy(row) for row in _MEMORY.products.values() if row["owner_handle"] == owner_handle]
        rows.sort(key=lambda item: item["created_at"])
        return rows

    result = (
        get_supabase()
        .table("products")
        .select("id,brand,product_name,identifiers,status,monitoring_enabled,created_at")
        .eq("owner_handle", owner_handle)
        .order("created_at")
        .execute()
    )
    return result.data or []


def create_recovery_case(
    *,
    owner_handle: str,
    product_id: str,
    case_type: str,
    due_date: str,
) -> dict[str, Any]:
    """Create a user-sourced recovery opportunity for one of the owner's products."""
    if uses_memory_store():
        with _MEMORY.lock:
            product = _MEMORY.products.get(product_id)
            if not product or product["owner_handle"] != owner_handle:
                raise LookupError("Product was not found for this message sender.")
            row = {
                "id": str(uuid.uuid4()),
                "owner_handle": owner_handle,
                "product_id": product_id,
                "case_type": case_type,
                "due_date": due_date,
                "status": "open",
                "date_source": "user_confirmed",
                "created_at": _now(),
                "completed_at": None,
            }
            _MEMORY.recovery_cases[row["id"]] = row
            return copy.deepcopy(row)

    client = get_supabase()
    product = (
        client.table("products")
        .select("id")
        .eq("id", product_id)
        .eq("owner_handle", owner_handle)
        .limit(1)
        .execute()
    )
    if not product.data:
        raise LookupError("Product was not found for this message sender.")
    result = client.table("recovery_cases").insert(
        {
            "product_id": product_id,
            "case_type": case_type,
            "due_date": due_date,
            "date_source": "user_confirmed",
        }
    ).execute()
    return result.data[0]


def list_owner_recovery_cases(*, owner_handle: str) -> list[dict[str, Any]]:
    if uses_memory_store():
        with _MEMORY.lock:
            rows = [
                copy.deepcopy(row)
                for row in _MEMORY.recovery_cases.values()
                if row["owner_handle"] == owner_handle
            ]
        rows.sort(key=lambda item: (item.get("due_date") or "9999-12-31", item["created_at"]))
        return rows

    products = list_owner_products(owner_handle=owner_handle)
    product_ids = [product["id"] for product in products]
    if not product_ids:
        return []
    result = (
        get_supabase()
        .table("recovery_cases")
        .select("id,product_id,case_type,due_date,status,date_source,created_at,completed_at")
        .in_("product_id", product_ids)
        .order("due_date")
        .order("created_at")
        .execute()
    )
    return result.data or []


def set_recovery_case_status(
    *, owner_handle: str, case_id: str, status: str
) -> bool:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.recovery_cases.get(case_id)
            if not row or row["owner_handle"] != owner_handle:
                return False
            row["status"] = status
            row["completed_at"] = _now() if status == "completed" else None
            return True

    owned_ids = {product["id"] for product in list_owner_products(owner_handle=owner_handle)}
    result = (
        get_supabase()
        .table("recovery_cases")
        .select("id,product_id")
        .eq("id", case_id)
        .limit(1)
        .execute()
    )
    if not result.data or result.data[0]["product_id"] not in owned_ids:
        return False
    get_supabase().table("recovery_cases").update(
        {
            "status": status,
            "completed_at": datetime.now(timezone.utc).isoformat() if status == "completed" else None,
        }
    ).eq("id", case_id).execute()
    return True


def create_price_watch(
    *, owner_handle: str, product_id: str, asin: str, product_url: str, target_price: float
) -> dict[str, Any]:
    if uses_memory_store():
        with _MEMORY.lock:
            product = _MEMORY.products.get(product_id)
            if not product or product["owner_handle"] != owner_handle:
                raise LookupError("Product was not found for this message sender.")
            row = {
                "id": str(uuid.uuid4()), "owner_handle": owner_handle, "product_id": product_id,
                "provider": "keepa", "asin": asin, "product_url": product_url,
                "target_price": round(target_price, 2), "current_price": None, "provider_title": None,
                "last_checked_at": None, "check_error": None, "is_active": True,
                "next_check_at": _now(),
                "created_at": _now(), "updated_at": _now(),
            }
            _MEMORY.price_watches[row["id"]] = row
            return copy.deepcopy(row)

    client = get_supabase()
    product = (
        client.table("products").select("id").eq("id", product_id)
        .eq("owner_handle", owner_handle).limit(1).execute()
    )
    if not product.data:
        raise LookupError("Product was not found for this message sender.")
    result = client.table("price_watches").insert({
        "product_id": product_id, "owner_handle": owner_handle, "provider": "keepa",
        "asin": asin, "product_url": product_url, "target_price": round(target_price, 2),
    }).execute()
    return result.data[0]


def list_owner_price_watches(*, owner_handle: str) -> list[dict[str, Any]]:
    if uses_memory_store():
        with _MEMORY.lock:
            rows = [copy.deepcopy(row) for row in _MEMORY.price_watches.values() if row["owner_handle"] == owner_handle]
        rows.sort(key=lambda row: row["created_at"])
        return rows
    result = (
        get_supabase().table("price_watches")
        .select("id,owner_handle,product_id,provider,asin,product_url,target_price,current_price,provider_title,last_checked_at,next_check_at,check_error,is_active,created_at")
        .eq("owner_handle", owner_handle).order("created_at").execute()
    )
    return result.data or []


def list_active_price_watches(*, limit: int = 25) -> list[dict[str, Any]]:
    if uses_memory_store():
        with _MEMORY.lock:
            now = _now()
            rows = [
                copy.deepcopy(row) for row in _MEMORY.price_watches.values()
                if row.get("is_active") and row.get("next_check_at", row.get("created_at", "")) <= now
            ]
        rows.sort(key=lambda row: row.get("next_check_at") or row.get("created_at") or "")
        return rows[:limit]
    now = _now()
    result = (
        get_supabase().table("price_watches")
        .select("id,owner_handle,product_id,provider,asin,product_url,target_price,current_price,provider_title,last_checked_at,next_check_at,check_error,is_active,created_at")
        .eq("is_active", True).lte("next_check_at", now)
        .order("next_check_at").limit(limit).execute()
    )
    return result.data or []


def record_price_check(
    *, owner_handle: str, watch_id: str, current_price: float | None,
    provider_title: str | None = None, error: str | None = None,
) -> dict[str, Any] | None:
    now = _now()
    from datetime import timedelta

    values = {
        "check_error": error[:250] if error else None,
        "next_check_at": (datetime.now(timezone.utc) + timedelta(hours=24)).isoformat(),
        "updated_at": now,
    }
    if current_price is not None:
        values.update({
            "current_price": round(current_price, 2),
            "provider_title": provider_title,
            "last_checked_at": now,
        })
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.price_watches.get(watch_id)
            if not row or row["owner_handle"] != owner_handle:
                return None
            row.update(values)
            return copy.deepcopy(row)
    result = (
        get_supabase().table("price_watches").update(values)
        .eq("id", watch_id).eq("owner_handle", owner_handle).execute()
    )
    return result.data[0] if result.data else None


def pause_price_watch(*, owner_handle: str, watch_id: str) -> bool:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.price_watches.get(watch_id)
            if not row or row["owner_handle"] != owner_handle:
                return False
            row["is_active"] = False
            row["updated_at"] = _now()
            return True
    result = (
        get_supabase().table("price_watches").update({"is_active": False, "updated_at": _now()})
        .eq("id", watch_id).eq("owner_handle", owner_handle).execute()
    )
    return bool(result.data)


def enqueue_notification(*, owner_handle: str, dedupe_key: str, message: str) -> None:
    row = {"owner_handle": owner_handle, "dedupe_key": dedupe_key, "message": message}
    if uses_memory_store():
        with _MEMORY.lock:
            _MEMORY.notification_outbox.setdefault(dedupe_key, {
                **row, "id": str(uuid.uuid4()), "created_at": _now(), "sent_at": None,
            })
        return
    try:
        get_supabase().table("notification_outbox").insert(row).execute()
    except Exception as error:
        # A duplicate dedupe key means the notification is already queued/sent.
        if "23505" not in str(error) and "duplicate key" not in str(error).casefold():
            raise


def list_pending_notifications(*, limit: int = 50) -> list[dict[str, Any]]:
    if uses_memory_store():
        with _MEMORY.lock:
            rows = [copy.deepcopy(row) for row in _MEMORY.notification_outbox.values() if row.get("sent_at") is None]
        rows.sort(key=lambda row: row.get("created_at", ""))
        return rows[:limit]
    result = (
        get_supabase().table("notification_outbox")
        .select("id,owner_handle,message,created_at")
        .is_("sent_at", "null").order("created_at").limit(limit).execute()
    )
    return result.data or []


def acknowledge_notification(*, notification_id: str) -> bool:
    if uses_memory_store():
        with _MEMORY.lock:
            row = next((item for item in _MEMORY.notification_outbox.values() if item["id"] == notification_id), None)
            if not row:
                return False
            row["sent_at"] = _now()
            return True
    result = (
        get_supabase().table("notification_outbox").update({"sent_at": _now()})
        .eq("id", notification_id).is_("sent_at", "null").execute()
    )
    return bool(result.data)


def delete_owner_products(*, owner_handle: str) -> int:
    """Delete an owner's product records; related evidence cascades in Postgres."""
    if uses_memory_store():
        with _MEMORY.lock:
            product_ids = [pid for pid, row in _MEMORY.products.items() if row["owner_handle"] == owner_handle]
            for pid in product_ids:
                del _MEMORY.products[pid]
            _MEMORY.matches = {key: row for key, row in _MEMORY.matches.items() if row["product_id"] not in product_ids}
            _MEMORY.follow_ups = [row for row in _MEMORY.follow_ups if row["product_id"] not in product_ids]
            _MEMORY.recovery_cases = {
                case_id: row
                for case_id, row in _MEMORY.recovery_cases.items()
                if row["product_id"] not in product_ids
            }
            _MEMORY.price_watches = {
                watch_id: row for watch_id, row in _MEMORY.price_watches.items()
                if row["product_id"] not in product_ids
            }
            _MEMORY.conversations.pop(owner_handle, None)
            _MEMORY.receipt_images = {
                key: row for key, row in _MEMORY.receipt_images.items() if key[0] != owner_handle
            }
            _MEMORY.notification_outbox = {
                key: row for key, row in _MEMORY.notification_outbox.items()
                if row["owner_handle"] != owner_handle
            }
            _MEMORY.photon_events = {
                eid: row for eid, row in _MEMORY.photon_events.items() if row["owner_handle"] != owner_handle
            }
            return len(product_ids)

    client = get_supabase()
    receipt_assets = client.table("receipt_images").select("storage_path").eq("owner_handle", owner_handle).execute()
    paths = [row["storage_path"] for row in (receipt_assets.data or [])]
    if paths:
        client.storage.from_("receipt-images").remove(paths)
    client.table("receipt_images").delete().eq("owner_handle", owner_handle).execute()
    client.table("notification_outbox").delete().eq("owner_handle", owner_handle).execute()
    result = client.table("products").select("id").eq("owner_handle", owner_handle).execute()
    count = len(result.data or [])
    client.table("products").delete().eq("owner_handle", owner_handle).execute()
    client.table("conversations").delete().eq("owner_handle", owner_handle).execute()
    client.table("photon_events").delete().eq("owner_handle", owner_handle).execute()
    return count


def claim_photon_event(*, event_id: str, owner_handle: str, event_type: str = "message") -> bool:
    if uses_memory_store():
        with _MEMORY.lock:
            existing = _MEMORY.photon_events.get(event_id)
            if existing is None:
                _MEMORY.photon_events[event_id] = {
                    "event_id": event_id,
                    "owner_handle": owner_handle,
                    "event_type": event_type,
                    "received_at": _now(),
                    "processed_at": None,
                    "processing_error": None,
                }
                return True
            if existing.get("processed_at") or not existing.get("processing_error"):
                return False
            existing["processing_error"] = None
            existing["received_at"] = _now()
            return True

    try:
        get_supabase().table("photon_events").insert(
            {"event_id": event_id, "owner_handle": owner_handle, "event_type": event_type}
        ).execute()
        return True
    except Exception as error:
        if "23505" in str(error) or "duplicate key" in str(error).casefold():
            result = (
                get_supabase()
                .table("photon_events")
                .select("processed_at,processing_error")
                .eq("event_id", event_id)
                .limit(1)
                .execute()
            )
            row = result.data[0] if result.data else {}
            if row.get("processed_at") or not row.get("processing_error"):
                return False
            get_supabase().table("photon_events").update(
                {"processing_error": None, "received_at": datetime.now(timezone.utc).isoformat()}
            ).eq("event_id", event_id).execute()
            return True
        raise


def mark_photon_event_processed(*, event_id: str) -> None:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.photon_events.get(event_id)
            if row:
                row["processed_at"] = _now()
        return
    get_supabase().table("photon_events").update({"processed_at": datetime.now(timezone.utc).isoformat()}).eq(
        "event_id", event_id
    ).execute()


def mark_photon_event_failed(*, event_id: str, error: str) -> None:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.photon_events.get(event_id)
            if row:
                row["processing_error"] = error[:500]
        return
    get_supabase().table("photon_events").update({"processing_error": error[:500]}).eq("event_id", event_id).execute()


def get_conversation(*, owner_handle: str) -> dict[str, Any]:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.conversations.get(owner_handle)
            return copy.deepcopy(row) if row else {"state": "idle", "payload": {}}

    result = (
        get_supabase()
        .table("conversations")
        .select("state,payload")
        .eq("owner_handle", owner_handle)
        .limit(1)
        .execute()
    )
    return result.data[0] if result.data else {"state": "idle", "payload": {}}


def set_conversation(*, owner_handle: str, state: str, payload: dict[str, Any]) -> None:
    if uses_memory_store():
        with _MEMORY.lock:
            _MEMORY.conversations[owner_handle] = {
                "owner_handle": owner_handle,
                "state": state,
                "payload": copy.deepcopy(payload),
                "updated_at": _now(),
            }
        return

    get_supabase().table("conversations").upsert(
        {
            "owner_handle": owner_handle,
            "state": state,
            "payload": payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()


def update_product_identifiers(
    *,
    product_id: str,
    owner_handle: str,
    updates: dict[str, Any],
    provenance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.products.get(product_id)
            if not row or row["owner_handle"] != owner_handle:
                raise LookupError("Product was not found for this message sender.")
            identifiers = {**(row.get("identifiers") or {}), **updates}
            row["identifiers"] = identifiers
            field_provenance = copy.deepcopy(row.get("field_provenance") or {})
            default_provenance = {
                "source": "user",
                "user_confirmed": True,
                "confirmed_at": _now(),
            }
            for key in updates:
                field_provenance[key] = copy.deepcopy((provenance or {}).get(key) or default_provenance)
            row["field_provenance"] = field_provenance
            row["updated_at"] = _now()
            return copy.deepcopy(row)

    client = get_supabase()
    existing = (
        client.table("products")
        .select("identifiers,field_provenance")
        .eq("id", product_id)
        .eq("owner_handle", owner_handle)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise LookupError("Product was not found for this message sender.")
    identifiers = {**(existing.data[0].get("identifiers") or {}), **updates}
    field_provenance = {**(existing.data[0].get("field_provenance") or {})}
    default_provenance = {
        "source": "user",
        "user_confirmed": True,
        "confirmed_at": datetime.now(timezone.utc).isoformat(),
    }
    for key in updates:
        field_provenance[key] = copy.deepcopy((provenance or {}).get(key) or default_provenance)
    result = (
        client.table("products")
        .update(
            {
                "identifiers": identifiers,
                "field_provenance": field_provenance,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        .eq("id", product_id)
        .eq("owner_handle", owner_handle)
        .execute()
    )
    return result.data[0]


def update_product_status(*, product_id: str, owner_handle: str, status: str) -> bool:
    if uses_memory_store():
        with _MEMORY.lock:
            row = _MEMORY.products.get(product_id)
            if not row or row["owner_handle"] != owner_handle:
                return False
            row["status"] = status
            row["updated_at"] = _now()
            return True

    result = (
        get_supabase()
        .table("products")
        .update({"status": status, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", product_id)
        .eq("owner_handle", owner_handle)
        .execute()
    )
    return bool(result.data)


def save_match_evidence(
    *,
    product_id: str,
    notice: dict[str, Any],
    decision: str,
    evidence: dict[str, Any],
) -> None:
    if uses_memory_store():
        with _MEMORY.lock:
            key = (notice["agency"], notice["recall_number"])
            notice_row = _MEMORY.notices.get(key)
            if notice_row is None:
                notice_row = {
                    "id": str(uuid.uuid4()),
                    "agency": notice["agency"],
                    "external_id": notice["recall_number"],
                    "title": notice["title"],
                    "official_url": notice["url"],
                    "affected_criteria": {
                        "models": notice["affected_models"],
                        "date_code_rule": notice["date_code_rule"],
                    },
                    "retrieved_at": _now(),
                    "historical_fixture": notice["historical_fixture"],
                }
                _MEMORY.notices[key] = notice_row
            match_key = (product_id, notice_row["id"])
            _MEMORY.matches[match_key] = {
                "id": str(uuid.uuid4()),
                "product_id": product_id,
                "notice_id": notice_row["id"],
                "decision": decision,
                "evidence": copy.deepcopy(evidence),
                "checked_at": _now(),
            }
        return

    client = get_supabase()
    client.table("notices").upsert(
        {
            "agency": notice["agency"],
            "external_id": notice["recall_number"],
            "title": notice["title"],
            "official_url": notice["url"],
            "affected_criteria": {
                "models": notice["affected_models"],
                "date_code_rule": notice["date_code_rule"],
            },
            "retrieved_at": datetime.now(timezone.utc).isoformat(),
            "historical_fixture": notice["historical_fixture"],
        },
        on_conflict="agency,external_id",
    ).execute()
    notice_row = (
        client.table("notices")
        .select("id")
        .eq("agency", notice["agency"])
        .eq("external_id", notice["recall_number"])
        .limit(1)
        .execute()
    ).data[0]
    client.table("matches").upsert(
        {
            "product_id": product_id,
            "notice_id": notice_row["id"],
            "decision": decision,
            "evidence": evidence,
            "checked_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="product_id,notice_id",
    ).execute()


def create_follow_up(*, product_id: str, notice_id: str | None, action: str) -> None:
    if uses_memory_store():
        with _MEMORY.lock:
            _MEMORY.follow_ups.append(
                {
                    "id": str(uuid.uuid4()),
                    "product_id": product_id,
                    "notice_id": notice_id,
                    "action": action,
                    "due_at": None,
                    "acknowledged_at": None,
                    "completed_at": None,
                    "created_at": _now(),
                }
            )
        return
    get_supabase().table("follow_ups").insert(
        {"product_id": product_id, "notice_id": notice_id, "action": action}
    ).execute()


def complete_latest_follow_up(*, product_id: str) -> bool:
    if uses_memory_store():
        with _MEMORY.lock:
            pending = [
                row
                for row in _MEMORY.follow_ups
                if row["product_id"] == product_id and row.get("completed_at") is None
            ]
            if not pending:
                return False
            pending.sort(key=lambda item: item["created_at"], reverse=True)
            pending[0]["completed_at"] = _now()
            pending[0]["acknowledged_at"] = _now()
            return True

    result = (
        get_supabase()
        .table("follow_ups")
        .select("id")
        .eq("product_id", product_id)
        .is_("completed_at", "null")
        .order("created_at", desc=True)
        .limit(1)
        .execute()
    )
    if not result.data:
        return False
    get_supabase().table("follow_ups").update(
        {"completed_at": datetime.now(timezone.utc).isoformat(), "acknowledged_at": datetime.now(timezone.utc).isoformat()}
    ).eq("id", result.data[0]["id"]).execute()
    return True


def reset_memory_store() -> None:
    """Test/demo helper: wipe the in-memory backend."""
    with _MEMORY.lock:
        _MEMORY.products.clear()
        _MEMORY.notices.clear()
        _MEMORY.matches.clear()
        _MEMORY.follow_ups.clear()
        _MEMORY.recovery_cases.clear()
        _MEMORY.price_watches.clear()
        _MEMORY.receipt_images.clear()
        _MEMORY.notification_outbox.clear()
        _MEMORY.account_links.clear()
        _MEMORY.account_link_codes.clear()
        _MEMORY.photon_events.clear()
        _MEMORY.conversations.clear()


def get_dashboard_snapshot(*, owner_handle: str | None = None) -> dict[str, Any]:
    """Team/demo dashboard payload: products, matches, notices, and follow-ups."""
    if uses_memory_store():
        with _MEMORY.lock:
            products = sorted(
                (
                    copy.deepcopy(row) for row in _MEMORY.products.values()
                    if owner_handle is None or row["owner_handle"] == owner_handle
                ),
                key=lambda item: item.get("created_at") or "",
                reverse=True,
            )
            notices = [copy.deepcopy(row) for row in _MEMORY.notices.values()]
            product_ids = {row["id"] for row in products}
            matches = sorted(
                (copy.deepcopy(row) for row in _MEMORY.matches.values() if row["product_id"] in product_ids),
                key=lambda item: item.get("checked_at") or "",
                reverse=True,
            )
            follow_ups = sorted(
                (copy.deepcopy(row) for row in _MEMORY.follow_ups if row["product_id"] in product_ids),
                key=lambda item: item.get("created_at") or "",
                reverse=True,
            )
            recovery_cases = sorted(
                (copy.deepcopy(row) for row in _MEMORY.recovery_cases.values() if row["product_id"] in product_ids),
                key=lambda item: (item.get("due_date") or "9999-12-31", item.get("created_at") or ""),
            )
            price_watches = sorted(
                (copy.deepcopy(row) for row in _MEMORY.price_watches.values() if row["product_id"] in product_ids),
                key=lambda item: item.get("created_at") or "",
            )
        return {
            "products": products,
            "notices": notices,
            "matches": matches,
            "follow_ups": follow_ups,
            "recovery_cases": recovery_cases,
            "price_watches": price_watches,
            "persistence": "memory",
        }

    client = get_supabase()
    products_query = (
        client.table("products")
        .select("id,owner_handle,brand,product_name,identifiers,field_provenance,status,monitoring_enabled,source_type,created_at,updated_at")
    )
    if owner_handle is not None:
        products_query = products_query.eq("owner_handle", owner_handle)
    products = products_query.order("created_at", desc=True).limit(100).execute().data or []
    notices = (
        client.table("notices")
        .select("id,agency,external_id,title,official_url,affected_criteria,retrieved_at,historical_fixture")
        .order("retrieved_at", desc=True)
        .limit(100)
        .execute()
        .data
        or []
    )
    product_ids = [product["id"] for product in products]
    matches = (
        client.table("matches")
        .select("id,product_id,notice_id,decision,evidence,checked_at")
        .in_("product_id", product_ids)
        .order("checked_at", desc=True)
        .limit(100)
        .execute()
        .data if product_ids else []
    ) or []
    follow_ups = (
        client.table("follow_ups")
        .select("id,product_id,notice_id,action,due_at,acknowledged_at,completed_at,created_at")
        .in_("product_id", product_ids)
        .order("created_at", desc=True)
        .limit(100)
        .execute()
        .data if product_ids else []
    ) or []
    recovery_cases = (
        client.table("recovery_cases")
        .select("id,product_id,case_type,due_date,status,date_source,created_at,completed_at")
        .in_("product_id", product_ids)
        .order("due_date")
        .order("created_at")
        .limit(200)
        .execute()
        .data
        if product_ids
        else []
    ) or []
    price_watches = (
        client.table("price_watches")
        .select("id,owner_handle,product_id,provider,asin,product_url,target_price,current_price,provider_title,last_checked_at,next_check_at,check_error,is_active,created_at")
        .in_("product_id", product_ids)
        .order("created_at")
        .limit(200)
        .execute()
        .data
        if product_ids
        else []
    ) or []
    return {
        "products": products,
        "notices": notices,
        "matches": matches,
        "follow_ups": follow_ups,
        "recovery_cases": recovery_cases,
        "price_watches": price_watches,
        "persistence": "supabase",
    }
