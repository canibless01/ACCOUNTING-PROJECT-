"""
End-of-day digest compiler.
Runs after each sync to gather totals and write a digest record.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timezone

from db import get_admin_client

logger = logging.getLogger(__name__)


def compile_digest(user_id: str, sync_alert_messages: list[str] | None = None) -> dict:
    """
    Gather post-sync statistics and upsert a digest record for today.
    Returns the digest row data.
    """
    client = get_admin_client()
    today = date.today().isoformat()
    alerts = list(sync_alert_messages or [])

    # ── New transactions inserted today ────────────────────────────────────────
    new_tx_result = (
        client.table("transactions")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .gte("created_at", f"{today}T00:00:00")
        .execute()
    )
    new_tx_count = new_tx_result.count or 0

    # ── Transactions needing review ────────────────────────────────────────────
    review_result = (
        client.table("transactions")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("needs_review", True)
        .execute()
    )
    review_count = review_result.count or 0

    # ── Reconciliation mismatches ──────────────────────────────────────────────
    recon_result = (
        client.table("accounts")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .eq("reconciliation_status", "mismatch")
        .execute()
    )
    recon_count = recon_result.count or 0
    if recon_count > 0:
        alerts.append(f"⚠ {recon_count} account(s) have a reconciliation mismatch.")

    # ── Failed imports ─────────────────────────────────────────────────────────
    fail_result = (
        client.table("failed_imports")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .execute()
    )
    fail_count = fail_result.count or 0
    if fail_count > 0:
        alerts.append(f"⚠ {fail_count} email(s) could not be parsed and are awaiting review.")

    if review_count > 0:
        alerts.append(f"📋 {review_count} transaction(s) need categorisation.")

    # ── Upsert digest record ───────────────────────────────────────────────────
    digest_data = {
        "user_id": user_id,
        "date": today,
        "new_transaction_count": new_tx_count,
        "needs_review_count": review_count,
        "reconciliation_mismatch_count": recon_count,
        "failed_import_count": fail_count,
        "alert_messages": alerts,
        "sent_at": datetime.now(timezone.utc).isoformat(),
    }

    # Check if a digest already exists for today
    existing = (
        client.table("digests")
        .select("id")
        .eq("user_id", user_id)
        .eq("date", today)
        .limit(1)
        .execute()
    )

    if existing.data:
        result = (
            client.table("digests")
            .update(digest_data)
            .eq("id", existing.data[0]["id"])
            .execute()
        )
    else:
        result = client.table("digests").insert(digest_data).execute()

    return result.data[0] if result.data else digest_data


def get_unread_digests(user_id: str) -> list[dict]:
    """Return all unread digests for a user, newest first."""
    client = get_admin_client()
    return (
        client.table("digests")
        .select("*")
        .eq("user_id", user_id)
        .eq("is_read", False)
        .order("date", desc=True)
        .execute()
    ).data or []


def mark_digest_read(user_id: str, digest_id: str) -> None:
    """Mark a specific digest as read."""
    client = get_admin_client()
    client.table("digests").update({"is_read": True}).eq("id", digest_id).eq("user_id", user_id).execute()


def mark_all_digests_read(user_id: str) -> None:
    """Mark all digests for a user as read."""
    client = get_admin_client()
    client.table("digests").update({"is_read": True}).eq("user_id", user_id).eq("is_read", False).execute()
