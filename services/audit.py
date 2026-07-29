"""
Audit log service.
Append-only writes to audit_log for manual syncs, balance adjustments,
category changes, and other significant events.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from db import get_admin_client

logger = logging.getLogger(__name__)


def log_audit(
    user_id: str,
    action_type: str,
    description: str,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    before_value: Optional[dict] = None,
    after_value: Optional[dict] = None,
    metadata: Optional[dict] = None,
    ip_address: Optional[str] = None,
) -> Optional[dict]:
    """
    Append one record to audit_log.
    Silently swallows errors so audit logging never breaks business logic.
    """
    try:
        client = get_admin_client()
        row = {
            "user_id": user_id,
            "action_type": action_type,
            "description": description[:2000],
        }
        if entity_type:
            row["entity_type"] = entity_type
        if entity_id:
            row["entity_id"] = entity_id
        if before_value is not None:
            row["before_value"] = before_value
        if after_value is not None:
            row["after_value"] = after_value
        if metadata is not None:
            row["metadata"] = metadata
        if ip_address:
            row["ip_address"] = ip_address

        result = client.table("audit_log").insert(row).execute()
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error(f"Audit log write failed: {exc}")
        return None


def get_audit_log(
    user_id: str,
    limit: int = 50,
    offset: int = 0,
    entity_type: Optional[str] = None,
    entity_id: Optional[str] = None,
    action_type: Optional[str] = None,
) -> list[dict]:
    """Return audit log entries for a user, newest first."""
    client = get_admin_client()
    q = (
        client.table("audit_log")
        .select("*")
        .eq("user_id", user_id)
        .order("created_at", desc=True)
        .range(offset, offset + limit - 1)
    )
    if entity_type:
        q = q.eq("entity_type", entity_type)
    if entity_id:
        q = q.eq("entity_id", entity_id)
    if action_type:
        q = q.eq("action_type", action_type)
    return q.execute().data or []
