"""
Authentication middleware for the Flask app.
Verifies Supabase JWT tokens by calling the Supabase Auth user endpoint.
This works with any Supabase JWT algorithm (HS256, RS256, ES256) without
needing to know the signing key or fetch JWKS.
"""
from __future__ import annotations

from functools import wraps
from typing import Callable

import requests
from flask import g, jsonify, request

from config import Config

# In-process cache: token → (payload_dict, expires_at_unix)
# Avoids a round-trip to Supabase for every request in a burst.
_token_cache: dict[str, tuple[dict, float]] = {}
_CACHE_TTL = 60  # seconds — short enough to catch revocations


def _verify_jwt(token: str) -> dict:
    """
    Verify a Supabase JWT by calling /auth/v1/user with the Bearer token.
    Returns the user payload dict on success.
    Raises ValueError on failure (invalid/expired/revoked token).

    Results are cached for _CACHE_TTL seconds so we don't hit Supabase
    on every single request.
    """
    import time

    # Check in-process cache first
    cached = _token_cache.get(token)
    if cached:
        payload, expires_at = cached
        if time.time() < expires_at:
            return payload
        del _token_cache[token]

    # Ask Supabase — token is valid iff the server accepts it
    try:
        resp = requests.get(
            f"{Config.SUPABASE_URL}/auth/v1/user",
            headers={
                "apikey": Config.SUPABASE_ANON_KEY,
                "Authorization": f"Bearer {token}",
            },
            timeout=10,
        )
    except requests.RequestException as exc:
        raise ValueError(f"Supabase auth check failed: {exc}") from exc

    if resp.status_code != 200:
        raise ValueError(f"Token rejected by Supabase (status {resp.status_code})")

    user = resp.json()
    payload = {
        "sub":   user.get("id", ""),
        "email": user.get("email", ""),
        "role":  user.get("role", "authenticated"),
        "aud":   "authenticated",
        # Supabase user object doesn't include exp; we cache with our own TTL
    }

    # Cache the successful result
    _token_cache[token] = (payload, time.time() + _CACHE_TTL)
    return payload


def require_auth(f: Callable) -> Callable:
    """
    Decorator — verifies the Bearer token in the Authorization header.
    On success, sets g.user_id, g.user_email, g.jwt_payload, g.access_token.
    On failure, returns 401.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Missing or malformed Authorization header"}), 401

        token = auth_header.split(" ", 1)[1]
        try:
            payload = _verify_jwt(token)
        except Exception as exc:
            return jsonify({"error": "Invalid or expired token", "detail": str(exc)}), 401

        g.user_id = payload.get("sub")
        g.user_email = payload.get("email", "")
        g.jwt_payload = payload
        g.access_token = token

        return f(*args, **kwargs)
    return decorated


def optional_auth(f: Callable) -> Callable:
    """Like require_auth but does not reject unauthenticated requests."""
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        g.user_id = None
        g.user_email = None
        g.jwt_payload = {}
        g.access_token = None
        if auth_header.startswith("Bearer "):
            try:
                token = auth_header.split(" ", 1)[1]
                payload = _verify_jwt(token)
                g.user_id = payload.get("sub")
                g.user_email = payload.get("email", "")
                g.jwt_payload = payload
                g.access_token = token
            except Exception:
                pass
        return f(*args, **kwargs)
    return decorated
