"""
Sync and Gmail OAuth endpoints.
  POST /api/sync/trigger             — trigger sync for all accounts (or one)
  GET  /api/sync/gmail/initiate      — start OAuth flow (v2: user-level, no account_id)
  GET  /api/sync/gmail/callback      — handle OAuth callback from Google
  POST /api/sync/gmail/disconnect/<account_id> — revoke Gmail access (v1 per-account, kept for compat)
  GET  /api/sync/gmail/status/<account_id>     — connection health for an account
  GET  /api/sync/jobs                — list recent sync jobs
  POST /api/sync/reparse/<account_id>— re-parse failed imports
  POST /api/sync/housekeeping        — run purge + archive jobs (cron-callable)
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from flask import Blueprint, g, jsonify, redirect, request

from auth_middleware import optional_auth, require_auth
from config import Config
from db import db_select, db_update, get_admin_client
from services.audit import log_audit
from services.gmail_oauth import (
    _parse_v2_state,
    exchange_code_for_tokens,
    exchange_code_for_tokens_v2,
    get_authorization_url,
    get_authorization_url_for_user,
    get_gmail_profile,
    get_gmail_profile_for_connection,
    revoke_tokens,
    store_tokens,
    store_tokens_for_connection,
)
from services.housekeeping import run_all_housekeeping
from services.sync_engine import sync_account, sync_all_accounts

bp = Blueprint("sync", __name__, url_prefix="/api/sync")


@bp.post("/trigger")
@require_auth
def trigger_sync():
    """
    Trigger a sync.
    Body: {account_id?}  — if omitted, syncs all connected accounts.
    """
    user_id = g.user_id
    data = request.get_json(silent=True) or {}
    account_id = data.get("account_id")

    try:
        if account_id:
            # Verify ownership
            rows = db_select("accounts", {"id": account_id, "user_id": user_id}, "id,is_gmail_connected,gmail_connection_id")
            if not rows:
                return jsonify({"error": "Account not found"}), 404
            acc = rows[0]
            if not acc.get("is_gmail_connected") and not acc.get("gmail_connection_id"):
                return jsonify({"error": "Gmail not connected for this account"}), 400

            result = sync_account(account_id, user_id, triggered_by="manual")
        else:
            result = sync_all_accounts(user_id, triggered_by="manual")
    except Exception as exc:
        import logging
        logging.getLogger(__name__).exception("Sync trigger failed")
        return jsonify({"error": str(exc)}), 500

    if "error" in result:
        return jsonify({"error": result["error"]}), 500

    return jsonify({
        "ok": True,
        "sync_result": result,
        "synced_at": datetime.now(timezone.utc).isoformat(),
    })


@bp.post("/cron")
@optional_auth
def cron_trigger():
    """
    Cron job endpoint — called by an external scheduler (e.g. cron-job.org).
    Accepts an API key in the X-Cron-Key header for simple authentication
    (set CRON_SECRET in environment), or a valid Bearer token.
    """
    cron_key = request.headers.get("X-Cron-Key", "")
    bearer_uid = g.user_id  # set by optional_auth if valid JWT provided

    if not _valid_cron_auth(cron_key, bearer_uid):
        return jsonify({"error": "Unauthorized"}), 401

    client = get_admin_client()

    # Collect unique users from BOTH old-style connected accounts AND new-style gmail_connections
    seen_users = set()

    # V1: accounts with direct gmail tokens
    users_v1 = (
        client.table("accounts")
        .select("user_id")
        .eq("is_gmail_connected", True)
        .eq("is_active", True)
        .execute()
    ).data or []

    # V2: users with active gmail_connections
    users_v2 = (
        client.table("gmail_connections")
        .select("user_id")
        .eq("is_active", True)
        .execute()
    ).data or []

    for row in users_v1 + users_v2:
        seen_users.add(row["user_id"])

    results = {}
    for uid in seen_users:
        results[uid] = sync_all_accounts(uid, triggered_by="cron")

    housekeeping_result = run_all_housekeeping()

    return jsonify({
        "ok": True,
        "users_synced": len(seen_users),
        "results": results,
        "housekeeping": housekeeping_result,
        "ran_at": datetime.now(timezone.utc).isoformat(),
    })


# ── Gmail OAuth flow (V2 — connection-level) ───────────────────────────────────

@bp.get("/gmail/initiate")
@require_auth
def initiate_gmail_oauth():
    """
    Begin the Gmail OAuth flow for the authenticated user (v2).
    No account_id required — connects a Gmail inbox for the user,
    which can then be linked to one or more accounts.
    Returns the Google authorization URL.

    Optional query param: redirect_url — the frontend origin to redirect back
    to after OAuth completes.  If omitted, falls back to Config.FRONTEND_URL.
    Encoding it here lets the callback redirect correctly even when FRONTEND_URL
    is misconfigured on the server.
    """
    redirect_url = request.args.get("redirect_url", "") or Config.FRONTEND_URL
    auth_url, state = get_authorization_url_for_user(g.user_id, redirect_url)
    return jsonify({"auth_url": auth_url, "state": state})


@bp.get("/gmail/callback")
def gmail_oauth_callback():
    """
    Handle the OAuth callback from Google.
    Google redirects here after the user grants permission.
    Supports both v2 (user:{user_id} state) and v1 (bare account_id state).
    """
    error = request.args.get("error")
    state = request.args.get("state", "")

    # Decode the frontend URL from state as early as possible so even error
    # redirects go to the correct frontend origin.
    if state.startswith("user:"):
        _uid_tmp, frontend_url = _parse_v2_state(state)
    else:
        frontend_url = Config.FRONTEND_URL

    if error:
        return redirect(f"{frontend_url}/settings?gmail_error={error}")

    code = request.args.get("code")

    if not code or not state:
        return redirect(f"{frontend_url}/settings?gmail_error=missing_params")

    try:
        # V2: state starts with "user:"
        if state.startswith("user:"):
            token_dict, user_id = exchange_code_for_tokens_v2(code, state)

            # Fetch Gmail profile to get the connected email address
            import tempfile
            from google.oauth2.credentials import Credentials
            from googleapiclient.discovery import build as gapi_build
            from encryption import encrypt_token
            gmail_email = None
            try:
                expiry = None
                if token_dict.get("token_expiry"):
                    from datetime import datetime
                    expiry = datetime.fromisoformat(token_dict["token_expiry"])
                tmp_creds = Credentials(
                    token=token_dict.get("access_token"),
                    refresh_token=token_dict.get("refresh_token"),
                    token_uri=token_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
                    client_id=token_dict.get("client_id"),
                    client_secret=token_dict.get("client_secret"),
                    scopes=token_dict.get("scopes"),
                    expiry=expiry,
                )
                service = gapi_build("gmail", "v1", credentials=tmp_creds)
                profile = service.users().getProfile(userId="me").execute()
                gmail_email = profile.get("emailAddress")
            except Exception:
                pass

            conn = store_tokens_for_connection(user_id, token_dict, gmail_email)
            connection_id = conn.get("id", "")

            log_audit(
                user_id=user_id,
                action_type="account_connected",
                entity_type="gmail_connection",
                entity_id=connection_id,
                description=f"Gmail inbox connected: {gmail_email or 'unknown'}",
                metadata={"gmail_email": gmail_email},
            )

            email_param = f"&gmail_email={gmail_email}" if gmail_email else ""
            return redirect(
                f"{frontend_url}/settings?tab=accounts&gmail_connected=1"
                f"&connection_id={connection_id}{email_param}"
            )

        # V1: state is account_id (backward compat)
        else:
            token_dict, account_id = exchange_code_for_tokens(code, state)
            store_tokens(account_id, token_dict)

            profile = get_gmail_profile(account_id)
            gmail_address = profile.get("emailAddress") if profile else None
            if gmail_address:
                db_update("accounts", {"name": f"Gmail ({gmail_address})"}, {"id": account_id})

            rows = db_select("accounts", {"id": account_id}, "user_id,name")
            if rows:
                log_audit(
                    user_id=rows[0]["user_id"],
                    action_type="account_connected",
                    entity_type="account",
                    entity_id=account_id,
                    description=f"Gmail connected for account '{rows[0]['name']}'",
                    metadata={"gmail_email": gmail_address},
                )

            return redirect(
                f"{frontend_url}/settings?tab=accounts&gmail_connected=1&account_id={account_id}"
            )

    except Exception as exc:
        return redirect(
            f"{frontend_url}/settings?gmail_error={str(exc)[:100]}"
        )


# ── V1: Per-account disconnect (kept for backward compat) ──────────────────────

@bp.post("/gmail/disconnect/<account_id>")
@require_auth
def disconnect_gmail(account_id):
    """Revoke Gmail OAuth access for an account (v1)."""
    rows = db_select("accounts", {"id": account_id, "user_id": g.user_id}, "id,name")
    if not rows:
        return jsonify({"error": "Account not found"}), 404

    success = revoke_tokens(account_id)
    if success:
        log_audit(
            user_id=g.user_id,
            action_type="account_disconnected",
            entity_type="account",
            entity_id=account_id,
            description=f"Gmail disconnected for account '{rows[0]['name']}'",
            ip_address=request.remote_addr,
        )

    return jsonify({"ok": success, "account_id": account_id})


# ── Gmail connection status ────────────────────────────────────────────────────

@bp.get("/gmail/status/<account_id>")
@require_auth
def gmail_status(account_id):
    """
    Return Gmail connection status for an account.
    Checks both v1 (account-level token) and v2 (gmail_connection FK).
    """
    client = get_admin_client()
    rows = (
        client.table("accounts")
        .select(
            "id,name,is_gmail_connected,gmail_token_expires_at,"
            "last_sync_at,first_sync_done,sender_email,gmail_scopes,gmail_connection_id"
        )
        .eq("id", account_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Account not found"}), 404

    acc = rows[0]
    token_expires_at = acc.get("gmail_token_expires_at")
    token_expiring_soon = False

    # Check v2 connection if linked
    connection_info = None
    gmail_connection_id = acc.get("gmail_connection_id")
    if gmail_connection_id:
        conn_rows = (
            client.table("gmail_connections")
            .select("id,email_address,is_active,gmail_token_expires_at,connected_at")
            .eq("id", gmail_connection_id)
            .limit(1)
            .execute()
        ).data
        if conn_rows:
            connection_info = conn_rows[0]
            token_expires_at = connection_info.get("gmail_token_expires_at") or token_expires_at

    if token_expires_at:
        try:
            expires_dt = datetime.fromisoformat(token_expires_at.replace("Z", "+00:00"))
            diff = (expires_dt - datetime.now(timezone.utc)).total_seconds()
            token_expiring_soon = diff < 600
        except Exception:
            pass

    is_connected = (
        acc.get("is_gmail_connected", False)
        or (connection_info is not None and connection_info.get("is_active", False))
    )

    return jsonify({
        "account_id": account_id,
        "account_name": acc.get("name"),
        "is_connected": is_connected,
        "sender_email": acc.get("sender_email"),
        "gmail_scopes": acc.get("gmail_scopes", []),
        "last_sync_at": acc.get("last_sync_at"),
        "first_sync_done": acc.get("first_sync_done", False),
        "token_expires_at": token_expires_at,
        "token_expiring_soon": token_expiring_soon,
        "gmail_connection_id": gmail_connection_id,
        "gmail_connection": connection_info,
    })


# ── Sync jobs history ──────────────────────────────────────────────────────────

@bp.get("/jobs")
@require_auth
def list_sync_jobs():
    """Return recent sync jobs. Query params: account_id, limit (max 100)"""
    user_id = g.user_id
    client = get_admin_client()
    limit = min(int(request.args.get("limit", 20)), 100)
    account_id = request.args.get("account_id")

    q = (
        client.table("sync_jobs")
        .select("*")
        .eq("user_id", user_id)
        .order("started_at", desc=True)
        .limit(limit)
    )
    if account_id:
        q = q.eq("account_id", account_id)

    return jsonify({"sync_jobs": q.execute().data or []})


@bp.get("/jobs/<job_id>")
@require_auth
def get_sync_job(job_id):
    client = get_admin_client()
    rows = (
        client.table("sync_jobs")
        .select("*")
        .eq("id", job_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Sync job not found"}), 404
    return jsonify({"sync_job": rows[0]})


# ── Re-parse failed imports ────────────────────────────────────────────────────

@bp.post("/reparse/<account_id>")
@require_auth
def reparse_failed_imports(account_id):
    """Re-run parser against all pending failed imports for an account."""
    user_id = g.user_id
    rows = db_select("accounts", {"id": account_id, "user_id": user_id}, "id,sender_email,is_gmail_connected")
    if not rows:
        return jsonify({"error": "Account not found"}), 404

    from parsers import get_parser
    from parsers.base import NonTransactionEmail, ParseError
    from services.transaction_writer import write_transaction

    account = rows[0]
    sender_email = account.get("sender_email", "")
    parser = get_parser(sender_email)
    if not parser:
        return jsonify({"error": f"No parser registered for sender: {sender_email}"}), 400

    client = get_admin_client()
    failed = (
        client.table("failed_imports")
        .select("id,raw_content,raw_subject,raw_from,raw_date,gmail_message_id")
        .eq("account_id", account_id)
        .eq("user_id", user_id)
        .eq("status", "pending")
        .execute()
    ).data or []

    converted = 0
    still_failing = 0

    for item in failed:
        raw_content = item.get("raw_content", "")
        subject = item.get("raw_subject", "")
        received_at = datetime.now(timezone.utc)

        try:
            parsed = parser.parse_email(subject, raw_content, "", received_at)
            row, outcome = write_transaction(
                user_id=user_id,
                account_id=account_id,
                sender_email=sender_email,
                parsed=parsed,
                gmail_message_id=item.get("gmail_message_id"),
            )
            if outcome == "inserted":
                client.table("failed_imports").update({
                    "status": "converted",
                    "converted_to_transaction_id": row["id"],
                }).eq("id", item["id"]).execute()
                converted += 1
            else:
                still_failing += 1
        except (NonTransactionEmail, ParseError, Exception):
            still_failing += 1

    log_audit(
        user_id=user_id,
        action_type="parsing_rule_updated",
        entity_type="account",
        entity_id=account_id,
        description=f"Re-parse: {converted} recovered, {still_failing} still failing",
        ip_address=request.remote_addr,
    )
    return jsonify({
        "account_id": account_id,
        "total_attempted": len(failed),
        "converted": converted,
        "still_failing": still_failing,
    })


# ── Housekeeping ───────────────────────────────────────────────────────────────

@bp.post("/housekeeping")
@optional_auth
def run_housekeeping():
    """Run purge + archive housekeeping jobs."""
    cron_key = request.headers.get("X-Cron-Key", "")
    if not _valid_cron_auth(cron_key, g.user_id):
        return jsonify({"error": "Unauthorized"}), 401
    result = run_all_housekeeping()
    return jsonify(result)


# ── Helpers ────────────────────────────────────────────────────────────────────

def _valid_cron_auth(cron_key: str, bearer_uid: Optional[str]) -> bool:
    import os
    expected = os.environ.get("CRON_SECRET", "")
    if expected and cron_key == expected:
        return True
    if bearer_uid:
        return True
    return False
