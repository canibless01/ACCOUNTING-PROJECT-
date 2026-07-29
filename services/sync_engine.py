"""
Core sync engine.
Orchestrates the end-to-end Gmail sync pipeline.

Two sync paths:
  V2 (new): per gmail_connection — fetches from one inbox, routes to accounts by sender_email
  V1 (legacy): per account — each account has its own Gmail token (backward compat)

sync_all_accounts() calls both paths and merges the results.
"""
from __future__ import annotations

import base64
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from googleapiclient.errors import HttpError

from config import Config
from db import db_select, db_update, get_admin_client
from parsers import get_parser
from parsers.base import NonTransactionEmail, ParseError
from services.audit import log_audit
from services.digest import compile_digest
from services.duplicate_check import is_gmail_id_already_failed
from services.transaction_writer import write_transaction

logger = logging.getLogger(__name__)


# ── Public API ─────────────────────────────────────────────────────────────────

def _auto_link_sender_accounts(user_id: str, client) -> int:
    """
    Auto-link sender accounts to the user's most-recently-connected active Gmail
    connection.  Handles three cases:

      1. Account has no gmail_connection_id (added before Gmail was connected).
      2. Account points to a now-INACTIVE connection (scope check fired, or user
         disconnected and reconnected, creating a new connection_id).
      3. Account already points to the active connection — leave it alone.

    Without case 2, a reconnect after scope failure leaves ALL accounts pointing
    at the old inactive connection_id.  The V2 sync path finds no accounts for
    the new connection and completes with 0 transactions every time.

    Returns the number of accounts that were linked or re-linked.
    """
    try:
        # Find the most recently connected ACTIVE Gmail connection for this user.
        connections = (
            client.table("gmail_connections")
            .select("id")
            .eq("user_id", user_id)
            .eq("is_active", True)
            .order("connected_at", desc=True)   # newest first
            .limit(1)
            .execute()
        ).data or []
        if not connections:
            return 0

        connection_id = connections[0]["id"]

        # Fetch ALL sender accounts for this user (we need to check connection status
        # for any that are linked to a different connection_id).
        all_sender_accounts = (
            client.table("accounts")
            .select("id,name,gmail_connection_id")
            .eq("user_id", user_id)
            .eq("is_active", True)
            .not_.is_("sender_email", "null")
            .execute()
        ).data or []

        if not all_sender_accounts:
            return 0

        # Collect unique non-active connection IDs so we can batch-check them.
        foreign_conn_ids = {
            a["gmail_connection_id"]
            for a in all_sender_accounts
            if a.get("gmail_connection_id") and a["gmail_connection_id"] != connection_id
        }

        # For each foreign connection, check whether it's still active.
        inactive_conn_ids: set = set()
        for fid in foreign_conn_ids:
            rows = (
                client.table("gmail_connections")
                .select("is_active")
                .eq("id", fid)
                .limit(1)
                .execute()
            ).data
            if not rows or not rows[0].get("is_active", False):
                inactive_conn_ids.add(fid)

        linked = 0
        for account in all_sender_accounts:
            acct_conn_id = account.get("gmail_connection_id")
            if acct_conn_id == connection_id:
                continue  # already on the active connection — nothing to do

            should_relink = (
                acct_conn_id is None                   # never linked
                or acct_conn_id in inactive_conn_ids   # linked to a now-inactive connection
            )
            if should_relink:
                client.table("accounts").update(
                    {"gmail_connection_id": connection_id}
                ).eq("id", account["id"]).execute()
                linked += 1

        if linked > 0:
            logger.info(
                "Auto-linked/re-linked %d sender account(s) to Gmail connection %s",
                linked, connection_id,
            )
        return linked
    except Exception as exc:
        logger.warning("Auto-link orphan senders failed (non-critical): %s", exc)
        return 0


