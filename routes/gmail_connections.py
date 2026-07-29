"""
Gmail connections endpoints.
Manages the gmail_connections table — one row per Gmail inbox a user has connected.
Multiple bank accounts can be linked to a single Gmail connection.

GET  /api/gmail-connections           — list all connections for the user
GET  /api/gmail-connections/<id>      — single connection detail + linked accounts
DELETE /api/gmail-connections/<id>    — revoke and remove a connection
PATCH /api/gmail-connections/<id>     — update (e.g. mark inactive)
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from auth_middleware import require_auth
from db import get_admin_client
from services.audit import log_audit
from services.gmail_oauth import revoke_connection

bp = Blueprint("gmail_connections", __name__, url_prefix="/api/gmail-connections")


@bp.get("")
@require_auth
def list_connections():
    """Return all Gmail connections for the authenticated user."""
    client = get_admin_client()
    connections = (
        client.table("gmail_connections")
        .select("id,email_address,connected_at,is_active,gmail_scopes,created_at")
        .eq("user_id", g.user_id)
        .order("connected_at", desc=True)
        .execute()
    ).data or []

    # Annotate each connection with how many accounts are linked to it
    for conn in connections:
        linked = (
            client.table("accounts")
            .select("id", count="exact")
            .eq("gmail_connection_id", conn["id"])
            .eq("is_active", True)
            .execute()
        )
        conn["linked_account_count"] = linked.count or 0

    # Filter out orphan inactive connections that have no email_address.
    # These are stale rows created by earlier buggy reconnect attempts where the
    # Google profile fetch failed → an INSERT was made instead of an UPDATE →
    # duplicate rows accumulated.  Showing them confuses users with permanent
    # "Needs reconnect" cards even after a successful reconnect.
    # Inactive connections WITH an email_address are still shown so the user
    # can manually disconnect them if needed.
    connections = [
        c for c in connections
        if c.get("is_active") or c.get("email_address")
    ]

    return jsonify({"gmail_connections": connections, "total": len(connections)})


@bp.get("/<connection_id>")
@require_auth
def get_connection(connection_id):
    """Return a single Gmail connection with its linked accounts."""
    client = get_admin_client()
    rows = (
        client.table("gmail_connections")
        .select("*")
        .eq("id", connection_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Gmail connection not found"}), 404

    conn = rows[0]

    # Get linked accounts
    accounts = (
        client.table("accounts")
        .select("id,name,sender_email,sender_label,last_sync_at,is_active")
        .eq("gmail_connection_id", connection_id)
        .execute()
    ).data or []

    conn["linked_accounts"] = accounts
    return jsonify({"gmail_connection": conn})


@bp.delete("/<connection_id>")
@require_auth
def disconnect_connection(connection_id):
    """Revoke OAuth access and remove a Gmail connection."""
    success = revoke_connection(connection_id, g.user_id)
    if not success:
        return jsonify({"error": "Connection not found"}), 404

    log_audit(
        user_id=g.user_id,
        action_type="account_disconnected",
        entity_type="gmail_connection",
        entity_id=connection_id,
        description="Gmail connection revoked and removed",
        ip_address=request.remote_addr,
    )
    return jsonify({"ok": True, "connection_id": connection_id})


@bp.patch("/<connection_id>")
@require_auth
def update_connection(connection_id):
    """Update a Gmail connection (e.g. pause/resume it)."""
    client = get_admin_client()
    rows = (
        client.table("gmail_connections")
        .select("id")
        .eq("id", connection_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Connection not found"}), 404

    data = request.get_json() or {}
    allowed = ["is_active"]
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    result = (
        client.table("gmail_connections")
        .update(updates)
        .eq("id", connection_id)
        .execute()
    )
    return jsonify({"gmail_connection": result.data[0] if result.data else {}})
