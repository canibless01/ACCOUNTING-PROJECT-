"""
Duplicate detection service.

Two-layer deduplication strategy
---------------------------------
Layer 1 — gmail_message_id (fastest, most reliable):
  If the gmail_message_id is already in the transactions table, this is the
  exact same email being re-processed (e.g. sync ran twice). Skip immediately.

Layer 2 — Content fingerprint:
  MD5(account_id|reference|amount|date) — catches the case where the same
  transaction was somehow emailed twice under different message IDs.
  The fingerprint also has a UNIQUE constraint in the DB as a final safety net.

Transfer detection
------------------
find_transfer_pair() looks for a transaction in another account with:
  - Same user
  - Same absolute amount
  - Opposite direction
  - Within 24 hours (configurable)
  - Not already linked to another transfer

This covers money moved between the user's own bank accounts.
"""
from __future__ import annotations

import hashlib
from typing import Optional

from db import get_admin_client


def build_fingerprint(account_id: str, reference: str, amount: float, date_str: str) -> str:
    """
    Compute the MD5 fingerprint used for duplicate detection.

    Matches the Python logic — uses the same field order as any DB-side checks:
      MD5(account_id | reference | amount:.2f | date)
    """
    raw = f"{account_id}|{reference}|{amount:.2f}|{date_str}"
    return hashlib.md5(raw.encode()).hexdigest()


def is_duplicate(
    account_id: str,
    reference: str,
    amount: float,
    date_str: str,
    gmail_message_id: Optional[str] = None,
) -> bool:
    """
    Return True if this transaction already exists.

    Checks Layer 1 (gmail_message_id) then Layer 2 (fingerprint).
    Either match is sufficient to call it a duplicate.
    """
    client = get_admin_client()

    # Layer 1: gmail_message_id fast-path (exact same email re-processed)
    if gmail_message_id:
        result = (
            client.table("transactions")
            .select("id")
            .eq("gmail_message_id", gmail_message_id)
            .limit(1)
            .execute()
        )
        if result.data:
            return True

    # Layer 2: content fingerprint
    fp = build_fingerprint(account_id, reference, amount, date_str)
    result = (
        client.table("transactions")
        .select("id")
        .eq("fingerprint", fp)
        .limit(1)
        .execute()
    )
    return bool(result.data)


def is_gmail_id_already_failed(user_id: str, gmail_message_id: str) -> bool:
    """
    Return True if this gmail_message_id already has a failed_imports record.
    Prevents the same unparseable email from being logged multiple times.
    """
    if not gmail_message_id:
        return False
    client = get_admin_client()
    result = (
        client.table("failed_imports")
        .select("id")
        .eq("user_id", user_id)
        .eq("gmail_message_id", gmail_message_id)
        .in_("status", ["pending", "ignored"])
        .limit(1)
        .execute()
    )
    return bool(result.data)


def find_transfer_pair(
    user_id: str,
    amount: float,
    date_str: str,
    direction: str,
    excluding_account_id: str,
    time_window_hours: int = 24,
) -> Optional[str]:
    """
    Look for a matching transfer leg among other connected accounts.

    A transfer pair is:
      - Same user
      - Same absolute amount
      - Opposite direction (debit ↔ credit)
      - Within ±time_window_hours of the same date
      - In a different account
      - Not already linked (is_transfer_pair_id IS NULL)

    Returns the matching transaction ID, or None.
    """
    opposite = "debit" if direction == "credit" else "credit"
    client = get_admin_client()

    from datetime import date as dt_date, timedelta
    try:
        tx_date = dt_date.fromisoformat(date_str)
    except ValueError:
        return None

    from_date = (tx_date - timedelta(hours=time_window_hours)).isoformat()
    to_date = (tx_date + timedelta(hours=time_window_hours)).isoformat()

    result = (
        client.table("transactions")
        .select("id")
        .eq("user_id", user_id)
        .eq("amount", amount)
        .eq("direction", opposite)
        .neq("account_id", excluding_account_id)
        .is_("is_transfer_pair_id", "null")
        .gte("date", from_date)
        .lte("date", to_date)
        .limit(1)
        .execute()
    )
    if result.data:
        return result.data[0]["id"]
    return None