def sync_all_accounts(user_id: str, triggered_by: str = "cron") -> dict:
    """
    Sync every connected account for a user.
    Runs V2 (connection-based) first, then V1 (per-account legacy) for any
    accounts that have their own tokens but no gmail_connection_id.
    """
    # Before syncing, ensure any sender accounts added before Gmail was connected
    # are linked to an active Gmail connection so the V2 path can find them.
    client = get_admin_client()
    _auto_link_sender_accounts(user_id, client)

    combined: dict = {
        "accounts_synced": 0,
        "transactions_inserted": 0,
        "duplicates_skipped": 0,
        "failed_imports_count": 0,
        "errors": [],
        "alert_messages": [],
    }

    # V2: connection-based sync
    v2_result = _sync_all_via_connections(user_id, triggered_by)
    _merge_results(combined, v2_result)

    # V1: legacy per-account sync for accounts NOT linked to a connection
    legacy_accounts = db_select(
        "accounts",
        {"user_id": user_id, "is_gmail_connected": True, "is_active": True},
    )
    # Skip accounts already handled via V2
    v2_synced_ids = set(v2_result.get("_synced_account_ids", []))
    for account in legacy_accounts:
        if account["id"] in v2_synced_ids:
            continue
        # Only sync if no gmail_connection_id (truly legacy)
        if account.get("gmail_connection_id"):
            continue
        result = sync_account(account["id"], user_id, triggered_by)
        if "error" in result:
            combined["errors"].append({"account": account["name"], "error": result["error"]})
        else:
            combined["accounts_synced"] += 1
            combined["transactions_inserted"] += result.get("transactions_inserted", 0)
            combined["duplicates_skipped"] += result.get("duplicates_skipped", 0)
            combined["failed_imports_count"] += result.get("failed_imports_count", 0)
            combined["alert_messages"].extend(result.get("alert_messages", []))

    compile_digest(user_id, combined.get("alert_messages", []))
    return combined


def sync_account(
    account_id: str,
    user_id: str,
    triggered_by: str = "cron",
    _service=None,  # optional pre-built Gmail service (V2 path passes this in)
) -> dict:
    """
    Run a full sync for one account.
    Returns a summary dict with counts and outcome.
    """
    client = get_admin_client()

    # ── Load account ───────────────────────────────────────────────────────────
    rows = db_select(
        "accounts",
        {"id": account_id},
        "id,user_id,name,sender_email,sender_label,last_sync_at,"
        "first_sync_done,is_gmail_connected,parser_version,gmail_connection_id",
    )
    if not rows:
        return {"error": "Account not found"}

    account = rows[0]

    # Check connectivity: v2 connection OR v1 own token
    gmail_connection_id = account.get("gmail_connection_id")
    if not account.get("is_gmail_connected") and not gmail_connection_id and _service is None:
        return {"error": "Gmail not connected for this account"}

    sender_email = account.get("sender_email", "")
    if not sender_email:
        return {"error": "No sender email configured for this account"}

    # ── Resolve parser ─────────────────────────────────────────────────────────
    parser = get_parser(sender_email)
    if parser is None:
        _fail_job_immediately(
            client, user_id, account_id,
            f"No parser registered for e-commerce sender: {sender_email}.",
        )
        return {"error": f"No parser for sender '{sender_email}'"}

    # ── Create sync job ────────────────────────────────────────────────────────
    job_row = client.table("sync_jobs").insert({
        "user_id": user_id,
        "account_id": account_id,
        "status": "running",
        "triggered_by": triggered_by,
    }).execute().data
    job_id = job_row[0]["id"] if job_row else None

    # ── Determine time window ──────────────────────────────────────────────────
    is_backfill = not account.get("first_sync_done")
    if is_backfill:
        since = datetime.now(timezone.utc) - timedelta(days=Config.BACKFILL_DAYS)
    else:
        last_sync = account.get("last_sync_at")
        if last_sync:
            since = datetime.fromisoformat(last_sync.replace("Z", "+00:00"))
        else:
            since = datetime.now(timezone.utc) - timedelta(days=7)

    # ── Build Gmail service ────────────────────────────────────────────────────
    service = _service  # may be pre-built by V2 path

    if service is None:
        if gmail_connection_id:
            from services.gmail_oauth import build_gmail_service_for_connection
            service = build_gmail_service_for_connection(gmail_connection_id)
        else:
            from services.gmail_oauth import build_gmail_service
            service = build_gmail_service(account_id)

    if service is None:
        _fail_job(client, job_id, "Could not build Gmail service — token may be expired")
        if not gmail_connection_id:
            db_update("accounts", {"is_gmail_connected": False}, {"id": account_id})
        return {"error": "Gmail authentication failed. Please reconnect."}

    # ── Fetch emails from Gmail ────────────────────────────────────────────────
    try:
        messages = _fetch_gmail_messages(service, sender_email, since)
    except HttpError as e:
        if e.resp.status == 429:
            _schedule_retry(client, job_id, account_id, user_id)
            return {"error": "Gmail rate limit hit — scheduled for retry"}
        # 403 with "Metadata scope" means the token was granted with the
        # restricted gmail.metadata scope instead of gmail.readonly.
        # Mark the connection inactive so the UI shows a reconnect prompt.
        if e.resp.status == 403 and "Metadata scope" in str(e):
            gmail_connection_id = account.get("gmail_connection_id")
            if gmail_connection_id:
                client.table("gmail_connections").update({
                    "is_active": False,
                }).eq("id", gmail_connection_id).execute()
            reconnect_msg = (
                "Gmail permission error: the connected inbox was authorised "
                "with a restricted scope. Please disconnect Gmail in Settings "
                "and reconnect to grant the required permissions."
            )
            _fail_job(client, job_id, reconnect_msg)
            return {"error": reconnect_msg, "needs_gmail_reconnect": True}
        _fail_job(client, job_id, str(e))
        return {"error": str(e)}

    # ── Parse and write each message ───────────────────────────────────────────
    stats = {
        "emails_fetched": len(messages),
        "transactions_inserted": 0,
        "duplicates_skipped": 0,
        "failed_imports_count": 0,
        "failure_counter": 0,
        "alert_messages": [],
    }

    for msg in messages:
        outcome = _process_message(
            client=client,
            service=service,
            msg=msg,
            account=account,
            user_id=user_id,
            parser=parser,
            stats=stats,
        )
        if outcome == "rate_limited":
            _schedule_retry(client, job_id, account_id, user_id)
            break

    # ── Alert if ≥3 parse failures ────────────────────────────────────────────
    if stats["failure_counter"] >= 3:
        alert = (
            f"⚠ {stats['failure_counter']} emails from "
            f"{account.get('sender_label', sender_email)} "
            f"could not be parsed — email format may have changed."
        )
        stats["alert_messages"].append(alert)
        logger.warning(alert)

    # ── Update account's last_sync_at and first_sync_done ─────────────────────
    now_iso = datetime.now(timezone.utc).isoformat()
    db_update(
        "accounts",
        {"last_sync_at": now_iso, "first_sync_done": True},
        {"id": account_id},
    )

    # ── Mark sync job as success ───────────────────────────────────────────────
    if job_id:
        client.table("sync_jobs").update({
            "status": "success",
            "emails_fetched": stats["emails_fetched"],
            "transactions_inserted": stats["transactions_inserted"],
            "duplicates_skipped": stats["duplicates_skipped"],
            "failed_imports_count": stats["failed_imports_count"],
            "failure_counter": stats["failure_counter"],
            "completed_at": now_iso,
            "sync_window_from": since.isoformat(),
            "sync_window_to": now_iso,
            "backfill_from": (since.date().isoformat() if is_backfill else None),
        }).eq("id", job_id).execute()

    # ── Refresh materialised view ──────────────────────────────────────────────
    try:
        client.rpc("refresh_account_summary", {}).execute()
    except Exception as _e:
        logger.warning(f"refresh_account_summary failed (non-critical): {_e}")

    # ── Audit log for manual syncs ─────────────────────────────────────────────
    if triggered_by == "manual":
        log_audit(
            user_id=user_id,
            action_type="manual_sync",
            entity_type="account",
            entity_id=account_id,
            description=(
                f"Manual sync: fetched {stats['emails_fetched']} emails, "
                f"inserted {stats['transactions_inserted']} transactions."
            ),
            metadata=stats,
        )

    return stats


