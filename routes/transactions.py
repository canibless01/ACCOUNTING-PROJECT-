"""
Transaction endpoints.
Paginated list, single-row detail/update, manual entry, category assignment.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from auth_middleware import require_auth
from db import get_admin_client
from services.audit import log_audit
from services.categorization import apply_manual_categorization
from services.transaction_writer import write_manual_transaction

bp = Blueprint("transactions", __name__, url_prefix="/api/transactions")


@bp.get("")
@require_auth
def list_transactions():
    """
    Paginated, filterable transaction list.
    Query params: account_id, category_id, status, direction, type,
                  date_from, date_to, needs_review, search, page, per_page
    """
    user_id = g.user_id
    client = get_admin_client()

    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 50)), 200)
    offset = (page - 1) * per_page

    q = (
        client.table("transactions")
        .select(
            "id,account_id,amount,direction,type,narration,reference,date,date_time,"
            "status,needs_review,category_id,categories(name,color),"
            "accounts(name,sender_label),parser_version,balance_after,source,"
            "is_transfer_pair_id,gmail_message_id,fingerprint,"
            "created_at,updated_at",
            count="exact",
        )
        .eq("user_id", user_id)
        .is_("voided_at", "null")
        .order("date", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + per_page - 1)
    )

    # Filters
    for field in ["account_id", "category_id", "direction", "type"]:
        val = request.args.get(field)
        if val:
            q = q.eq(field, val)
    # status is stored title-cased in the DB ('Raw', 'Validated', 'Categorized')
    # but the frontend sends lowercase ('raw', 'validated', 'categorized').
    status_val = request.args.get("status")
    if status_val:
        q = q.eq("status", status_val.capitalize())

    if request.args.get("date_from"):
        q = q.gte("date", request.args["date_from"])
    if request.args.get("date_to"):
        q = q.lte("date", request.args["date_to"])
    if request.args.get("needs_review") == "true":
        q = q.eq("needs_review", True)
    if request.args.get("search"):
        q = q.ilike("narration", f"%{request.args['search']}%")

    result = q.execute()
    # Post-process rows:
    #   - Normalize status to lowercase ('Raw' → 'raw', etc.) to match frontend types.
    #   - Derive is_manual from source so the frontend's tx.isManual works
    #     (the transactions table has no is_manual column; source='manual' is the canonical flag).
    rows = result.data or []
    for row in rows:
        if row.get("status"):
            row["status"] = row["status"].lower()
        row["is_manual"] = row.get("source") == "manual"
    return jsonify({
        "transactions": rows,
        "total": result.count or 0,
        "page": page,
        "per_page": per_page,
        "pages": -(-( result.count or 0) // per_page),  # ceiling division
    })


@bp.get("/<tx_id>")
@require_auth
def get_transaction(tx_id):
    """Return a single transaction with all related data."""
    client = get_admin_client()
    rows = (
        client.table("transactions")
        .select(
            "id,account_id,amount,direction,type,narration,reference,date,date_time,"
            "status,needs_review,category_id,categories(name,color,keywords),"
            "accounts(name,sender_label,sender_email),parser_version,balance_after,"
            "source,is_transfer_pair_id,gmail_message_id,fingerprint,"
            "voided_at,void_reason,created_at,updated_at"
        )
        .eq("id", tx_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Transaction not found"}), 404
    return jsonify({"transaction": rows[0]})


@bp.post("")
@require_auth
def create_manual_transaction():
    """
    Create a manual transaction entry.
    Body: {account_id, amount, direction, reference, date, category_id?, narration?, note?}
    A reference is required to enable duplicate detection.
    """
    user_id = g.user_id
    data = request.get_json() or {}

    required = ["account_id", "amount", "direction", "reference", "date"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"'{field}' is required"}), 400

    direction = data["direction"]
    if direction not in ("credit", "debit"):
        return jsonify({"error": "direction must be 'credit' or 'debit'"}), 400

    try:
        amount = float(data["amount"])
        if amount <= 0:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a positive number"}), 400

    # Verify account belongs to user
    client = get_admin_client()
    acc_rows = (
        client.table("accounts")
        .select("id,sender_email")
        .eq("id", data["account_id"])
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data
    if not acc_rows:
        return jsonify({"error": "Account not found"}), 404

    # Validate category_id exists for this user (if provided)
    category_id = data.get("category_id")
    if category_id:
        cat_rows = (
            client.table("categories")
            .select("id")
            .eq("id", category_id)
            .eq("user_id", user_id)
            .is_("deleted_at", "null")
            .limit(1)
            .execute()
        ).data
        if not cat_rows:
            return jsonify({"error": "category_id not found"}), 404

    row, outcome = write_manual_transaction(
        user_id=user_id,
        account_id=data["account_id"],
        amount=amount,
        direction=direction,
        category_id=data.get("category_id"),
        narration=data.get("narration"),
        reference=data["reference"],
        date_str=data["date"],
        note=data.get("note"),
    )

    if outcome == "duplicate" or outcome == "duplicate_reference":
        return jsonify({
            "error": "A transaction with this reference already exists for this account"
        }), 409

    if outcome == "error" or row is None:
        return jsonify({"error": "Failed to insert transaction"}), 500

    log_audit(
        user_id=user_id,
        action_type="transaction_created",
        entity_type="transaction",
        entity_id=row["id"],
        description=f"Manual transaction: ₦{amount:,.2f} {direction} — {data.get('narration', '')}",
        ip_address=request.remote_addr,
    )
    return jsonify({"transaction": row, "outcome": "inserted"}), 201


@bp.patch("/<tx_id>")
@require_auth
def update_transaction(tx_id):
    """
    Update a transaction row.
    Allowed fields: category_id, type, narration, needs_review, status.
    When category_id is changed and the tx has a narration, also upserts a parsing_rule.
    """
    user_id = g.user_id
    client = get_admin_client()

    tx_rows = (
        client.table("transactions")
        .select("id,category_id,narration,account_id,accounts(sender_email),needs_review,status")
        .eq("id", tx_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data
    if not tx_rows:
        return jsonify({"error": "Transaction not found"}), 404

    tx = tx_rows[0]
    data = request.get_json() or {}

    allowed = ["category_id", "type", "narration", "needs_review", "status"]
    updates = {k: v for k, v in data.items() if k in allowed}

    # Validate status value
    valid_statuses = {"Raw", "Validated", "Categorized"}
    if "status" in updates and updates["status"] not in valid_statuses:
        return jsonify({"error": f"Invalid status '{updates['status']}'. Must be one of: {', '.join(sorted(valid_statuses))}"}), 400

    old_category = tx.get("category_id")
    new_category = updates.get("category_id")

    if new_category and new_category != old_category:
        # Use the full manual categorization flow (which also upserts a parsing rule)
        sender_email = (tx.get("accounts") or {}).get("sender_email", "")
        apply_manual_categorization(
            user_id=user_id,
            transaction_id=tx_id,
            category_id=new_category,
            narration=updates.get("narration") or tx.get("narration", ""),
            sender_email=sender_email,
            also_create_rule=bool(data.get("create_rule", True)),
        )
        # Remove from updates dict so we don't double-apply
        for field in ("category_id", "needs_review", "status"):
            updates.pop(field, None)

    if updates:
        client.table("transactions").update(updates).eq("id", tx_id).execute()

    log_audit(
        user_id=user_id,
        action_type="transaction_updated",
        entity_type="transaction",
        entity_id=tx_id,
        description=f"Transaction updated: {list(updates.keys())} changed",
        before_value={"category_id": old_category},
        after_value={"category_id": new_category or old_category},
        ip_address=request.remote_addr,
    )

    # Return updated transaction
    updated = (
        client.table("transactions")
        .select("*,categories(name,color),accounts(name)")
        .eq("id", tx_id)
        .limit(1)
        .execute()
    ).data
    return jsonify({"transaction": updated[0] if updated else {}})


@bp.delete("/<tx_id>")
@require_auth
def void_transaction(tx_id):
    """
    Soft-void a transaction by setting voided_at to now.
    Body: {reason?}
    """
    from datetime import datetime, timezone
    client = get_admin_client()
    rows = (
        client.table("transactions")
        .select("id")
        .eq("id", tx_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Transaction not found"}), 404

    data = request.get_json(silent=True) or {}
    void_reason = data.get("reason", "Voided by user")

    client.table("transactions").update({
        "voided_at": datetime.now(timezone.utc).isoformat(),
        "void_reason": void_reason,
    }).eq("id", tx_id).execute()

    log_audit(
        user_id=g.user_id,
        action_type="transaction_updated",
        entity_type="transaction",
        entity_id=tx_id,
        description=f"Transaction voided: {void_reason}",
        ip_address=request.remote_addr,
    )
    return jsonify({"ok": True, "tx_id": tx_id})


@bp.post("/<tx_id>/mark-transfer")
@require_auth
def mark_as_transfer(tx_id):
    """Mark a transaction as an internal transfer."""
    client = get_admin_client()
    tx_rows = (
        client.table("transactions")
        .select("id")
        .eq("id", tx_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not tx_rows:
        return jsonify({"error": "Transaction not found"}), 404

    client.table("transactions").update({
        "type": "transfer",
        "needs_review": False,
        "status": "Categorized",
    }).eq("id", tx_id).execute()

    updated = (
        client.table("transactions")
        .select("*,categories(name,color),accounts(name,sender_label)")
        .eq("id", tx_id)
        .limit(1)
        .execute()
    ).data
    return jsonify({"transaction": updated[0] if updated else {"id": tx_id, "type": "transfer"}})


@bp.post("/<tx_id>/flag-mis-parse")
@require_auth
def flag_mis_parse(tx_id):
    """
    Flag a transaction as mis-parsed.
    Sets needs_review=True and reverts status to Raw so it surfaces in the review queue.
    """
    client = get_admin_client()
    rows = (
        client.table("transactions")
        .select("id")
        .eq("id", tx_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Transaction not found"}), 404

    data = request.get_json() or {}
    note = data.get("note", "Flagged as mis-parsed by user")
    client.table("transactions").update({
        "needs_review": True,
        "status": "Validated",
    }).eq("id", tx_id).execute()

    log_audit(
        user_id=g.user_id,
        action_type="transaction_updated",
        entity_type="transaction",
        entity_id=tx_id,
        description=f"Flagged as mis-parsed: {note}",
        ip_address=request.remote_addr,
    )
    return jsonify({"ok": True})


@bp.get("/archive")
@require_auth
def list_archive():
    """
    Paginated view of archived transactions (>2 years old).
    Same filter params as /transactions.
    """
    user_id = g.user_id
    client = get_admin_client()
    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 50)), 200)
    offset = (page - 1) * per_page

    q = (
        client.table("transactions_archive")
        .select("*", count="exact")
        .eq("user_id", user_id)
        .order("date", desc=True)
        .range(offset, offset + per_page - 1)
    )
    result = q.execute()
    return jsonify({
        "transactions": result.data or [],
        "total": result.count or 0,
        "page": page,
        "per_page": per_page,
        "note": "These are archived transactions older than 2 years.",
    })
