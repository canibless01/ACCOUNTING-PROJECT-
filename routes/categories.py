"""
Category management endpoints.
CRUD with cascading reassignment when deleting a category.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from auth_middleware import require_auth
from db import get_admin_client
from services.audit import log_audit

bp = Blueprint("categories", __name__, url_prefix="/api/categories")


@bp.get("")
@require_auth
def list_categories():
    """Return all categories for the user."""
    client = get_admin_client()
    rows = (
        client.table("categories")
        .select("*")
        .eq("user_id", g.user_id)
        .order("name", desc=False)
        .execute()
    ).data or []
    return jsonify({"categories": rows, "total": len(rows)})


@bp.post("")
@require_auth
def create_category():
    """
    Create a new category.
    Body: {name, keywords?, color?, applies_to?}
    """
    user_id = g.user_id
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "Category name is required"}), 400

    client = get_admin_client()
    # Check for duplicate name
    existing = (
        client.table("categories")
        .select("id")
        .eq("user_id", user_id)
        .eq("name", name)
        .limit(1)
        .execute()
    ).data
    if existing:
        return jsonify({"error": f"A category named '{name}' already exists"}), 409

    row = {
        "user_id": user_id,
        "name": name,
        "keywords": data.get("keywords", []),
        "color": data.get("color", "#6B7280"),
        "applies_to": data.get("applies_to", "both"),
        "is_system": False,
    }
    result = client.table("categories").insert(row).execute()
    if not result.data:
        return jsonify({"error": "Failed to create category"}), 500

    log_audit(
        user_id=user_id,
        action_type="category_created",
        entity_type="category",
        entity_id=result.data[0]["id"],
        description=f"Category created: {name}",
        ip_address=request.remote_addr,
    )
    return jsonify({"category": result.data[0]}), 201


@bp.get("/<category_id>")
@require_auth
def get_category(category_id):
    client = get_admin_client()
    rows = (
        client.table("categories")
        .select("*")
        .eq("id", category_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Category not found"}), 404
    return jsonify({"category": rows[0]})


@bp.patch("/<category_id>")
@require_auth
def update_category(category_id):
    """
    Update a category.
    Body: {name?, keywords?, color?, applies_to?}
    """
    user_id = g.user_id
    client = get_admin_client()
    existing = (
        client.table("categories")
        .select("id,name,is_system")
        .eq("id", category_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data
    if not existing:
        return jsonify({"error": "Category not found"}), 404

    data = request.get_json() or {}
    allowed = ["name", "keywords", "color", "applies_to"]
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}

    # Prevent duplicate names
    if "name" in updates:
        dup = (
            client.table("categories")
            .select("id")
            .eq("user_id", user_id)
            .eq("name", updates["name"])
            .neq("id", category_id)
            .limit(1)
            .execute()
        ).data
        if dup:
            return jsonify({"error": f"A category named '{updates['name']}' already exists"}), 409

    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    result = (
        client.table("categories")
        .update(updates)
        .eq("id", category_id)
        .execute()
    )
    log_audit(
        user_id=user_id,
        action_type="category_updated",
        entity_type="category",
        entity_id=category_id,
        description=f"Category updated: {existing[0]['name']}",
        ip_address=request.remote_addr,
    )
    return jsonify({"category": result.data[0] if result.data else {}})


@bp.delete("/<category_id>")
@require_auth
def delete_category(category_id):
    """
    Delete a category. Requires {reassign_to?} to handle existing transactions.
    If reassign_to is provided, moves all transactions to that category before deleting.
    If not, sets category_id to NULL on affected transactions.
    """
    user_id = g.user_id
    client = get_admin_client()
    existing = (
        client.table("categories")
        .select("id,name,is_system")
        .eq("id", category_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data
    if not existing:
        return jsonify({"error": "Category not found"}), 404

    data = request.get_json() or {}
    reassign_to = data.get("reassign_to")

    # Count affected transactions
    tx_count = (
        client.table("transactions")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("category_id", category_id)
        .execute()
    ).count or 0

    if tx_count > 0:
        if reassign_to:
            # Verify reassign_to category exists
            reassign_check = (
                client.table("categories")
                .select("id")
                .eq("id", reassign_to)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            ).data
            if not reassign_check:
                return jsonify({"error": "Reassign target category not found"}), 404

            client.table("transactions").update({
                "category_id": reassign_to,
            }).eq("user_id", user_id).eq("category_id", category_id).execute()

            # Also update parsing_rules
            client.table("parsing_rules").update({
                "category_id": reassign_to,
            }).eq("user_id", user_id).eq("category_id", category_id).execute()
        else:
            # Null out category and set needs_review
            client.table("transactions").update({
                "category_id": None,
                "needs_review": True,
                "status": "Validated",
            }).eq("user_id", user_id).eq("category_id", category_id).execute()

            # Deactivate parsing_rules referencing this category
            client.table("parsing_rules").update({
                "is_active": False,
            }).eq("user_id", user_id).eq("category_id", category_id).execute()

    client.table("categories").delete().eq("id", category_id).execute()

    log_audit(
        user_id=user_id,
        action_type="category_deleted",
        entity_type="category",
        entity_id=category_id,
        description=(
            f"Category '{existing[0]['name']}' deleted. "
            f"{tx_count} transactions {'reassigned to ' + reassign_to if reassign_to else 'uncategorized'}."
        ),
        ip_address=request.remote_addr,
    )
    return jsonify({
        "ok": True,
        "affected_transactions": tx_count,
        "reassigned_to": reassign_to,
    })


@bp.get("/<category_id>/transactions-count")
@require_auth
def category_tx_count(category_id):
    """How many transactions reference this category (used before showing delete confirmation)."""
    client = get_admin_client()
    result = (
        client.table("transactions")
        .select("id", count="exact")
        .eq("user_id", g.user_id)
        .eq("category_id", category_id)
        .execute()
    )
    return jsonify({"count": result.count or 0})