# ── V2: Connection-based sync ──────────────────────────────────────────────────

def _sync_all_via_connections(user_id: str, triggered_by: str = "cron") -> dict:
    """
    New primary sync path: one Gmail fetch per connection,
    routes emails to the correct account by sender_email.
    """
    client = get_admin_client()

    try:
        connections = (
            client.table("gmail_connections")
            .select("id,email_address")
            .eq("user_id", user_id)
            .eq("is_active", True)
            .execute()
        ).data or []
    except Exception as exc:
        # gmail_connections table may not exist yet (v3 migration not applied).
        # Treat as no connections so the V1 legacy path can still run.
        logger.warning("gmail_connections query failed (schema not applied?): %s", exc)
        connections = []

    combined: dict = {
        "connections_synced": 0,
        "accounts_synced": 0,
        "transactions_inserted": 0,
        "duplicates_skipped": 0,
        "failed_imports_count": 0,
        "errors": [],
        "alert_messages": [],
        "_synced_account_ids": [],  # internal — used to avoid double-syncing in v1 path
    }

    for conn in connections:
        connection_id = conn["id"]

        # Get all accounts linked to this connection that have a sender_email
        accounts = (
            client.table("accounts")
            .select(
                "id,user_id,name,sender_email,sender_label,last_sync_at,"
                "first_sync_done,is_gmail_connected,parser_version,gmail_connection_id"
            )
            .eq("gmail_connection_id", connection_id)
            .eq("is_active", True)
            .execute()
        ).data or []

        if not accounts:
            continue

        # Build service once per connection
        from services.gmail_oauth import build_gmail_service_for_connection
        service = build_gmail_service_for_connection(connection_id)
        if not service:
            for acc in accounts:
                combined["errors"].append({
                    "account": acc["name"],
                    "error": f"Gmail auth failed for connection {conn.get('email_address', connection_id)}",
                })
            continue

        combined["connections_synced"] += 1

        for account in accounts:
            if not account.get("sender_email"):
                continue
            result = sync_account(
                account["id"], user_id, triggered_by, _service=service
            )
            combined["_synced_account_ids"].append(account["id"])
            if "error" in result:
                combined["errors"].append({"account": account["name"], "error": result["error"]})
            else:
                combined["accounts_synced"] += 1
                combined["transactions_inserted"] += result.get("transactions_inserted", 0)
                combined["duplicates_skipped"] += result.get("duplicates_skipped", 0)
                combined["failed_imports_count"] += result.get("failed_imports_count", 0)
                combined["alert_messages"].extend(result.get("alert_messages", []))

    return combined


