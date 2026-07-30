"""
Configuration — single source of truth for every runtime value.

ALL values come from environment variables (or a .env file in development).
Nothing is hardcoded here except safe, non-secret defaults for optional
tuning parameters (timeouts, limits, etc.).
"""
import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    # ── Supabase ──────────────────────────────────────────────────────────────
    SUPABASE_URL: str               = os.environ["SUPABASE_URL"]
    SUPABASE_SERVICE_ROLE_KEY: str  = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
    SUPABASE_ANON_KEY: str          = os.environ["SUPABASE_ANON_KEY"]
    SUPABASE_JWT_SECRET: str        = os.environ["SUPABASE_JWT_SECRET"]

    # ── Gmail OAuth ───────────────────────────────────────────────────────────
    GOOGLE_CLIENT_ID: str           = os.environ["GOOGLE_CLIENT_ID"]
    GOOGLE_CLIENT_SECRET: str       = os.environ["GOOGLE_CLIENT_SECRET"]

    # Set GMAIL_REDIRECT_URI explicitly in your environment.
    # Development: http://localhost:8000/api/sync/gmail/callback
    # Production:  https://your-api-domain.com/api/sync/gmail/callback
    GMAIL_REDIRECT_URI: str = os.environ.get(
        "GMAIL_REDIRECT_URI", "http://localhost:8000/api/sync/gmail/callback"
    )

    # gmail.readonly is sufficient for message search (q=), full-body fetch,
    # and profile lookup. gmail.metadata must NOT be included — when it is,
    # Google restricts messages.list to metadata-only and rejects the 'q'
    # query parameter with a 403, preventing any email from being fetched.
    GMAIL_SCOPES: list[str] = [
        "https://www.googleapis.com/auth/gmail.readonly",
    ]

    # ── Token encryption ──────────────────────────────────────────────────────
    TOKEN_ENCRYPTION_KEY: str       = os.environ["TOKEN_ENCRYPTION_KEY"]
    TOKEN_ENCRYPTION_KEY_VERSION: int = int(os.environ.get("TOKEN_ENCRYPTION_KEY_VERSION", "1"))

    # ── Flask ─────────────────────────────────────────────────────────────────
    SECRET_KEY: str                 = os.environ.get("FLASK_SECRET_KEY") or os.environ["SESSION_SECRET"]
    DEBUG: bool                     = os.environ.get("FLASK_DEBUG", "false").lower() == "true"
    PORT: int                       = int(os.environ.get("PORT", "8000"))

    # Set FRONTEND_URL explicitly in your environment.
    # Development: http://localhost:5000
    # Production:  https://accounting-project-alpha.vercel.app
    FRONTEND_URL: str = os.environ.get("FRONTEND_URL", "https://accounting-project-alpha.vercel.app")

    # ── Cron auth ─────────────────────────────────────────────────────────────
    CRON_SECRET: str                = os.environ["CRON_SECRET"]

    # ── Sync behaviour ────────────────────────────────────────────────────────
    BACKFILL_DAYS: int              = int(os.environ.get("BACKFILL_DAYS", "30"))
    GMAIL_MAX_RESULTS: int          = int(os.environ.get("GMAIL_MAX_RESULTS", "500"))
    SYNC_RETRY_DELAYS: list[int]    = [60, 300, 900, 3600]

    # ── Housekeeping ──────────────────────────────────────────────────────────
    FAILED_IMPORT_TTL_DAYS: int     = int(os.environ.get("FAILED_IMPORT_TTL_DAYS", "30"))
    TRANSACTION_ARCHIVE_YEARS: int  = int(os.environ.get("TRANSACTION_ARCHIVE_YEARS", "2"))

    # ── Export ────────────────────────────────────────────────────────────────
    EXPORT_TEMP_DIR: str            = os.environ.get("EXPORT_TEMP_DIR", "/tmp/holy_grills_exports")
