"""
Balance & reconciliation service.
Computes running account balances and checks them against actual balances.
"""
from __future__ import annotations

from typing import Optional

from db import db_rpc, db_select, db_update, get_admin_client


def get_account_balance(account_id: str) -> float:
    """
    Return the computed running balance for an account:
      opening_balance + SUM(credits) - SUM(debits) for Validated/Categorized/Reconciled transactions.
    """
    result = db_rpc("compute_account_balance", {"p_account_id": account_id})
    return float(result) if result is not None else 0.0


def get_all_balances(user_id: str) -> list[dict]:
    """
    Return balance summary for every active account of a user.
    Each item: {id, name, calculated_balance, actual_balance, reconciliation_status, ...}
    """
    client = get_admin_client()
    rows = (
        client.table("account_summary")
        .select("*")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
    ).data or []
    return rows


def get_total_balance(user_id: str) -> float:
    """Combined calculated balance across all active accounts."""
    accounts = get_all_balances(user_id)
    return sum(float(a.get("calculated_balance") or 0) for a in accounts)


def recompute_reconciliation(account_id: str) -> str:
    """Recompute and persist reconciliation status. Returns 'ok', 'mismatch', or 'unknown'."""
    result = db_rpc("update_reconciliation_status", {"p_account_id": account_id})
    return result or "unknown"


def record_balance_adjustment(
    user_id: str,
    account_id: str,
    new_opening_balance: float,
    reason: str,
) -> dict:
    """
    Adjust an account's opening balance.
    This is the correct way to correct a balance — never silently editing history.
    Logs to audit_log automatically.
    """
    from services.audit import log_audit

    # Fetch old value
    rows = db_select("accounts", {"id": account_id}, "opening_balance,name")
    old_balance = float(rows[0]["opening_balance"]) if rows else 0.0

    db_update("accounts", {"opening_balance": new_opening_balance}, {"id": account_id})

    log_audit(
        user_id=user_id,
        action_type="balance_adjustment",
        entity_type="account",
        entity_id=account_id,
        description=(
            f"Opening balance adjusted for '{rows[0]['name'] if rows else account_id}': "
            f"₦{old_balance:,.2f} → ₦{new_opening_balance:,.2f}. Reason: {reason}"
        ),
        before_value={"opening_balance": old_balance},
        after_value={"opening_balance": new_opening_balance},
        metadata={"reason": reason},
    )

    # Re-run reconciliation after adjustment
    recompute_reconciliation(account_id)

    return {"account_id": account_id, "new_opening_balance": new_opening_balance}


def get_balance_over_time(account_id: str, days: int = 30) -> list[dict]:
    """
    Return daily balance snapshots for the last N days.
    Uses cumulative transaction sums.
    """
    from datetime import date, timedelta
    client = get_admin_client()

    rows = db_select("accounts", {"id": account_id}, "opening_balance")
    opening = float(rows[0]["opening_balance"]) if rows else 0.0

    today = date.today()
    start_date = today - timedelta(days=days)

    tx_rows = (
        client.table("transactions")
        .select("date,direction,amount")
        .eq("account_id", account_id)
        .neq("status", "Raw")
        .gte("date", start_date.isoformat())
        .order("date", desc=False)
        .execute()
    ).data or []

    # Group by date
    daily_deltas: dict[str, float] = {}
    for tx in tx_rows:
        d = tx["date"]
        delta = float(tx["amount"]) if tx["direction"] == "credit" else -float(tx["amount"])
        daily_deltas[d] = daily_deltas.get(d, 0.0) + delta

    # Get all transactions before start_date to compute opening at start_date
    prior_txs = (
        client.table("transactions")
        .select("direction,amount")
        .eq("account_id", account_id)
        .neq("status", "Raw")
        .lt("date", start_date.isoformat())
        .execute()
    ).data or []
    running = opening + sum(
        (float(t["amount"]) if t["direction"] == "credit" else -float(t["amount"]))
        for t in prior_txs
    )

    result = []
    current = start_date
    while current <= today:
        d_str = current.isoformat()
        running += daily_deltas.get(d_str, 0.0)
        result.append({"date": d_str, "balance": round(running, 2)})
        current += timedelta(days=1)

    return result


def get_reconciliation_summary(user_id: str) -> list[dict]:
    """
    Return a list of accounts currently showing a reconciliation mismatch.
    """
    client = get_admin_client()
    rows = (
        client.table("accounts")
        .select(
            "id,name,reconciliation_status,reconciliation_gap,"
            "reconciliation_mismatch_since,actual_balance,actual_balance_at"
        )
        .eq("user_id", user_id)
        .eq("is_active", True)
        .eq("reconciliation_status", "mismatch")
        .order("reconciliation_mismatch_since", desc=False)
        .execute()
    ).data or []

    for row in rows:
        row["calculated_balance"] = get_account_balance(row["id"])

    return rows
