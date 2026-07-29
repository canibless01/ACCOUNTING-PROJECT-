"""
Transaction writer service.

Full insert pipeline:
  1. Gmail message ID fast-path duplicate check
  2. Content-fingerprint duplicate check
  3. Determine transaction type (income / expense) from direction
  4. Categorization engine (parsing rules → keyword fallback)
  5. Status progression: Raw → Validated → Categorized
  6. Set balance_after from parsed email's available-balance field
  7. Insert row (DB UNIQUE on fingerprint is final safety net)
  8. Internal transfer detection — link both legs
  9. Update account.actual_balance when email reported it
  10. Trigger reconciliation recompute (best-effort)
"""
from __future__ import annotations

from datetime import date as dt_date
from typing import Optional

from parsers.base import ParsedTransaction
from services.categorization import categorize_transaction
from services.duplicate_check import build_fingerprint, find_transfer_pair
from db import get_admin_client


def write_transaction(
    user_id: str,
    account_id: str,
    sender_email: str,
    parsed: ParsedTransaction,
    source: str = "email",
    gmail_message_id: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """
    Full insert pipeline for a parsed transaction.

    Returns (inserted_row_or_None, outcome) where outcome is one of:
      'inserted', 'duplicate', 'error'
    """
    client = get_admin_client()

    date_str = (
        parsed.date.isoformat()
        if isinstance(parsed.date, dt_date)
        else str(parsed.date)
    )

    # ── 1. gmail_message_id fast-path dedup ────────────────────────────────────
    if gmail_message_id:
        existing = (
            client.table("transactions")
            .select("id")
            .eq("gmail_message_id", gmail_message_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            return None, "duplicate"

    # ── 2. Content fingerprint dedup ───────────────────────────────────────────
    fp = build_fingerprint(account_id, parsed.reference, parsed.amount, date_str)
    existing = (
        client.table("transactions")
        .select("id")
        .eq("fingerprint", fp)
        .limit(1)
        .execute()
    )
    if existing.data:
        return None, "duplicate"

    # ── 3. Determine type ──────────────────────────────────────────────────────
    tx_type = "income" if parsed.direction == "credit" else "expense"

    # ── 4. Categorization ──────────────────────────────────────────────────────
    category_id, category_matched = categorize_transaction(
        user_id=user_id,
        sender_email=sender_email,
        narration=parsed.narration or "",
        direction=parsed.direction,
    )

    # ── 5. Status progression ──────────────────────────────────────────────────
    #   Raw → Validated (direction confirmed from parsed email — always achievable)
    #   Validated → Categorized (category rule matched)
    if category_matched and category_id:
        status = "Categorized"
        needs_review = False
    else:
        status = "Validated"
        needs_review = True

    # ── 6. balance_after — snapshot from the email's Available Balance ─────────
    #   When the bank includes the post-transaction balance in the alert email,
    #   we store it directly as balance_after. This is the most accurate value
    #   possible (straight from the bank). When not available, leave NULL and
    #   let compute_account_balance() derive it on demand.
    balance_after = float(parsed.actual_balance) if parsed.actual_balance is not None else None

    # ── 7. Build and insert row ────────────────────────────────────────────────
    row = {
        "user_id":        user_id,
        "account_id":     account_id,
        "amount":         float(parsed.amount),
        "direction":      parsed.direction,
        "type":           tx_type,
        "category_id":    category_id,
        "narration":      parsed.narration,
        "reference":      parsed.reference,
        "date":           date_str,
        "date_time":      parsed.date_time.isoformat() if parsed.date_time else None,
        "status":         status,
        "needs_review":   needs_review,
        "parser_version": parsed.parser_version,
        "fingerprint":    fp,
        "balance_after":  balance_after,
        "source":         source,
        "gmail_message_id": gmail_message_id,
    }

    try:
        result = client.table("transactions").insert(row).execute()
        if not result.data:
            return None, "error"
        inserted = result.data[0]
    except Exception as exc:
        err = str(exc).lower()
        if "unique" in err or "duplicate" in err:
            return None, "duplicate"
        raise

    # ── 8. Internal transfer detection ─────────────────────────────────────────
    transfer_pair_id = find_transfer_pair(
        user_id=user_id,
        amount=float(parsed.amount),
        date_str=date_str,
        direction=parsed.direction,
        excluding_account_id=account_id,
    )
    if transfer_pair_id:
        tx_id = inserted["id"]
        # Tag the newly inserted leg
        client.table("transactions").update({
            "type": "transfer",
            "is_transfer_pair_id": transfer_pair_id,
        }).eq("id", tx_id).execute()
        # Tag the existing opposite leg
        client.table("transactions").update({
            "type": "transfer",
            "is_transfer_pair_id": tx_id,
        }).eq("id", transfer_pair_id).execute()

        inserted["type"] = "transfer"
        inserted["is_transfer_pair_id"] = transfer_pair_id

    # ── 9. Update account.actual_balance when email reported it ────────────────
    if parsed.actual_balance is not None:
        client.table("accounts").update({
            "actual_balance": float(parsed.actual_balance),
            "actual_balance_at": (
                parsed.date_time.isoformat()
                if parsed.date_time
                else f"{date_str}T00:00:00"
            ),
        }).eq("id", account_id).execute()

        # ── 10. Recompute reconciliation ──────────────────────────────────────
        _trigger_reconciliation(account_id)

    return inserted, "inserted"


def write_manual_transaction(
    user_id: str,
    account_id: str,
    amount: float,
    direction: str,
    category_id: Optional[str],
    narration: Optional[str],
    reference: str,
    date_str: str,
    note: Optional[str] = None,
) -> tuple[Optional[dict], str]:
    """
    Insert a manually entered transaction (cash account or correction entry).
    Runs both reference and fingerprint dedup checks before insert.
    """
    client = get_admin_client()

    ref_upper = reference.strip().upper()

    # Reference uniqueness per account (belt-and-suspenders for manual entries)
    ref_exists = (
        client.table("transactions")
        .select("id")
        .eq("account_id", account_id)
        .eq("reference", ref_upper)
        .limit(1)
        .execute()
    )
    if ref_exists.data:
        return None, "duplicate_reference"

    fp = build_fingerprint(account_id, ref_upper, amount, date_str)
    fp_exists = (
        client.table("transactions")
        .select("id")
        .eq("fingerprint", fp)
        .limit(1)
        .execute()
    )
    if fp_exists.data:
        return None, "duplicate"

    tx_type = "income" if direction == "credit" else "expense"
    narration_full = narration or ""
    if note:
        narration_full = f"{narration_full} | Note: {note}".strip(" |")

    row = {
        "user_id":      user_id,
        "account_id":   account_id,
        "amount":       float(amount),
        "direction":    direction,
        "type":         tx_type,
        "category_id":  category_id,
        "narration":    narration_full[:500] if narration_full else None,
        "reference":    ref_upper,
        "date":         date_str,
        "status":       "Validated",
        "needs_review": not bool(category_id),
        "fingerprint":  fp,
        "source":       "manual",
    }

    try:
        result = client.table("transactions").insert(row).execute()
        if not result.data:
            return None, "error"
        return result.data[0], "inserted"
    except Exception as exc:
        exc_str = str(exc).lower()
        if "unique" in exc_str:
            return None, "duplicate"
        # Numeric overflow (Postgres error 22003) means the amount value is too
        # large for the NUMERIC(15,2) column.  Return "error" instead of re-raising
        # so the sync engine can save a failed_import record and continue with the
        # next message rather than crashing the entire sync job.
        if "22003" in str(exc) or "overflow" in exc_str or "numeric" in exc_str:
            return None, "error"
        raise


def _trigger_reconciliation(account_id: str) -> None:
    """Best-effort recompute of reconciliation status after a balance update."""
    try:
        from db import get_admin_client as _get
        _get().rpc("update_reconciliation_status", {"p_account_id": account_id}).execute()
    except Exception:
        pass
