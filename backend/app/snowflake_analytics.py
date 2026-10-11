"""Optional Snowflake analytics layer.

Supabase/memory remains the system of record. Confirmed purchases, recall
decisions, and recovery actions are copied here asynchronously for insights.
"""

from __future__ import annotations

import logging
import threading
from datetime import date, datetime, timezone
from functools import lru_cache
from typing import Any

from .settings import get_settings

logger = logging.getLogger("lodestone.snowflake")

_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()


def snowflake_enabled() -> bool:
    settings = get_settings()
    return bool(
        settings.snowflake_account
        and settings.snowflake_user
        and settings.snowflake_password
        and settings.snowflake_warehouse
        and settings.snowflake_database
    )


def schedule(fn, *args: Any, **kwargs: Any) -> None:
    """Fire-and-forget so iMessage replies never wait on Snowflake."""
    if not snowflake_enabled():
        return

    def runner() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:
            logger.exception("Snowflake background task failed: %s", getattr(fn, "__name__", fn))

    threading.Thread(target=runner, name="lodestone-snowflake", daemon=True).start()


@lru_cache
def _connect():
    import snowflake.connector

    settings = get_settings()
    return snowflake.connector.connect(
        account=settings.snowflake_account,
        user=settings.snowflake_user,
        password=settings.snowflake_password,
        warehouse=settings.snowflake_warehouse,
        database=settings.snowflake_database,
        schema=settings.snowflake_schema or "PUBLIC",
        role=settings.snowflake_role or None,
    )


def _ensure_schema() -> None:
    global _SCHEMA_READY
    if _SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _SCHEMA_READY:
            return
        conn = _connect()
        cur = conn.cursor()
        try:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS LODESTONE_PRODUCTS (
                    PRODUCT_ID VARCHAR PRIMARY KEY,
                    OWNER_HANDLE VARCHAR,
                    BRAND VARCHAR,
                    PRODUCT_NAME VARCHAR,
                    STATUS VARCHAR,
                    MONITORING_ENABLED BOOLEAN,
                    SOURCE_TYPE VARCHAR,
                    CREATED_AT TIMESTAMP_TZ,
                    UPDATED_AT TIMESTAMP_TZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS LODESTONE_MATCHES (
                    MATCH_KEY VARCHAR PRIMARY KEY,
                    PRODUCT_ID VARCHAR,
                    OWNER_HANDLE VARCHAR,
                    DECISION VARCHAR,
                    NOTICE_TITLE VARCHAR,
                    OFFICIAL_URL VARCHAR,
                    CHECKED_AT TIMESTAMP_TZ
                )
                """
            )
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS LODESTONE_RECOVERY_CASES (
                    CASE_ID VARCHAR PRIMARY KEY,
                    PRODUCT_ID VARCHAR,
                    OWNER_HANDLE VARCHAR,
                    CASE_TYPE VARCHAR,
                    DUE_DATE DATE,
                    STATUS VARCHAR,
                    CREATED_AT TIMESTAMP_TZ
                )
                """
            )
            conn.commit()
            _SCHEMA_READY = True
        finally:
            cur.close()


