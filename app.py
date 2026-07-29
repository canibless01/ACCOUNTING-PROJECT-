"""
Holy Grills Bookkeeping Platform — Flask Application Entry Point.

Run locally:
  pip install -r requirements.txt
  gunicorn app:app -w 1 -b 0.0.0.0:8000 --timeout 60

Production (Gunicorn):
  gunicorn app:app -w 2 -b 0.0.0.0:$PORT
"""
from __future__ import annotations

import logging
import os

from flask import Flask, jsonify, request
from flask_cors import CORS

from config import Config


def create_app() -> Flask:
    app = Flask(__name__)
    app.secret_key = Config.SECRET_KEY

    # ── CORS ───────────────────────────────────────────────────────────────────
    CORS(
        app,
        origins="*",
        supports_credentials=True,
    )

    # ── Logging ────────────────────────────────────────────────────────────────
    logging.basicConfig(
        level=logging.DEBUG if Config.DEBUG else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # ── Register blueprints ────────────────────────────────────────────────────
    from routes.overview import bp as overview_bp
    from routes.accounts import bp as accounts_bp
    from routes.transactions import bp as transactions_bp
    from routes.categories import bp as categories_bp
    from routes.review import bp as review_bp
    from routes.reports import bp as reports_bp
    from routes.settings import bp as settings_bp
    from routes.sync import bp as sync_bp
    from routes.gmail_connections import bp as gmail_connections_bp

    app.register_blueprint(overview_bp)
    app.register_blueprint(accounts_bp)
    app.register_blueprint(transactions_bp)
    app.register_blueprint(categories_bp)
    app.register_blueprint(review_bp)
    app.register_blueprint(reports_bp)
    app.register_blueprint(settings_bp)
    app.register_blueprint(sync_bp)
    app.register_blueprint(gmail_connections_bp)

    # ── Legacy alias: GMAIL_REDIRECT_URI on Render is /api/auth/gmail/callback ─
    # The real route lives at /api/sync/gmail/callback.  Register an alias so
    # both paths work — whichever URI is registered in Google Cloud Console.
    @app.get("/api/auth/gmail/callback")
    def auth_gmail_callback_alias():
        from routes.sync import gmail_oauth_callback
        return gmail_oauth_callback()

    # ── API Root / Welcome ────────────────────────────────────────────────────
    @app.get("/")
    @app.get("/api")
    def api_root():
        return jsonify({
            "service": "Holy Grills Bookkeeping API",
            "version": "v2",
            "status": "live",
            "health": "/api/healthz",
        })

    # ── Health check ───────────────────────────────────────────────────────────
    @app.get("/api/healthz")
    def health():
        return jsonify({"status": "ok", "service": "holy-grills-api"})

    # ── List all routes (debug) ──────────────────────────────────────────────
    @app.get("/api/routes")
    def list_routes():
        routes = []
        for rule in app.url_map.iter_rules():
            if rule.endpoint == 'static':
                continue
            routes.append({
                "endpoint": rule.endpoint,
                "methods": sorted([m for m in rule.methods if m not in ('HEAD', 'OPTIONS')]),
                "path": str(rule)
            })
        return jsonify({
            "total_routes": len(routes),
            "routes": sorted(routes, key=lambda x: x['path'])
        })

    # ── Onboarding status ──────────────────────────────────────────────────────
    @app.get("/api/onboarding")
    def onboarding_status():
        """
        Returns the current user's onboarding progress.
        Frontend uses is_onboarded to decide whether to show the dashboard or the onboarding flow.
        """
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        token = auth_header.split(" ", 1)[1]
        try:
            from auth_middleware import _verify_jwt
            payload = _verify_jwt(token)
        except Exception:
            return jsonify({"error": "Invalid token"}), 401

        user_id = payload.get("sub")
        from db import get_admin_client
        client = get_admin_client()

        profile = (
            client.table("profiles").select("onboarding_complete,onboarding_step")
            .eq("id", user_id).limit(1).execute()
        ).data
        profile = profile[0] if profile else {}

        accounts = (
            client.table("accounts").select("id,name,is_gmail_connected,first_sync_done,gmail_connection_id")
            .eq("user_id", user_id).eq("is_active", True).execute()
        ).data or []

        # Check v2 connections
        connections = (
            client.table("gmail_connections").select("id,email_address,is_active")
            .eq("user_id", user_id).eq("is_active", True).execute()
        ).data or []

        connected_accounts = [a for a in accounts if a.get("is_gmail_connected") or a.get("gmail_connection_id")]
        complete = profile.get("onboarding_complete", False) or len(accounts) > 0

        return jsonify({
            # Primary field the frontend uses
            "is_onboarded": complete,
            # Additional context
            "onboarding_complete": complete,
            "current_step": profile.get("onboarding_step", "connect_gmail"),
            "accounts": accounts,
            "connected_accounts_count": len(connected_accounts),
            "gmail_connections": connections,
        })

    # ── Complete onboarding ────────────────────────────────────────────────────
    @app.post("/api/onboarding/complete")
    def complete_onboarding():
        """Mark the user's onboarding as complete in the profiles table."""
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401

        token = auth_header.split(" ", 1)[1]
        try:
            from auth_middleware import _verify_jwt
            payload = _verify_jwt(token)
        except Exception:
            return jsonify({"error": "Invalid token"}), 401

        user_id = payload.get("sub")
        from db import get_admin_client
        client = get_admin_client()

        # Upsert so this works whether or not a profiles row already exists
        client.table("profiles").upsert({
            "id": user_id,
            "onboarding_complete": True,
            "onboarding_step": "done",
        }, on_conflict="id").execute()

        return jsonify({"ok": True, "is_onboarded": True})

    # ── Error handlers ─────────────────────────────────────────────────────────
    @app.errorhandler(400)
    def bad_request(e):
        return jsonify({"error": "Bad request", "detail": str(e)}), 400

    @app.errorhandler(401)
    def unauthorized(e):
        return jsonify({"error": "Unauthorized — please log in"}), 401

    @app.errorhandler(403)
    def forbidden(e):
        return jsonify({"error": "Forbidden"}), 403

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Resource not found"}), 404

    @app.errorhandler(405)
    def method_not_allowed(e):
        return jsonify({"error": "Method not allowed", "allowed": list(e.valid_methods or [])}), 405

    @app.errorhandler(409)
    def conflict(e):
        return jsonify({"error": "Conflict", "detail": str(e)}), 409

    @app.errorhandler(500)
    def server_error(e):
        logging.exception("Unhandled server error")
        return jsonify({"error": "Internal server error — please try again later"}), 500

    @app.errorhandler(Exception)
    def handle_exception(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        import traceback
        logging.error(f"Unhandled exception: {traceback.format_exc()}")
        if Config.DEBUG:
            return jsonify({"error": str(e), "traceback": traceback.format_exc()}), 500
        return jsonify({"error": "An unexpected error occurred"}), 500

    return app


app = create_app()

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=Config.PORT,
        debug=Config.DEBUG,
    )
