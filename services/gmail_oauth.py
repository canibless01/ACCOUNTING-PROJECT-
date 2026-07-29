"""
Gmail OAuth integration service.
Handles:
  - OAuth flow initiation & callback (v1: per-account, v2: per-connection)
  - Encrypted token storage & retrieval
  - Automatic access token refresh
  - Token revocation (disconnect)
  - Building an authorised Gmail API service object
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

import requests
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build

from config import Config
from db import db_select, db_update, get_admin_client
from encryption import decrypt_token, encrypt_token


# ── OAuth flow helpers ─────────────────────────────────────────────────────────

def build_oauth_flow(state: Optional[str] = None) -> Flow:
    """Construct a google_auth_oauthlib Flow from config."""
    client_config = {
        "web": {
            "client_id": Config.GOOGLE_CLIENT_ID,
            "client_secret": Config.GOOGLE_CLIENT_SECRET,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [Config.GMAIL_REDIRECT_URI],
        }
    }
    flow = Flow.from_client_config(
        client_config,
        scopes=Config.GMAIL_SCOPES,
        redirect_uri=Config.GMAIL_REDIRECT_URI,
        state=state,
    )
    return flow


# ── V2: Connection-level OAuth (one Gmail inbox → many accounts) ───────────────

def get_authorization_url_for_user(user_id: str, frontend_url: str = "") -> tuple[str, str]:
    """
    Generate the Google OAuth authorization URL for a user (v2 connection model).
    Returns (url, state) — state encodes user_id and the initiating frontend URL
    so the callback always redirects back to the right place regardless of
    the FRONTEND_URL server env var.
    """
    import base64
    flow = build_oauth_flow()
    # Encode frontend_url in state so it survives the round-trip through Google
    fe_b64 = base64.urlsafe_b64encode((frontend_url or "").encode()).decode().rstrip("=")
    state_payload = f"user:{user_id}|fe:{fe_b64}"
    auth_url, state = flow.authorization_url(
        access_type="offline",
        # NOTE: Do NOT pass include_granted_scopes="true" here.
        # That flag merges any previously granted scopes (e.g. gmail.metadata
        # from an older authorisation) into the new token.  When gmail.metadata
        # is bundled in, Google still issues a metadata-only token even though
        # gmail.readonly was explicitly requested, which causes the sync engine
        # to receive a 403 "Metadata scope" error and mark the connection
        # inactive — trapping the user in a reconnect loop.
        # Using prompt="consent" alone is sufficient to force a fresh grant.
        prompt="consent",
        state=state_payload,
    )
    return auth_url, state


def _parse_v2_state(state: str) -> tuple[str, str]:
    """
    Parse a v2 OAuth state string into (user_id, frontend_url).
    State format: "user:{user_id}|fe:{base64_frontend_url}"
    Old format (no |fe: part): "user:{user_id}" — returns empty string for frontend_url.
    """
    import base64
    from config import Config
    parts = state.split("|")
    user_id = parts[0].removeprefix("user:")
    frontend_url = Config.FRONTEND_URL  # fallback to env var
    for part in parts[1:]:
        if part.startswith("fe:"):
            encoded = part[3:]
            # Re-add base64 padding
            padding = (4 - len(encoded) % 4) % 4
            try:
                decoded = base64.urlsafe_b64decode(encoded + "=" * padding).decode()
                if decoded:
                    frontend_url = decoded
            except Exception:
                pass
    return user_id, frontend_url


def exchange_code_for_tokens_v2(code: str, state: str) -> tuple[dict, str]:
    """
    Exchange the OAuth code for credentials (v2 — user-scoped state).
    Returns (token_dict, user_id).
    """
    import os
    # Google often returns additional scopes (openid, email, profile) alongside
    # the Gmail scopes we requested.  Without this flag, requests-oauthlib raises
    # ScopeChanged and aborts the token exchange.
    os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

    user_id, _frontend_url = _parse_v2_state(state)
    flow = build_oauth_flow(state=state)
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_dict = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
    }
    return token_dict, user_id


def store_tokens_for_connection(user_id: str, token_dict: dict, email_address: Optional[str] = None) -> dict:
    """
    Upsert a gmail_connections row for this user+email combo.
    Returns the connection row.

    Lookup order:
      1. Exact user+email match (most precise — used when profile fetch succeeds).
      2. Any existing connection for this user (fallback when profile fetch fails so
         we always UPDATE instead of INSERT, preventing orphan connections that leave
         all accounts pointing at a stale inactive connection_id).
    """
    client = get_admin_client()
    encrypted = encrypt_token(token_dict)
    expires_at = token_dict.get("token_expiry")

    existing = None

    # Primary lookup: user + exact email address
    if email_address:
        rows = (
            client.table("gmail_connections")
            .select("id")
            .eq("user_id", user_id)
            .eq("email_address", email_address)
            .limit(1)
            .execute()
        ).data
        if rows:
            existing = rows[0]

    # Fallback lookup: any connection for this user (handles profile-fetch failure
    # during reconnect — ensures we update-in-place rather than create a new orphan)
    if not existing:
        rows = (
            client.table("gmail_connections")
            .select("id")
            .eq("user_id", user_id)
            .order("connected_at", desc=True)
            .limit(1)
            .execute()
        ).data
        if rows:
            existing = rows[0]

    row_data = {
        "gmail_token_encrypted": encrypted,
        "gmail_token_expires_at": expires_at,
        "gmail_scopes": token_dict.get("scopes") or Config.GMAIL_SCOPES,
        "is_active": True,
    }
    if email_address:
        row_data["email_address"] = email_address

    if existing:
        result = (
            client.table("gmail_connections")
            .update(row_data)
            .eq("id", existing["id"])
            .execute()
        )
        active_id = existing["id"]
        conn_row = result.data[0] if result.data else existing
    else:
        row_data["user_id"] = user_id
        result = client.table("gmail_connections").insert(row_data).execute()
        conn_row = result.data[0] if result.data else {}
        active_id = conn_row.get("id")

    # Clean up orphan connections — inactive rows with no email_address that
    # were created by previous buggy reconnect attempts (profile fetch failed →
    # INSERT instead of UPDATE → stale rows accumulate and show as "Needs
    # reconnect" cards in the UI on every page load).
    if active_id:
        try:
            client.table("gmail_connections").delete().eq(
                "user_id", user_id
            ).eq("is_active", False).is_("email_address", "null").neq(
                "id", active_id
            ).execute()
        except Exception as _e:
            logger.warning("Could not clean up orphan connections: %s", _e)

    return conn_row


def load_credentials_for_connection(connection_id: str) -> Optional[Credentials]:
    """
    Load and (if needed) refresh Google credentials for a gmail_connection row.
    Returns None if no connected credentials found.
    """
    client = get_admin_client()
    rows = (
        client.table("gmail_connections")
        .select("id,gmail_token_encrypted,is_active")
        .eq("id", connection_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return None
    conn = rows[0]
    if not conn.get("is_active") or not conn.get("gmail_token_encrypted"):
        return None

    try:
        token_dict = decrypt_token(conn["gmail_token_encrypted"])
    except Exception:
        return None

    # NOTE: We intentionally do NOT pre-emptively check stored scopes here.
    #
    # The only authoritative signal that a Gmail connection was granted with
    # the wrong scope is an HTTP 403 "Metadata scope" error returned by the
    # Gmail API itself during a sync — that case is already handled in
    # sync_engine._fetch_gmail_messages and marks the connection inactive with a
    # clear reconnect message.
    #
    # Pre-emptively checking stored_scopes here causes a serious infinite
    # reconnect loop:
    #   • Google's token response may omit the `scope` field (RFC 6749 §5.1,
    #     "OPTIONAL if identical to the scope requested"), so stored_scopes can
    #     legitimately be empty even for a valid gmail.readonly token.
    #   • google-auth-oauthlib sometimes stores only the *requested* scopes
    #     (not what Google granted), which can look incomplete.
    # Either case causes a false positive → connection marked inactive → user
    # reconnects → same false positive → infinite loop.
    #
    # The Gmail API's own 403 response is the correct signal. Trust it.

    expiry = None
    if token_dict.get("token_expiry"):
        from datetime import datetime
        try:
            expiry = datetime.fromisoformat(token_dict["token_expiry"])
        except (ValueError, TypeError):
            pass

    creds = Credentials(
        token=token_dict.get("access_token"),
        refresh_token=token_dict.get("refresh_token"),
        token_uri=token_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_dict.get("client_id", Config.GOOGLE_CLIENT_ID),
        client_secret=token_dict.get("client_secret", Config.GOOGLE_CLIENT_SECRET),
        scopes=token_dict.get("scopes", Config.GMAIL_SCOPES),
        expiry=expiry,
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                refreshed = {
                    "access_token": creds.token,
                    "refresh_token": creds.refresh_token,
                    "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
                    "token_uri": creds.token_uri,
                    "client_id": creds.client_id,
                    "client_secret": creds.client_secret,
                    "scopes": list(creds.scopes or []),
                }
                encrypted = encrypt_token(refreshed)
                client.table("gmail_connections").update({
                    "gmail_token_encrypted": encrypted,
                    "gmail_token_expires_at": refreshed["token_expiry"],
                }).eq("id", connection_id).execute()
            except Exception as exc:
                err_lower = str(exc).lower()
                # Only mark inactive for permanent Google-side revocations.
                # "invalid_grant" means the user revoked access or the refresh
                # token expired (> 6 months unused).  Everything else
                # (network error, rate limit, transient 503) is temporary — do
                # NOT mark inactive or the user lands in an infinite reconnect
                # loop every time there is a brief network hiccup.
                is_permanent = any(
                    s in err_lower
                    for s in ("invalid_grant", "token_revoked", "token has been expired",
                               "unauthorized_client", "account_disabled")
                )
                if is_permanent:
                    logger.warning(
                        "Connection %s permanently revoked by Google (%s). Marking inactive.",
                        connection_id, exc,
                    )
                    client.table("gmail_connections").update(
                        {"is_active": False}
                    ).eq("id", connection_id).execute()
                else:
                    logger.warning(
                        "Connection %s token refresh failed (transient): %s. "
                        "Will retry on next sync.",
                        connection_id, exc,
                    )
                return None
        else:
            # No refresh token — credentials incomplete.
            # Return None but do NOT mark inactive; the user may still be
            # connected and this could be a decrypt/format edge case.
            logger.warning(
                "Connection %s: credentials invalid with no refresh token. "
                "Returning None without marking inactive.",
                connection_id,
            )
            return None

    return creds


def build_gmail_service_for_connection(connection_id: str):
    """Return an authenticated Gmail API service object from a gmail_connection, or None."""
    creds = load_credentials_for_connection(connection_id)
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


def revoke_connection(connection_id: str, user_id: str) -> bool:
    """
    Revoke Google OAuth access for a gmail_connection and mark it inactive.
    Returns True on success.
    """
    client = get_admin_client()
    rows = (
        client.table("gmail_connections")
        .select("id,gmail_token_encrypted,is_active")
        .eq("id", connection_id)
        .eq("user_id", user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return False

    try:
        token_dict = decrypt_token(rows[0]["gmail_token_encrypted"])
        access_token = token_dict.get("access_token")
        if access_token:
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": access_token},
                headers={"Content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
    except Exception:
        pass

    client.table("gmail_connections").update({
        "gmail_token_encrypted": None,
        "is_active": False,
        "gmail_scopes": [],
    }).eq("id", connection_id).execute()

    # Unlink any accounts that referenced this connection
    client.table("accounts").update({"gmail_connection_id": None}).eq("gmail_connection_id", connection_id).execute()

    return True


def get_gmail_profile_for_connection(connection_id: str) -> Optional[dict]:
    """Fetch the Gmail profile for a connection (returns emailAddress)."""
    service = build_gmail_service_for_connection(connection_id)
    if not service:
        return None
    try:
        return service.users().getProfile(userId="me").execute()
    except Exception:
        return None


# ── V1: Per-account OAuth (kept for backward compat) ──────────────────────────

def get_authorization_url(account_id: str) -> tuple[str, str]:
    """
    Generate the Google OAuth authorization URL (v1 — per-account).
    Returns (url, state) — state encodes account_id for the callback.
    """
    flow = build_oauth_flow()
    auth_url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",
        state=account_id,
    )
    return auth_url, state


def exchange_code_for_tokens(code: str, state: str) -> tuple[dict, str]:
    """
    Exchange the OAuth code for credentials (v1 — account-scoped state).
    Returns (token_dict, account_id).
    """
    account_id = state
    flow = build_oauth_flow(state=state)
    flow.fetch_token(code=code)
    creds = flow.credentials
    token_dict = {
        "access_token": creds.token,
        "refresh_token": creds.refresh_token,
        "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
        "token_uri": creds.token_uri,
        "client_id": creds.client_id,
        "client_secret": creds.client_secret,
        "scopes": list(creds.scopes or []),
    }
    return token_dict, account_id


def store_tokens(account_id: str, token_dict: dict) -> None:
    """Encrypt and persist tokens on the account row (v1)."""
    encrypted = encrypt_token(token_dict)
    expires_at = token_dict.get("token_expiry")
    db_update(
        "accounts",
        {
            "gmail_token_encrypted": encrypted,
            "gmail_token_expires_at": expires_at,
            "is_gmail_connected": True,
            "gmail_scopes": token_dict.get("scopes", Config.GMAIL_SCOPES),
        },
        {"id": account_id},
    )


def load_credentials(account_id: str) -> Optional[Credentials]:
    """
    Load and (if needed) refresh Google credentials for an account (v1).
    Returns None if the account has no connected Gmail.
    """
    rows = db_select("accounts", {"id": account_id}, "gmail_token_encrypted,is_gmail_connected")
    if not rows:
        return None
    account = rows[0]
    if not account.get("is_gmail_connected") or not account.get("gmail_token_encrypted"):
        return None

    try:
        token_dict = decrypt_token(account["gmail_token_encrypted"])
    except Exception:
        return None

    expiry = None
    if token_dict.get("token_expiry"):
        from datetime import datetime
        try:
            expiry = datetime.fromisoformat(token_dict["token_expiry"])
        except (ValueError, TypeError):
            pass

    creds = Credentials(
        token=token_dict.get("access_token"),
        refresh_token=token_dict.get("refresh_token"),
        token_uri=token_dict.get("token_uri", "https://oauth2.googleapis.com/token"),
        client_id=token_dict.get("client_id", Config.GOOGLE_CLIENT_ID),
        client_secret=token_dict.get("client_secret", Config.GOOGLE_CLIENT_SECRET),
        scopes=token_dict.get("scopes", Config.GMAIL_SCOPES),
        expiry=expiry,
    )

    if not creds.valid:
        if creds.expired and creds.refresh_token:
            creds.refresh(Request())
            refreshed = {
                "access_token": creds.token,
                "refresh_token": creds.refresh_token,
                "token_expiry": creds.expiry.isoformat() if creds.expiry else None,
                "token_uri": creds.token_uri,
                "client_id": creds.client_id,
                "client_secret": creds.client_secret,
                "scopes": list(creds.scopes or []),
            }
            store_tokens(account_id, refreshed)
        else:
            db_update("accounts", {"is_gmail_connected": False}, {"id": account_id})
            return None

    return creds


def build_gmail_service(account_id: str):
    """Return an authenticated Gmail API service object for an account (v1), or None."""
    creds = load_credentials(account_id)
    if not creds:
        return None
    return build("gmail", "v1", credentials=creds)


def revoke_tokens(account_id: str) -> bool:
    """
    Revoke Google OAuth access for an account (v1) and clear stored tokens.
    Returns True on success.
    """
    rows = db_select("accounts", {"id": account_id}, "gmail_token_encrypted,is_gmail_connected")
    if not rows or not rows[0].get("is_gmail_connected"):
        return True

    try:
        token_dict = decrypt_token(rows[0]["gmail_token_encrypted"])
        access_token = token_dict.get("access_token")
        if access_token:
            requests.post(
                "https://oauth2.googleapis.com/revoke",
                params={"token": access_token},
                headers={"Content-type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
    except Exception:
        pass

    db_update(
        "accounts",
        {
            "gmail_token_encrypted": None,
            "is_gmail_connected": False,
            "gmail_scopes": [],
        },
        {"id": account_id},
    )
    return True


def get_gmail_profile(account_id: str) -> Optional[dict]:
    """Fetch the Gmail profile for the connected account (v1, returns emailAddress)."""
    service = build_gmail_service(account_id)
    if not service:
        return None
    try:
        return service.users().getProfile(userId="me").execute()
    except Exception:
        return None