def _merge_results(base: dict, other: dict) -> None:
    """Merge counts from one result dict into another in-place."""
    for key in ("accounts_synced", "transactions_inserted", "duplicates_skipped", "failed_imports_count"):
        base[key] = base.get(key, 0) + other.get(key, 0)
    base.setdefault("errors", []).extend(other.get("errors", []))
    base.setdefault("alert_messages", []).extend(other.get("alert_messages", []))


# ── Private helpers ────────────────────────────────────────────────────────────

def _fetch_gmail_messages(service, sender_email: str, since: datetime) -> list[dict]:
    """
    Query Gmail for messages from sender_email after `since`.
    Returns a list of message stub dicts (id, threadId).
    """
    since_unix = int(since.timestamp())
    query = f"from:{sender_email} after:{since_unix}"
    messages: list[dict] = []
    page_token = None

    while True:
        kwargs: dict = {
            "userId": "me",
            "q": query,
            "maxResults": Config.GMAIL_MAX_RESULTS,
        }
        if page_token:
            kwargs["pageToken"] = page_token

        response = _gmail_request_with_backoff(
            lambda: service.users().messages().list(**kwargs).execute()
        )
        batch = response.get("messages", [])
        messages.extend(batch)
        page_token = response.get("nextPageToken")
        if not page_token or len(messages) >= Config.GMAIL_MAX_RESULTS:
            break

    return messages


def _gmail_request_with_backoff(fn, max_retries: int = 4):
    """Execute a Gmail API call with exponential backoff on 429/500/503 errors."""
    delays = Config.SYNC_RETRY_DELAYS
    for attempt, delay in enumerate(delays + [None]):
        try:
            return fn()
        except HttpError as e:
            if e.resp.status in (429, 500, 503) and delay is not None:
                logger.warning(f"Gmail API error {e.resp.status} — retrying in {delay}s")
                time.sleep(delay)
            else:
                raise


def _fetch_message_content(service, msg_id: str) -> dict:
    return _gmail_request_with_backoff(
        lambda: service.users().messages().get(
            userId="me", id=msg_id, format="full"
        ).execute()
    )


def _decode_gmail_part(part: dict) -> str:
    data = part.get("body", {}).get("data", "")
    if data:
        return base64.urlsafe_b64decode(data + "==").decode("utf-8", errors="replace")
    return ""


def _extract_email_body(payload: dict) -> tuple[str, str]:
    plain, html = "", ""
    mime_type = payload.get("mimeType", "")

    if mime_type == "text/plain":
        plain = _decode_gmail_part(payload)
    elif mime_type == "text/html":
        html = _decode_gmail_part(payload)
    elif "parts" in payload:
        for part in payload["parts"]:
            p, h = _extract_email_body(part)
            plain = plain or p
            html = html or h

    return plain, html


def _get_header(headers: list[dict], name: str) -> str:
    for h in headers:
        if h.get("name", "").lower() == name.lower():
            return h.get("value", "")
    return ""


