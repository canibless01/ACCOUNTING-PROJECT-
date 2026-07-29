"""
Housekeeping jobs:
  - Purge failed_imports older than 30 days
  - Archive transactions older than 2 years
These are called from the /housekeeping cron endpoint.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from db import db_rpc, get_admin_client

logger = logging.getLogger(__name__)


def purge_expired_failed_imports() -> dict:
    """Delete failed_imports rows past their 30-day expiry."""
    try:
        deleted = db_rpc("purge_expired_failed_imports", {})
        count = int(deleted) if deleted is not None else 0
        logger.info(f"Housekeeping: purged {count} expired failed imports")
        return {"purged_failed_imports": count}
    except Exception as exc:
        logger.error(f"Housekeeping purge_failed_imports error: {exc}")
        return {"error": str(exc)}


def archive_old_transactions() -> dict:
    """Move transactions older than 2 years into the archive table."""
    try:
        archived = db_rpc("archive_old_transactions", {})
        count = int(archived) if archived is not None else 0
        logger.info(f"Housekeeping: archived {count} old transactions")
        return {"archived_transactions": count}
    except Exception as exc:
        logger.error(f"Housekeeping archive_transactions error: {exc}")
        return {"error": str(exc)}


def recompute_all_reconciliations(user_id: str) -> dict:
    """
    Recompute reconciliation status for every active account of a user.
    Useful to call after a large sync or bulk categorization change.
    """
    client = get_admin_client()
    accounts = (
        client.table("accounts")
        .select("id")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
    ).data or []

    results = {}
    for account in accounts:
        try:
            status = db_rpc("update_reconciliation_status", {"p_account_id": account["id"]})
            results[account["id"]] = status or "unknown"
        except Exception as exc:
            results[account["id"]] = f"error: {exc}"

    return results


def run_all_housekeeping() -> dict:
    """Run all housekeeping tasks and return a combined summary."""
    return {
        **purge_expired_failed_imports(),
        **archive_old_transactions(),
        "ran_at": datetime.now(timezone.utc).isoformat(),
    }
