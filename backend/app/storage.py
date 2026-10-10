from __future__ import annotations

from functools import lru_cache
from datetime import datetime, timezone
from typing import Any

from supabase import Client, create_client

from .settings import get_settings


@lru_cache
def get_supabase() -> Client:
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
) -> dict[str, Any]:
    """Store only user-confirmed structured fields; uploaded images are not stored here."""
    result = (
        get_supabase()
        .table("products")
        .insert(
            {
                "owner_handle": owner_handle,
                "brand": brand,
                "product_name": product_name,
                "identifiers": identifiers,
                "source_type": source_type,
                "monitoring_enabled": False,
            }
        )
        .execute()
    )
    return result.data[0]


def enable_monitoring(*, product_id: str, owner_handle: str) -> dict[str, Any]:
    """Monitoring is opt-in and can only be enabled by an explicit user action."""
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


def delete_owner_products(*, owner_handle: str) -> int:
    """Delete an owner's product records; related evidence cascades in Postgres."""
    client = get_supabase()
    result = client.table("products").select("id").eq("owner_handle", owner_handle).execute()
    count = len(result.data or [])
    client.table("products").delete().eq("owner_handle", owner_handle).execute()
    client.table("conversations").delete().eq("owner_handle", owner_handle).execute()
    client.table("photon_events").delete().eq("owner_handle", owner_handle).execute()
    return count


def claim_photon_event(*, event_id: str, owner_handle: str, event_type: str = "message") -> bool:
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
    get_supabase().table("photon_events").update({"processed_at": datetime.now(timezone.utc).isoformat()}).eq(
        "event_id", event_id
    ).execute()


def mark_photon_event_failed(*, event_id: str, error: str) -> None:
    get_supabase().table("photon_events").update({"processing_error": error[:500]}).eq("event_id", event_id).execute()


def get_conversation(*, owner_handle: str) -> dict[str, Any]:
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
    get_supabase().table("conversations").upsert(
        {
            "owner_handle": owner_handle,
            "state": state,
            "payload": payload,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
    ).execute()


def update_product_identifiers(*, product_id: str, owner_handle: str, updates: dict[str, Any]) -> dict[str, Any]:
    client = get_supabase()
    existing = (
        client.table("products")
        .select("identifiers")
        .eq("id", product_id)
        .eq("owner_handle", owner_handle)
        .limit(1)
        .execute()
    )
    if not existing.data:
        raise LookupError("Product was not found for this message sender.")
    identifiers = {**(existing.data[0].get("identifiers") or {}), **updates}
    result = (
        client.table("products")
        .update({"identifiers": identifiers, "updated_at": datetime.now(timezone.utc).isoformat()})
        .eq("id", product_id)
        .eq("owner_handle", owner_handle)
        .execute()
    )
    return result.data[0]


def update_product_status(*, product_id: str, owner_handle: str, status: str) -> bool:
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
    get_supabase().table("follow_ups").insert(
        {"product_id": product_id, "notice_id": notice_id, "action": action}
    ).execute()


def complete_latest_follow_up(*, product_id: str) -> bool:
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