def _process_message(
    client,
    service,
    msg: dict,
    account: dict,
    user_id: str,
    parser,
    stats: dict,
) -> str:
    """
    Fetch, parse, and write one Gmail message.
    Returns: 'inserted', 'duplicate', 'skipped', 'failed', 'rate_limited'.
    """
    account_id = account["id"]
    sender_email = account.get("sender_email", "")
    msg_id = msg.get("id", "")

    try:
        full_msg = _fetch_message_content(service, msg_id)
    except HttpError as e:
        if e.resp.status == 429:
            return "rate_limited"
        stats["failure_counter"] += 1
        stats["failed_imports_count"] += 1
        _save_failed_import(
            client, user_id, account_id, sender_email, msg_id,
            "", "", "", f"Gmail fetch error: {e}",
        )
        return "failed"

    payload = full_msg.get("payload", {})
    headers = payload.get("headers", [])
    subject = _get_header(headers, "Subject")
    from_addr = _get_header(headers, "From")

    internal_date = full_msg.get("internalDate")
    received_at = (
        datetime.fromtimestamp(int(internal_date) / 1000, tz=timezone.utc)
        if internal_date
        else datetime.now(timezone.utc)
    )

    body_plain, body_html = _extract_email_body(payload)

    try:
        parsed = parser.parse_email(subject, body_plain, body_html, received_at)
    except NonTransactionEmail:
        return "skipped"
    except (ParseError, Exception) as exc:
        stats["failure_counter"] += 1
        if msg_id and is_gmail_id_already_failed(user_id, msg_id):
            return "failed"
        stats["failed_imports_count"] += 1
        _save_failed_import(
            client, user_id, account_id, sender_email, msg_id,
            body_plain or body_html, subject, from_addr, str(exc),
        )
        return "failed"

    try:
        row, outcome = write_transaction(
            user_id=user_id,
            account_id=account_id,
            sender_email=sender_email,
            parsed=parsed,
            source="email",
            gmail_message_id=msg_id,
        )
    except Exception as exc:
        # Unexpected write error (e.g. a DB error not caught by transaction_writer).
        # Save as a failed_import so the user can see it in Review Queue, then
        # continue processing the remaining messages rather than crashing the job.
        logger.exception("write_transaction raised unexpectedly for msg %s: %s", msg_id, exc)
        stats["failure_counter"] += 1
        stats["failed_imports_count"] += 1
        _save_failed_import(
            client, user_id, account_id, sender_email, msg_id,
            body_plain or body_html, subject, from_addr,
            f"Write error: {exc}",
        )
        return "failed"

    if outcome == "error":
        # write_transaction returned a soft error (e.g. numeric overflow after
        # the parser produced an unreasonably large amount).  Save as failed_import.
        stats["failure_counter"] += 1
        stats["failed_imports_count"] += 1
        _save_failed_import(
            client, user_id, account_id, sender_email, msg_id,
            body_plain or body_html, subject, from_addr,
            "Transaction write failed (amount may be out of range or data malformed)",
        )
        return "failed"

    if outcome == "inserted":
        stats["transactions_inserted"] += 1
    elif outcome == "duplicate":
        stats["duplicates_skipped"] += 1

    return outcome


def _save_failed_import(
    client,
    user_id: str,
    account_id: str,
    sender_email: str,
    gmail_message_id: str,
    raw_content: str,
    subject: str,
    from_addr: str,
    reason: str,
) -> None:
    try:
        client.table("failed_imports").insert({
            "user_id": user_id,
            "account_id": account_id,
            "sender_email": sender_email,
            "gmail_message_id": gmail_message_id or None,
            "raw_content": raw_content[:10000],
            "raw_subject": subject[:500] if subject else None,
            "raw_from": from_addr[:200] if from_addr else None,
            "failure_reason": reason[:1000] if reason else None,
        }).execute()
    except Exception as e:
        if "unique" not in str(e).lower():
            logger.error(f"Could not save failed import: {e}")


def _fail_job(client, job_id: Optional[str], error_message: str) -> None:
    if not job_id:
        return
    try:
        client.table("sync_jobs").update({
            "status": "failed",
            "error_message": error_message[:2000],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()
    except Exception:
        pass


def _fail_job_immediately(client, user_id: str, account_id: str, error_message: str) -> None:
    try:
        client.table("sync_jobs").insert({
            "user_id": user_id,
            "account_id": account_id,
            "status": "failed",
            "triggered_by": "system",
            "error_message": error_message[:2000],
            "completed_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception:
        pass


def _schedule_retry(client, job_id: Optional[str], account_id: str, user_id: str) -> None:
    if not job_id:
        return
    try:
        rows = client.table("sync_jobs").select("retry_count").eq("id", job_id).execute()
        retry_count = rows.data[0]["retry_count"] if rows.data else 0
        delays = Config.SYNC_RETRY_DELAYS
        delay_secs = delays[min(retry_count, len(delays) - 1)]
        next_retry = (
            datetime.now(timezone.utc) + timedelta(seconds=delay_secs)
        ).isoformat()
        client.table("sync_jobs").update({
            "status": "failed",
            "error_message": "Gmail rate limit — scheduled for retry",
            "next_retry_at": next_retry,
            "retry_count": retry_count + 1,
        }).eq("id", job_id).execute()
    except Exception:
        pass
