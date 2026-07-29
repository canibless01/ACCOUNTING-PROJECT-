"""
Account management endpoints.
CRUD for accounts, balance calculations, reconciliation.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from auth_middleware import require_auth
from db import get_admin_client
from services.audit import log_audit
from services.balance import (
    get_account_balance,
    get_all_balances,
    get_balance_over_time,
    get_reconciliation_summary,
    record_balance_adjustment,
    recompute_reconciliation,
)

bp = Blueprint("accounts", __name__, url_prefix="/api/accounts")


@bp.get("")
@require_auth
def list_accounts():
    """Return all active accounts with computed balances."""
    accounts = get_all_balances(g.user_id)
    return jsonify({"accounts": accounts, "total": len(accounts)})


@bp.post("")
@require_auth
def create_account():
    """
    Create a new account (manual/cash-only or Gmail-connected).
    Body: {name, sender_email?, sender_label?, opening_balance?, is_manual?, gmail_connection_id?}
    """
    data = request.get_json() or {}
    name = data.get("name", "").strip()
    if not name:
        return jsonify({"error": "Account name is required"}), 400

    client = get_admin_client()

    # Validate gmail_connection_id if provided
    gmail_connection_id = data.get("gmail_connection_id") or None
    if gmail_connection_id:
        conn_rows = (
            client.table("gmail_connections")
            .select("id")
            .eq("id", gmail_connection_id)
            .eq("user_id", g.user_id)
            .limit(1)
            .execute()
        ).data
        if not conn_rows:
            return jsonify({"error": "Gmail connection not found"}), 404

    account_type = data.get("type") or data.get("account_type") or "bank"
    if account_type not in ("bank", "mobile", "cash"):
        account_type = "bank"

    opening_balance_date = data.get("opening_balance_date") or None

    row = {
        "user_id": g.user_id,
        "name": name,
        "account_type": account_type,
        "sender_email": data.get("sender_email") or None,
        "sender_label": data.get("sender_label") or None,
        "opening_balance": float(data.get("opening_balance", 0)),
        "opening_balance_date": opening_balance_date,
        "is_manual": bool(data.get("is_manual", account_type == "cash")),
        "currency": data.get("currency", "NGN"),
        "gmail_connection_id": gmail_connection_id,
    }
    try:
        result = client.table("accounts").insert(row).execute()
    except Exception as exc:
        err = str(exc).lower()
        if "unique" in err or "duplicate" in err:
            return jsonify({"error": "An account with this sender email already exists"}), 409
        raise
    if not result.data:
        return jsonify({"error": "Failed to create account"}), 500

    account_id = result.data[0]["id"]
    log_audit(
        user_id=g.user_id,
        action_type="account_created",
        entity_type="account",
        entity_id=account_id,
        description=f"Account created: {name}",
        after_value=row,
        ip_address=request.remote_addr,
    )
    # Re-read from account_summary so the response includes computed fields
    # (type, calculated_balance, opening_balance_date, etc.) in the shape the
    # frontend expects (account_summary.type instead of accounts.account_type).
    summary = (
        client.table("account_summary")
        .select("*")
        .eq("id", account_id)
        .limit(1)
        .execute()
    ).data
    account = summary[0] if summary else result.data[0]
    return jsonify({"account": account}), 201


@bp.get("/<account_id>")
@require_auth
def get_account(account_id):
    """Return a single account with computed balance."""
    client = get_admin_client()
    rows = (
        client.table("account_summary")
        .select("*")
        .eq("id", account_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Account not found"}), 404
    return jsonify({"account": rows[0]})


@bp.patch("/<account_id>")
@require_auth
def update_account(account_id):
    """
    Update account metadata.
    Body: {name?, sender_label?, currency?, gmail_connection_id?}
    Opening balance changes must go through /balance-adjustment.
    """
    client = get_admin_client()
    existing = (
        client.table("accounts")
        .select("id")
        .eq("id", account_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not existing:
        return jsonify({"error": "Account not found"}), 404

    data = request.get_json() or {}
    allowed = ["name", "sender_label", "currency", "gmail_connection_id", "sender_email", "account_type", "opening_balance_date"]
    updates = {k: v for k, v in data.items() if k in allowed}

    # Accept frontend "type" field and map it to account_type column
    if "type" in data and "account_type" not in updates:
        t = data["type"]
        if t in ("bank", "mobile", "cash"):
            updates["account_type"] = t

    # Validate gmail_connection_id
    if "gmail_connection_id" in updates and updates["gmail_connection_id"]:
        conn_rows = (
            client.table("gmail_connections")
            .select("id")
            .eq("id", updates["gmail_connection_id"])
            .eq("user_id", g.user_id)
            .limit(1)
            .execute()
        ).data
        if not conn_rows:
            return jsonify({"error": "Gmail connection not found"}), 404

    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    client.table("accounts").update(updates).eq("id", account_id).execute()

    # Re-read from account_summary for a consistently shaped response
    summary = (
        client.table("account_summary")
        .select("*")
        .eq("id", account_id)
        .limit(1)
        .execute()
    ).data
    return jsonify({"account": summary[0] if summary else {}})


@bp.delete("/<account_id>")
@require_auth
def deactivate_account(account_id):
    """Soft-delete (deactivate) an account."""
    client = get_admin_client()
    rows = (
        client.table("accounts")
        .select("id,name")
        .eq("id", account_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Account not found"}), 404

    client.table("accounts").update({"is_active": False}).eq("id", account_id).execute()
    log_audit(
        user_id=g.user_id,
        action_type="account_disconnected",
        entity_type="account",
        entity_id=account_id,
        description=f"Account deactivated: {rows[0]['name']}",
        ip_address=request.remote_addr,
    )
    return jsonify({"ok": True})


@bp.get("/<account_id>/balance")
@require_auth
def account_balance(account_id):
    """Return current computed balance for the account."""
    _assert_owns_account(account_id, g.user_id)
    balance = get_account_balance(account_id)
    return jsonify({"account_id": account_id, "balance": round(balance, 2)})


@bp.get("/<account_id>/balance-history")
@require_auth
def account_balance_history(account_id):
    """Daily balance snapshots. Query param: days (default 30)."""
    _assert_owns_account(account_id, g.user_id)
    days = int(request.args.get("days", 30))
    days = min(max(days, 7), 365)
    history = get_balance_over_time(account_id, days)
    return jsonify({"history": history, "days": days})


@bp.post("/<account_id>/balance-adjustment")
@require_auth
def balance_adjustment(account_id):
    """
    Adjust the account's opening balance (with audit log entry).
    Body: {new_opening_balance, reason}
    """
    _assert_owns_account(account_id, g.user_id)
    data = request.get_json() or {}
    new_balance = data.get("new_opening_balance")
    reason = data.get("reason", "Manual adjustment").strip()

    if new_balance is None:
        return jsonify({"error": "new_opening_balance is required"}), 400
    try:
        new_balance = float(new_balance)
    except (TypeError, ValueError):
        return jsonify({"error": "new_opening_balance must be a number"}), 400

    result = record_balance_adjustment(g.user_id, account_id, new_balance, reason)
    return jsonify(result)


@bp.post("/<account_id>/recompute-reconciliation")
@require_auth
def recompute_account_reconciliation(account_id):
    """Force-recompute reconciliation status for the account."""
    _assert_owns_account(account_id, g.user_id)
    status = recompute_reconciliation(account_id)
    return jsonify({"account_id": account_id, "reconciliation_status": status})


@bp.get("/<account_id>/transactions")
@require_auth
def account_transactions(account_id):
    """
    Filtered transaction list for a single account.
    Supports: status, category_id, date_from, date_to, direction, page, per_page
    """
    _assert_owns_account(account_id, g.user_id)
    client = get_admin_client()

    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 50)), 200)
    offset = (page - 1) * per_page

    q = (
        client.table("transactions")
        .select(
            "id,amount,direction,type,narration,reference,date,status,needs_review,"
            "category_id,categories(name,color),created_at,parser_version,balance_after,source",
            count="exact",
        )
        .eq("account_id", account_id)
        .eq("user_id", g.user_id)
        .order("date", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + per_page - 1)
    )

    q = _apply_tx_filters(q, request.args)
    result = q.execute()
    return jsonify({
        "transactions": result.data or [],
        "total": result.count or 0,
        "page": page,
        "per_page": per_page,
    })


# ── Helpers ────────────────────────────────────────────────────────────────────

def _assert_owns_account(account_id: str, user_id: str):
    client = get_admin_client()
    rows = (
        client.table("accounts")
        .select("id")
        .eq("id", account_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        from flask import abort
        abort(404)


def _apply_tx_filters(q, args):
    if args.get("status"):
        q = q.eq("status", args["status"])
    if args.get("direction"):
        q = q.eq("direction", args["direction"])
    if args.get("category_id"):
        q = q.eq("category_id", args["category_id"])
    if args.get("date_from"):
        q = q.gte("date", args["date_from"])
    if args.get("date_to"):
        q = q.lte("date", args["date_to"])
    if args.get("needs_review") == "true":
        q = q.eq("needs_review", True)
    return q