def upsert_product(product: dict[str, Any]) -> None:
    _ensure_schema()
    conn = _connect()
    cur = conn.cursor()
    now = datetime.now(timezone.utc).isoformat()
    try:
        cur.execute("DELETE FROM LODESTONE_PRODUCTS WHERE PRODUCT_ID = %s", (product["id"],))
        cur.execute(
            """
            INSERT INTO LODESTONE_PRODUCTS (
                PRODUCT_ID, OWNER_HANDLE, BRAND, PRODUCT_NAME, STATUS,
                MONITORING_ENABLED, SOURCE_TYPE, CREATED_AT, UPDATED_AT
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                product["id"],
                product.get("owner_handle"),
                product.get("brand"),
                product.get("product_name"),
                product.get("status") or "owned",
                bool(product.get("monitoring_enabled")),
                product.get("source_type"),
                product.get("created_at") or now,
                product.get("updated_at") or now,
            ),
        )
        conn.commit()
    finally:
        cur.close()


def upsert_match(
    *,
    product_id: str,
    owner_handle: str | None,
    decision: str,
    notice_title: str,
    official_url: str,
    checked_at: str | None = None,
) -> None:
    _ensure_schema()
    match_key = f"{product_id}:{official_url}"
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM LODESTONE_MATCHES WHERE MATCH_KEY = %s", (match_key,))
        cur.execute(
            """
            INSERT INTO LODESTONE_MATCHES (
                MATCH_KEY, PRODUCT_ID, OWNER_HANDLE, DECISION,
                NOTICE_TITLE, OFFICIAL_URL, CHECKED_AT
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                match_key,
                product_id,
                owner_handle,
                decision,
                notice_title,
                official_url,
                checked_at or datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        cur.close()


def upsert_recovery_case(case: dict[str, Any]) -> None:
    _ensure_schema()
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM LODESTONE_RECOVERY_CASES WHERE CASE_ID = %s", (case["id"],))
        cur.execute(
            """
            INSERT INTO LODESTONE_RECOVERY_CASES (
                CASE_ID, PRODUCT_ID, OWNER_HANDLE, CASE_TYPE, DUE_DATE, STATUS, CREATED_AT
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (
                case["id"],
                case.get("product_id"),
                case.get("owner_handle"),
                case.get("case_type"),
                case.get("due_date"),
                case.get("status") or "open",
                case.get("created_at") or datetime.now(timezone.utc).isoformat(),
            ),
        )
        conn.commit()
    finally:
        cur.close()


def delete_owner_analytics(*, owner_handle: str) -> None:
    _ensure_schema()
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute("DELETE FROM LODESTONE_MATCHES WHERE OWNER_HANDLE = %s", (owner_handle,))
        cur.execute("DELETE FROM LODESTONE_RECOVERY_CASES WHERE OWNER_HANDLE = %s", (owner_handle,))
        cur.execute("DELETE FROM LODESTONE_PRODUCTS WHERE OWNER_HANDLE = %s", (owner_handle,))
        conn.commit()
    finally:
        cur.close()


def _metrics_from_rows(
    product_count: int,
    monitoring_count: int,
    affected_count: int,
    possible_count: int,
    open_recovery: int,
    due_soon: int,
    overdue: int,
) -> dict[str, Any]:
    return {
        "product_count": product_count,
        "monitoring_count": monitoring_count,
        "affected_count": affected_count,
        "possible_match_count": possible_count,
        "open_recovery_count": open_recovery,
        "due_within_30_days": due_soon,
        "overdue_count": overdue,
    }


def _narrative_from_metrics(metrics: dict[str, Any]) -> str:
    parts = [
        f"{metrics['product_count']} saved purchase(s)",
        f"{metrics['monitoring_count']} with monitoring on",
        f"{metrics['affected_count']} historical affected recall match(es)",
        f"{metrics['possible_match_count']} possible match(es) needing review",
        f"{metrics['open_recovery_count']} open recovery action(s)",
        f"{metrics['due_within_30_days']} due within 30 days",
        f"{metrics['overdue_count']} overdue",
    ]
    return (
        "Purchase insights: "
        + "; ".join(parts)
        + ". Exact identifier verification currently covers the labeled myCharge POWER HUB notice only."
    )


def _local_insights() -> dict[str, Any]:
    from .storage import get_dashboard_snapshot

    snapshot = get_dashboard_snapshot()
    products = snapshot.get("products") or []
    matches = snapshot.get("matches") or []
    cases = snapshot.get("recovery_cases") or []
    today = date.today()
    open_cases = [case for case in cases if case.get("status") == "open"]
    due_soon = 0
    overdue = 0
    for case in open_cases:
        try:
            due = date.fromisoformat(str(case.get("due_date")))
        except ValueError:
            continue
        delta = (due - today).days
        if delta < 0:
            overdue += 1
        elif delta <= 30:
            due_soon += 1
    metrics = _metrics_from_rows(
        product_count=len(products),
        monitoring_count=sum(1 for item in products if item.get("monitoring_enabled")),
        affected_count=sum(1 for item in matches if item.get("decision") == "affected"),
        possible_count=sum(1 for item in matches if item.get("decision") == "possible_match"),
        open_recovery=len(open_cases),
        due_soon=due_soon,
        overdue=overdue,
    )
    return {
        "source": "local",
        "snowflake_enabled": False,
        "used_cortex": False,
        "summary": _narrative_from_metrics(metrics),
        "metrics": metrics,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def _snowflake_insights() -> dict[str, Any]:
    _ensure_schema()
    conn = _connect()
    cur = conn.cursor()
    try:
        cur.execute("SELECT COUNT(*), SUM(IFF(MONITORING_ENABLED, 1, 0)) FROM LODESTONE_PRODUCTS")
        product_count, monitoring_count = cur.fetchone()
        cur.execute(
            """
            SELECT
                SUM(IFF(DECISION = 'affected', 1, 0)),
                SUM(IFF(DECISION = 'possible_match', 1, 0))
            FROM LODESTONE_MATCHES
            """
        )
        affected_count, possible_count = cur.fetchone()
        cur.execute("SELECT COUNT(*) FROM LODESTONE_RECOVERY_CASES WHERE STATUS = 'open'")
        open_recovery = cur.fetchone()[0]
        cur.execute(
            """
            SELECT
                SUM(IFF(STATUS = 'open' AND DUE_DATE < CURRENT_DATE(), 1, 0)),
                SUM(IFF(
                    STATUS = 'open'
                    AND DUE_DATE BETWEEN CURRENT_DATE() AND DATEADD(day, 30, CURRENT_DATE()),
                    1, 0
                ))
            FROM LODESTONE_RECOVERY_CASES
            """
        )
        overdue, due_soon = cur.fetchone()

        metrics = _metrics_from_rows(
            product_count=int(product_count or 0),
            monitoring_count=int(monitoring_count or 0),
            affected_count=int(affected_count or 0),
            possible_count=int(possible_count or 0),
            open_recovery=int(open_recovery or 0),
            due_soon=int(due_soon or 0),
            overdue=int(overdue or 0),
        )
        summary = _narrative_from_metrics(metrics)
        used_cortex = False
        settings = get_settings()
        if settings.snowflake_use_cortex:
            prompt = (
                "You are Lodestone, a household purchase recovery assistant. "
                "In 2 short sentences, summarize these metrics for a demo dashboard. "
                "Be concrete. Do not invent product names. Mention that exact recall "
                f"verification currently covers only the labeled myCharge POWER HUB notice. Metrics: {metrics}"
            )
            try:
                cur.execute(
                    "SELECT SNOWFLAKE.CORTEX.COMPLETE(%s, %s)",
                    (settings.snowflake_cortex_model, prompt),
                )
                cortex_summary = cur.fetchone()[0]
                if cortex_summary and str(cortex_summary).strip():
                    summary = str(cortex_summary).strip()
                    used_cortex = True
            except Exception:
                logger.exception("Snowflake Cortex summary failed; using SQL narrative")

        return {
            "source": "snowflake",
            "snowflake_enabled": True,
            "used_cortex": used_cortex,
            "summary": summary,
            "metrics": metrics,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    finally:
        cur.close()


def get_purchase_insights() -> dict[str, Any]:
    if not snowflake_enabled():
        return _local_insights()
    try:
        return _snowflake_insights()
    except Exception:
        logger.exception("Snowflake insights failed; falling back to local snapshot")
        fallback = _local_insights()
        fallback["source"] = "local_fallback"
        fallback["snowflake_enabled"] = True
        return fallback
