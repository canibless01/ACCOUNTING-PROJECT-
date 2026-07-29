"""
Review Queue & Failed Imports endpoints.
Surfaces transactions needing categorization and unparseable emails.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from auth_middleware import require_auth
from db import get_admin_client
from services.audit import log_audit
from services.categorization import apply_manual_categorization
from services.transaction_writer import write_manual_transaction

bp = Blueprint("review", __name__, url_prefix="/api/review")


# ── Review Queue ────────────────────────────────────────────────────────────────

@bp.get("/queue")
@require_auth
def get_review_queue():
    """
    Return transactions where needs_review=True, paginated.
    Query params: page, per_page, account_id
    """
    user_id = g.user_id
    client = get_admin_client()

    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 25)), 100)
    offset = (page - 1) * per_page

    q = (
        client.table("transactions")
        .select(
            "id,account_id,amount,direction,type,narration,reference,date,status,"
            "category_id,categories(name,color),accounts(name,sender_label,sender_email),"
            "parser_version,created_at",
            count="exact",
        )
        .eq("user_id", user_id)
        .eq("needs_review", True)
        .order("date", desc=True)
        .order("created_at", desc=True)
        .range(offset, offset + per_page - 1)
    )

    if request.args.get("account_id"):
        q = q.eq("account_id", request.args["account_id"])

    result = q.execute()
    total = result.count or 0

    return jsonify({
        "transactions": result.data or [],
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": -(-(total) // per_page),
        "reviewed": 0,  # client tracks progress
    })


@bp.post("/queue/<tx_id>/categorize")
@require_auth
def categorize_review_item(tx_id):
    """
    Assign a category to a review-queue transaction.
    Also upserts a parsing_rule so the same narration auto-matches next time.
    Body: {category_id, create_rule?}
    """
    user_id = g.user_id
    data = request.get_json() or {}
    category_id = data.get("category_id")
    if not category_id:
        return jsonify({"error": "category_id is required"}), 400

    client = get_admin_client()
    tx_rows = (
        client.table("transactions")
        .select("id,narration,account_id,accounts(sender_email),needs_review")
        .eq("id", tx_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data
    if not tx_rows:
        return jsonify({"error": "Transaction not found"}), 404

    tx = tx_rows[0]
    if not tx.get("needs_review"):
        return jsonify({"error": "This transaction is not in the review queue"}), 400

    sender_email = (tx.get("accounts") or {}).get("sender_email", "")
    create_rule = bool(data.get("create_rule", True))

    apply_manual_categorization(
        user_id=user_id,
        transaction_id=tx_id,
        category_id=category_id,
        narration=tx.get("narration", ""),
        sender_email=sender_email,
        also_create_rule=create_rule,
    )

    log_audit(
        user_id=user_id,
        action_type="category_change",
        entity_type="transaction",
        entity_id=tx_id,
        description=(
            f"Review queue: transaction categorized. Rule created: {create_rule}."
        ),
        ip_address=request.remote_addr,
    )

    return jsonify({
        "ok": True,
        "tx_id": tx_id,
        "category_id": category_id,
        "rule_created": create_rule,
    })


@bp.post("/queue/<tx_id>/skip")
@require_auth
def skip_review_item(tx_id):
    """
    Skip a review-queue item for now (leaves needs_review=True but updates a deferred flag).
    The frontend tracks skipped items in session; no server state change needed.
    """
    return jsonify({"ok": True, "tx_id": tx_id, "action": "skipped"})


# ── Failed Imports ─────────────────────────────────────────────────────────────

@bp.get("/failed-imports")
@require_auth
def list_failed_imports():
    """
    List pending failed imports for the user.
    Query params: page, per_page, account_id
    """
    user_id = g.user_id
    client = get_admin_client()

    page = max(int(request.args.get("page", 1)), 1)
    per_page = min(int(request.args.get("per_page", 25)), 100)
    offset = (page - 1) * per_page

    q = (
        client.table("failed_imports")
        .select(
            "id,account_id,sender_email,raw_subject,raw_content,raw_from,raw_date,"
            "failure_reason,gmail_message_id,status,created_at,expires_at,"
            "accounts(name,sender_label)",
            count="exact",
        )
        .eq("user_id", user_id)
        .eq("status", "pending")
        .order("created_at", desc=True)
        .range(offset, offset + per_page - 1)
    )

    if request.args.get("account_id"):
        q = q.eq("account_id", request.args["account_id"])

    result = q.execute()
    return jsonify({
        "failed_imports": result.data or [],
        "total": result.count or 0,
        "page": page,
        "per_page": per_page,
    })


@bp.get("/failed-imports/<import_id>")
@require_auth
def get_failed_import(import_id):
    """Return the full content of a single failed import."""
    client = get_admin_client()
    rows = (
        client.table("failed_imports")
        .select("*,accounts(name,sender_label,sender_email)")
        .eq("id", import_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Failed import not found"}), 404
    return jsonify({"failed_import": rows[0]})


@bp.post("/failed-imports/<import_id>/convert")
@require_auth
def convert_failed_import(import_id):
    """
    Convert a failed import to a manual transaction.
    Body: {account_id, amount, direction, reference, date, category_id?, narration?, note?}
    """
    user_id = g.user_id
    client = get_admin_client()

    rows = (
        client.table("failed_imports")
        .select("id,status")
        .eq("id", import_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Failed import not found"}), 404
    if rows[0]["status"] != "pending":
        return jsonify({"error": "This import has already been processed"}), 409

    data = request.get_json() or {}
    required = ["account_id", "amount", "direction", "reference", "date"]
    for field in required:
        if not data.get(field):
            return jsonify({"error": f"'{field}' is required"}), 400

    try:
        amount = float(data["amount"])
    except (TypeError, ValueError):
        return jsonify({"error": "amount must be a number"}), 400

    row, outcome = write_manual_transaction(
        user_id=user_id,
        account_id=data["account_id"],
        amount=amount,
        direction=data["direction"],
        category_id=data.get("category_id"),
        narration=data.get("narration"),
        reference=data["reference"],
        date_str=data["date"],
        note=data.get("note"),
    )

    if outcome in ("duplicate", "duplicate_reference"):
        return jsonify({"error": "A transaction with this reference already exists"}), 409
    if outcome == "error" or row is None:
        return jsonify({"error": "Failed to create transaction"}), 500

    # Mark the failed import as converted
    client.table("failed_imports").update({
        "status": "converted",
        "converted_to_transaction_id": row["id"],
    }).eq("id", import_id).execute()

    log_audit(
        user_id=user_id,
        action_type="failed_import_converted",
        entity_type="failed_import",
        entity_id=import_id,
        description=f"Failed import converted to transaction {row['id']}",
        ip_address=request.remote_addr,
    )
    return jsonify({"transaction": row, "failed_import_id": import_id}), 201


@bp.post("/failed-imports/<import_id>/ignore")
@require_auth
def ignore_failed_import(import_id):
    """Dismiss a failed import (mark as ignored)."""
    client = get_admin_client()
    rows = (
        client.table("failed_imports")
        .select("id,status")
        .eq("id", import_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Failed import not found"}), 404

    client.table("failed_imports").update({"status": "ignored"}).eq("id", import_id).execute()

    log_audit(
        user_id=g.user_id,
        action_type="failed_import_ignored",
        entity_type="failed_import",
        entity_id=import_id,
        description="Failed import dismissed by user",
        ip_address=request.remote_addr,
    )
    return jsonify({"ok": True})


@bp.get("/stats")
@require_auth
def review_stats():
    """Quick summary counters for the Review Queue page header."""
    user_id = g.user_id
    client = get_admin_client()

    needs_review = (
        client.table("transactions")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("needs_review", True)
        .execute()
    ).count or 0

    failed_imports = (
        client.table("failed_imports")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("status", "pending")
        .execute()
    ).count or 0

    return jsonify({
        "needs_review": needs_review,
        "failed_imports": failed_imports,
        "total": needs_review + failed_imports,
    })
