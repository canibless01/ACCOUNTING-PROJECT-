"""
Supabase client helpers.
Uses the service-role key for backend operations (bypasses RLS where needed).
Uses the anon key + user JWT for user-scoped operations (respects RLS).
"""
from __future__ import annotations

from functools import lru_cache
from typing import Any

from supabase import Client, create_client

from config import Config


@lru_cache(maxsize=1)
def get_admin_client() -> Client:
    """Service-role Supabase client — bypasses RLS. Use for backend sync jobs."""
    return create_client(Config.SUPABASE_URL, Config.SUPABASE_SERVICE_ROLE_KEY)


@lru_cache(maxsize=1)
def get_anon_client() -> Client:
    """Anon Supabase client — obeys RLS. Use as a base for user-scoped clients."""
    return create_client(Config.SUPABASE_URL, Config.SUPABASE_ANON_KEY)


def get_user_client(access_token: str) -> Client:
    """Return a Supabase client scoped to the authenticated user's JWT."""
    client = create_client(Config.SUPABASE_URL, Config.SUPABASE_ANON_KEY)
    client.auth.set_session(access_token, "")
    return client


# ── Convenience wrappers ──────────────────────────────────────────────────────

def db_select(table: str, filters: dict[str, Any] | None = None, columns: str = "*") -> list[dict]:
    """Simple select from a table using the service-role client."""
    client = get_admin_client()
    q = client.table(table).select(columns)
    if filters:
        for k, v in filters.items():
            q = q.eq(k, v)
    return q.execute().data or []


def db_insert(table: str, data: dict | list[dict]) -> list[dict]:
    """Insert one or many rows."""
    client = get_admin_client()
    return client.table(table).insert(data).execute().data or []


def db_update(table: str, data: dict, filters: dict[str, Any]) -> list[dict]:
    """Update rows matching filters."""
    client = get_admin_client()
    q = client.table(table).update(data)
    for k, v in filters.items():
        q = q.eq(k, v)
    return q.execute().data or []


def db_delete(table: str, filters: dict[str, Any]) -> list[dict]:
    """Delete rows matching filters."""
    client = get_admin_client()
    q = client.table(table).delete()
    for k, v in filters.items():
        q = q.eq(k, v)
    return q.execute().data or []


def db_rpc(fn_name: str, params: dict[str, Any] | None = None) -> Any:
    """Call a Postgres function via PostgREST RPC."""
    client = get_admin_client()
    return client.rpc(fn_name, params or {}).execute().data
