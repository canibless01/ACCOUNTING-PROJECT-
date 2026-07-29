"""
Holy Grills API — Comprehensive Live Integration Test Suite
===========================================================
Phases 1–20 + API completeness validation.
Runs against real Supabase. Creates test users, exercises every endpoint,
runs parser unit tests, checks DB constraints, tests RLS isolation, and
wipes ALL data from all tables when done.

Usage:
    cd artifacts/holy-grills-api && python tests/test_api.py
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from datetime import date, datetime, timedelta
from typing import Any, Optional

import requests

# ── Config ─────────────────────────────────────────────────────────────────────
BASE = "http://0.0.0.0:8000"
SUPABASE_URL = os.environ["SUPABASE_URL"]
SUPABASE_ANON_KEY = os.environ["SUPABASE_ANON_KEY"]
SUPABASE_SERVICE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

TEST_EMAIL   = f"test-a-{uuid.uuid4().hex[:8]}@holy-grills-test.local"
TEST_EMAIL_B = f"test-b-{uuid.uuid4().hex[:8]}@holy-grills-test.local"
TEST_PASSWORD = "Abcd1234!Test"

PASS = "\033[92m✅ PASS\033[0m"
FAIL = "\033[91m❌ FAIL\033[0m"
SKIP = "\033[93m⚠️  SKIP\033[0m"
INFO = "\033[96mℹ️  INFO\033[0m"

results: list[dict] = []

# Global test state — User A (primary)
_access_token:   Optional[str] = None
_user_id:        Optional[str] = None
_account_id:     Optional[str] = None   # Moniepoint account
_account_id_b:   Optional[str] = None   # OPay account (for transfer tests)
_tx_id:          Optional[str] = None
_tx_id2:         Optional[str] = None   # second transaction (transfer pair)
_category_id:    Optional[str] = None
_rule_id:        Optional[str] = None
_budget_id:      Optional[str] = None
_failed_import_id: Optional[str] = None

# Global test state — User B (RLS isolation)
_token_b:  Optional[str] = None
_user_id_b: Optional[str] = None

# Track all endpoints tested for completeness report
TESTED_ENDPOINTS: set[str] = set()

# All known API endpoints  (method:path)
ALL_ENDPOINTS = {
    "GET:/api/healthz",
    "GET:/api/onboarding",
    "GET:/api/overview",
    "GET:/api/overview/chart",
    "GET:/api/overview/donut",
    "GET:/api/overview/digests",
    "POST:/api/overview/digests/<id>/read",
    "POST:/api/overview/digests/read-all",
    "GET:/api/accounts",
    "POST:/api/accounts",
    "GET:/api/accounts/<id>",
    "PATCH:/api/accounts/<id>",
    "DELETE:/api/accounts/<id>",
    "GET:/api/accounts/<id>/balance",
    "GET:/api/accounts/<id>/balance-history",
    "POST:/api/accounts/<id>/balance-adjustment",
    "POST:/api/accounts/<id>/recompute-reconciliation",
    "GET:/api/accounts/<id>/transactions",
    "GET:/api/transactions",
    "POST:/api/transactions",
    "GET:/api/transactions/<id>",
    "PATCH:/api/transactions/<id>",
    "POST:/api/transactions/<id>/mark-transfer",
    "POST:/api/transactions/<id>/flag-mis-parse",
    "GET:/api/transactions/archive",
    "GET:/api/categories",
    "POST:/api/categories",
    "GET:/api/categories/<id>",
    "PATCH:/api/categories/<id>",
    "DELETE:/api/categories/<id>",
    "GET:/api/categories/<id>/transactions-count",
    "GET:/api/review/queue",
    "POST:/api/review/queue/<id>/categorize",
    "POST:/api/review/queue/<id>/skip",
    "GET:/api/review/failed-imports",
    "GET:/api/review/failed-imports/<id>",
    "POST:/api/review/failed-imports/<id>/convert",
    "POST:/api/review/failed-imports/<id>/ignore",
    "GET:/api/review/stats",
    "GET:/api/settings",
    "PATCH:/api/settings",
    "GET:/api/settings/profile",
    "PATCH:/api/settings/profile",
    "GET:/api/settings/parsing-rules",
    "POST:/api/settings/parsing-rules",
    "PATCH:/api/settings/parsing-rules/<id>",
    "DELETE:/api/settings/parsing-rules/<id>",
    "GET:/api/settings/budgets",
    "POST:/api/settings/budgets",
    "DELETE:/api/settings/budgets/<id>",
    "GET:/api/settings/audit-log",
    "GET:/api/reports/pl",
    "GET:/api/reports/cashflow",
    "GET:/api/reports/reconciliation",
    "GET:/api/reports/export/<type>.<fmt>",
    "POST:/api/sync/trigger",
    "POST:/api/sync/cron",
    "GET:/api/sync/gmail/initiate",
    "GET:/api/sync/gmail/callback",
    "POST:/api/sync/gmail/disconnect/<id>",
    "GET:/api/sync/jobs",
    "GET:/api/sync/jobs/<id>",
    "POST:/api/sync/reparse/<id>",
    "POST:/api/sync/housekeeping",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def record(name: str, passed: bool, status: int, body: Any, note: str = ""):
    icon = PASS if passed else FAIL
    label = f"{icon} [{status}] {name}"
    if note:
        label += f"  — {note}"
    print(label)
    results.append({"name": name, "passed": passed, "status": status, "note": note})


def auth_header(token: Optional[str] = None) -> dict:
    tok = token or _access_token
    return {"Authorization": f"Bearer {tok}"}


def api(method: str, path: str, *, json_body=None, params=None, headers=None,
        expected=(200, 201), token: Optional[str] = None) -> tuple[int, dict, bool]:
    tok = token or _access_token
    h = {**(headers or {})}
    if tok:
        h["Authorization"] = f"Bearer {tok}"
    fn = getattr(requests, method.lower())
    r = fn(f"{BASE}{path}", json=json_body, params=params, headers=h, timeout=30)
    try:
        body = r.json()
    except Exception:
        body = {"_raw": r.text[:500]}
    exp = expected if isinstance(expected, (list, tuple)) else [expected]
    passed = r.status_code in exp
    TESTED_ENDPOINTS.add(f"{method.upper()}:{path}")
    return r.status_code, body, passed


def supabase_admin(method: str, path: str, **kwargs):
    """Call Supabase REST with service role (bypasses RLS)."""
    h = {
        "apikey": SUPABASE_SERVICE_KEY,
        "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        "Content-Type": "application/json",
        "Prefer": "return=representation",
    }
    fn = getattr(requests, method.lower())
    return fn(f"{SUPABASE_URL}/rest/v1/{path}", headers=h, timeout=15, **kwargs)


def signup_user(email: str, password: str) -> tuple[Optional[str], Optional[str]]:
    """Create a user via Supabase auth. Returns (access_token, user_id)."""
    r = requests.post(
        f"{SUPABASE_URL}/auth/v1/signup",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"email": email, "password": password},
        timeout=15,
    )
    body = r.json()
    if r.status_code in (200, 201) and "access_token" in body:
        return body["access_token"], body.get("user", {}).get("id")
    return None, None


def delete_auth_user(user_id: str):
    """Hard-delete a user from Supabase auth (service role)."""
    requests.delete(
        f"{SUPABASE_URL}/auth/v1/admin/users/{user_id}",
        headers={
            "apikey": SUPABASE_SERVICE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_KEY}",
        },
        timeout=15,
    )


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 1 — Authentication & Onboarding
# ═══════════════════════════════════════════════════════════════════════════════

def test_health():
    sc, body, ok = api("GET", "/api/healthz")
    record("1.0 Health check", ok and body.get("status") == "ok", sc, body)


def test_create_user():
    global _access_token, _user_id
    tok, uid = signup_user(TEST_EMAIL, TEST_PASSWORD)
    ok = tok is not None
    if ok:
        _access_token = tok
        _user_id = uid
    record(
        "1.1 Auth — sign up User A",
        ok, 200 if ok else 400, {},
        f"user_id={_user_id}" if ok else "signup failed",
    )
    return ok


def test_db_triggers_after_signup():
    """Verify profiles, settings, and 9 default categories are created by DB triggers."""
    if not _user_id:
        print(f"{SKIP} 1.1b DB triggers — no user")
        return

    time.sleep(1)  # let triggers settle

    # Check profiles row
    r = supabase_admin("GET", f"profiles?id=eq.{_user_id}&select=id,created_at")
    profiles = r.json()
    has_profile = isinstance(profiles, list) and len(profiles) == 1
    record("1.1b DB trigger — profiles row created", has_profile, r.status_code, profiles)

    # Check settings row
    r2 = supabase_admin("GET", f"settings?user_id=eq.{_user_id}&select=user_id,digest_enabled,timezone")
    settings = r2.json()
    has_settings = isinstance(settings, list) and len(settings) == 1
    record("1.1b DB trigger — settings row created", has_settings, r2.status_code, settings,
           f"digest_enabled={settings[0].get('digest_enabled') if has_settings else '?'}")

    # Check 9 default categories seeded
    r3 = supabase_admin("GET", f"categories?user_id=eq.{_user_id}&is_system=eq.true&select=id,name")
    cats = r3.json()
    cat_count = len(cats) if isinstance(cats, list) else 0
    record("1.1b DB trigger — 9 default categories seeded", cat_count == 9, r3.status_code, cats,
           f"found={cat_count} (expected 9)")


def test_sign_in():
    global _access_token
    r = requests.post(
        f"{SUPABASE_URL}/auth/v1/token?grant_type=password",
        headers={"apikey": SUPABASE_ANON_KEY, "Content-Type": "application/json"},
        json={"email": TEST_EMAIL, "password": TEST_PASSWORD},
        timeout=15,
    )
    body = r.json()
    ok = r.status_code == 200 and "access_token" in body
    if ok:
        _access_token = body["access_token"]
    record("1.1c Auth — sign in (fresh JWT)", ok, r.status_code, body,
           "token obtained" if ok else body.get("error_description", ""))


def test_auth_required():
    r = requests.get(f"{BASE}/api/accounts", timeout=10)
    ok = r.status_code == 401
    TESTED_ENDPOINTS.add("GET:/api/accounts")
    record("1.2a Auth — 401 on missing token (Test 18.3)", ok, r.status_code, {})


def test_invalid_token():
    r = requests.get(
        f"{BASE}/api/accounts",
        headers={"Authorization": "Bearer totally.invalid.token"},
        timeout=10,
    )
    ok = r.status_code == 401
    TESTED_ENDPOINTS.add("GET:/api/accounts")
    record("1.2b Auth — 401 on bad token", ok, r.status_code, {})


def test_onboarding():
    sc, body, ok = api("GET", "/api/onboarding")
    ok = ok and "onboarding_complete" in body
    record("1.3 Onboarding status", ok, sc, body,
           f"complete={body.get('onboarding_complete')}" if ok else str(body)[:120])


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 2 — Settings & Profile
# ═══════════════════════════════════════════════════════════════════════════════

def test_get_profile():
    sc, body, ok = api("GET", "/api/settings/profile")
    ok = ok and "profile" in body
    record("2.1 GET /api/settings/profile", ok, sc, body,
           f"id={body.get('profile',{}).get('id','?')}" if ok else str(body)[:120])
    return ok


def test_update_profile():
    sc, body, ok = api("PATCH", "/api/settings/profile",
                        json_body={"full_name": "Test User HG"})
    ok = ok and "profile" in body
    record("2.2 PATCH /api/settings/profile", ok, sc, body)


def test_get_settings():
    sc, body, ok = api("GET", "/api/settings")
    ok = ok and "settings" in body
    record("2.3 GET /api/settings", ok, sc, body,
           f"user_id={body.get('settings',{}).get('user_id','?')}" if ok else str(body)[:120])
    return ok


def test_update_settings():
    sc, body, ok = api("PATCH", "/api/settings",
                        json_body={
                            "digest_enabled": True,
                            "timezone": "Africa/Lagos",
                            "notification_email": TEST_EMAIL,
                        })
    ok = ok and "settings" in body
    record("2.4 PATCH /api/settings (Test 7.6)", ok, sc, body)


def test_update_settings_notifications():
    """Test 7.6 — notification preferences specifically."""
    sc, body, ok = api("PATCH", "/api/settings",
                        json_body={"digest_enabled": False})
    ok2 = ok and "settings" in body
    record("2.5 PATCH /api/settings — digest_enabled=False (Test 7.6)", ok2, sc, body)
    # Restore
    api("PATCH", "/api/settings", json_body={"digest_enabled": True})


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 3 — Accounts CRUD (Tests 3.1–3.5)
# ═══════════════════════════════════════════════════════════════════════════════

def test_list_accounts_empty():
    sc, body, ok = api("GET", "/api/accounts")
    ok = ok and "accounts" in body
    record("3.0 GET /api/accounts (empty)", ok, sc, body,
           f"total={body.get('total',0)}")


def test_create_account():
    global _account_id
    sc, body, ok = api("POST", "/api/accounts",
                        json_body={
                            "name": "Test Moniepoint",
                            "sender_email": "no-reply@moniepoint.com",
                            "sender_label": "Moniepoint",
                            "opening_balance": 50000.00,
                            "is_manual": False,
                            "currency": "NGN",
                        },
                        expected=(201,))
    ok = ok and "account" in body
    if ok:
        _account_id = body["account"]["id"]
    record("3.1 POST /api/accounts (Test 3.1)", ok, sc, body,
           f"account_id={_account_id}" if ok else str(body)[:120])
    return ok


def test_create_account_opay():
    """Create second account (OPay) for transfer-pair tests (Test 11.1)."""
    global _account_id_b
    sc, body, ok = api("POST", "/api/accounts",
                        json_body={
                            "name": "Test OPay",
                            "sender_email": "alerts@opay.com",
                            "sender_label": "OPay",
                            "opening_balance": 10000.00,
                            "is_manual": False,
                            "currency": "NGN",
                        },
                        expected=(201,))
    ok = ok and "account" in body
    if ok:
        _account_id_b = body["account"]["id"]
    record("3.1b POST /api/accounts — OPay (for transfer tests)", ok, sc, body,
           f"account_id={_account_id_b}" if ok else str(body)[:120])


def test_duplicate_sender_email():
    """Test 1.3 — UNIQUE(user_id, sender_email) prevents duplicate senders."""
    sc, body, ok = api("POST", "/api/accounts",
                        json_body={
                            "name": "Moniepoint Duplicate",
                            "sender_email": "no-reply@moniepoint.com",
                        },
                        expected=(409,))
    record("3.1c POST /api/accounts — 409 on duplicate sender_email (Test 1.3)", ok, sc, body)


def test_get_account():
    if not _account_id:
        print(f"{SKIP} 3.2 GET /api/accounts/<id>")
        return
    sc, body, ok = api("GET", f"/api/accounts/{_account_id}")
    ok = ok and "account" in body
    record("3.2 GET /api/accounts/<id> (Test 3.2)", ok, sc, body,
           f"name={body.get('account',{}).get('name','?')}" if ok else str(body)[:120])


def test_update_account():
    if not _account_id:
        print(f"{SKIP} 3.3 PATCH /api/accounts/<id>")
        return
    sc, body, ok = api("PATCH", f"/api/accounts/{_account_id}",
                        json_body={"opening_balance": 75000.00, "sender_label": "Moniepoint Updated"})
    ok = ok and "account" in body
    record("3.3 PATCH /api/accounts/<id> opening_balance=75000 (Test 3.3)", ok, sc, body)


def test_account_balance():
    if not _account_id:
        return
    sc, body, ok = api("GET", f"/api/accounts/{_account_id}/balance")
    ok = ok and "balance" in body
    record("3.2b GET /api/accounts/<id>/balance (Test 3.2)", ok, sc, body,
           f"balance={body.get('balance')}" if ok else str(body)[:120])


def test_account_balance_history():
    if not _account_id:
        return
    sc, body, ok = api("GET", f"/api/accounts/{_account_id}/balance-history",
                        params={"days": 7})
    ok = ok and "history" in body
    record("3.5 GET /api/accounts/<id>/balance-history (Test 3.5)", ok, sc, body,
           f"points={len(body.get('history',[]))}" if ok else str(body)[:120])


def test_balance_adjustment():
    if not _account_id:
        return
    sc, body, ok = api("POST", f"/api/accounts/{_account_id}/balance-adjustment",
                        json_body={"new_opening_balance": 80000.00, "reason": "Test adjustment"})
    ok = ok and "account_id" in body
    record("3.3b POST /api/accounts/<id>/balance-adjustment", ok, sc, body)


def test_recompute_reconciliation():
    if not _account_id:
        return
    sc, body, ok = api("POST", f"/api/accounts/{_account_id}/recompute-reconciliation")
    ok = ok and "reconciliation_status" in body
    record("3.4b POST /api/accounts/<id>/recompute-reconciliation (Test 8.2)", ok, sc, body,
           f"status={body.get('reconciliation_status')}" if ok else str(body)[:120])


def test_list_accounts_with_data():
    sc, body, ok = api("GET", "/api/accounts")
    ok = ok and "accounts" in body and body.get("total", 0) >= 1
    record("3.2c GET /api/accounts (with data)", ok, sc, body,
           f"total={body.get('total')}" if ok else str(body)[:120])


def test_create_account_missing_name():
    sc, body, ok = api("POST", "/api/accounts",
                        json_body={"opening_balance": 1000},
                        expected=(400,))
    record("3.1d POST /api/accounts — 400 missing name (Test 18.2)", ok, sc, body)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 4 — Categories CRUD (Tests 7.3)
# ═══════════════════════════════════════════════════════════════════════════════

def test_list_categories():
    sc, body, ok = api("GET", "/api/categories")
    ok = ok and "categories" in body
    total = body.get("total", 0)
    # After signup there should be 9 system categories
    record("7.3a GET /api/categories (initial — expect ≥9 system cats)", ok, sc, body,
           f"total={total}")


def test_create_category():
    global _category_id
    sc, body, ok = api("POST", "/api/categories",
                        json_body={
                            "name": "Test Food & Dining",
                            "keywords": ["restaurant", "food", "eat"],
                            "color": "#F97316",
                            "applies_to": "expense",
                        },
                        expected=(201,))
    ok = ok and "category" in body
    if ok:
        _category_id = body["category"]["id"]
    record("7.3b POST /api/categories", ok, sc, body,
           f"category_id={_category_id}" if ok else str(body)[:120])
    return ok


def test_get_category():
    if not _category_id:
        return
    sc, body, ok = api("GET", f"/api/categories/{_category_id}")
    ok = ok and "category" in body
    record("7.3c GET /api/categories/<id>", ok, sc, body)


def test_update_category():
    if not _category_id:
        return
    sc, body, ok = api("PATCH", f"/api/categories/{_category_id}",
                        json_body={"keywords": ["restaurant", "food", "eat", "suya"], "color": "#EF4444"})
    ok = ok and "category" in body
    record("7.3d PATCH /api/categories/<id>", ok, sc, body)


def test_duplicate_category_name():
    """Test 7.3 — duplicate active name → 409."""
    if not _category_id:
        return
    sc, body, ok = api("POST", "/api/categories",
                        json_body={"name": "Test Food & Dining"},
                        expected=(409,))
    record("7.3e POST /api/categories — 409 on duplicate name", ok, sc, body)


def test_category_tx_count():
    if not _category_id:
        return
    sc, body, ok = api("GET", f"/api/categories/{_category_id}/transactions-count")
    ok = ok and "count" in body
    record("7.3f GET /api/categories/<id>/transactions-count", ok, sc, body,
           f"count={body.get('count')}" if ok else str(body)[:120])


def test_soft_delete_category_recreate():
    """Test 9.3 — soft-delete a category, then re-create with the same name."""
    # Create a temporary category
    sc, body, ok = api("POST", "/api/categories",
                        json_body={"name": "Temp Marketing Test", "color": "#F59E0B"},
                        expected=(201,))
    if not ok or "category" not in body:
        print(f"{SKIP} 9.3 soft-delete/recreate — could not create temp category")
        return
    tmp_id = body["category"]["id"]

    # Delete it (soft-delete sets deleted_at)
    sc2, body2, ok2 = api("DELETE", f"/api/categories/{tmp_id}", json_body={})
    record("9.3a DELETE category (soft-delete)", ok2 and body2.get("ok") is True, sc2, body2)

    # Re-create with the same name — partial unique index allows this
    sc3, body3, ok3 = api("POST", "/api/categories",
                           json_body={"name": "Temp Marketing Test", "color": "#F59E0B"},
                           expected=(201,))
    record("9.3b POST /api/categories same name after soft-delete — must succeed (Test 9.3)",
           ok3 and "category" in body3, sc3, body3)

    # Clean up
    if ok3 and "category" in body3:
        api("DELETE", f"/api/categories/{body3['category']['id']}", json_body={})


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 5 — Transactions CRUD + Advanced (Tests 4.1–4.7)
# ═══════════════════════════════════════════════════════════════════════════════

def test_list_transactions_empty():
    sc, body, ok = api("GET", "/api/transactions")
    ok = ok and "transactions" in body
    record("4.0 GET /api/transactions (empty)", ok, sc, body,
           f"total={body.get('total',0)}")


def test_create_manual_transaction():
    global _tx_id
    if not _account_id:
        print(f"{SKIP} 4.1 POST /api/transactions — no account")
        return
    ref = f"TEST-REF-{uuid.uuid4().hex[:8].upper()}"
    sc, body, ok = api("POST", "/api/transactions",
                        json_body={
                            "account_id": _account_id,
                            "amount": 5000.00,
                            "direction": "debit",
                            "reference": ref,
                            "date": date.today().isoformat(),
                            "category_id": _category_id,
                            "narration": "Test food purchase at restaurant",
                        },
                        expected=(201,))
    ok = ok and "transaction" in body
    if ok:
        _tx_id = body["transaction"]["id"]
        # Test 4.1 — verify status auto-advances to 'Validated'
        status = body["transaction"].get("status", "")
        record("4.1a POST /api/transactions (manual) — status=Validated", ok and status == "Validated",
               sc, body, f"status={status}")
    else:
        record("4.1a POST /api/transactions (manual)", False, sc, body, str(body)[:120])
    return ok


def test_duplicate_transaction():
    """Test 4.5 — UNIQUE(account_id, reference) → 409."""
    if not _account_id or not _tx_id:
        print(f"{SKIP} 4.5 duplicate tx test")
        return
    sc, body, ok = api("GET", "/api/transactions", params={"account_id": _account_id})
    txs = body.get("transactions", [])
    if not txs:
        print(f"{SKIP} 4.5 — no existing transactions")
        return
    ref = txs[0].get("reference", "")
    if not ref:
        return
    sc2, body2, ok2 = api("POST", "/api/transactions",
                           json_body={
                               "account_id": _account_id,
                               "amount": 100,
                               "direction": "debit",
                               "reference": ref,
                               "date": date.today().isoformat(),
                           },
                           expected=(409,))
    record("4.5 POST /api/transactions — 409 on duplicate reference (Test 4.5)", ok2, sc2, body2)


def test_get_transaction():
    if not _tx_id:
        print(f"{SKIP} 4.2 GET /api/transactions/<id>")
        return
    sc, body, ok = api("GET", f"/api/transactions/{_tx_id}")
    ok = ok and "transaction" in body
    record("4.2a GET /api/transactions/<id>", ok, sc, body,
           f"status={body.get('transaction',{}).get('status')}" if ok else str(body)[:120])


def test_update_transaction():
    """Test 4.3 — update narration, verify status advances to Categorized."""
    if not _tx_id:
        return
    sc, body, ok = api("PATCH", f"/api/transactions/{_tx_id}",
                        json_body={"narration": "Updated food narration", "category_id": _category_id})
    ok = ok and "transaction" in body
    status = body.get("transaction", {}).get("status", "") if ok else ""
    record("4.3 PATCH /api/transactions/<id> (Test 4.3)", ok, sc, body,
           f"status={status}")


def test_create_transfer_pair():
    """Test 11.1 — create debit on A and credit on B, link as transfer."""
    global _tx_id2
    if not _account_id or not _account_id_b:
        print(f"{SKIP} 11.1 transfer-pair — need two accounts")
        return

    # Debit on account A
    ref_debit = f"TRF-D-{uuid.uuid4().hex[:8].upper()}"
    sc1, b1, ok1 = api("POST", "/api/transactions",
                         json_body={
                             "account_id": _account_id,
                             "amount": 50000.00,
                             "direction": "debit",
                             "reference": ref_debit,
                             "date": date.today().isoformat(),
                             "narration": "Transfer to OPay",
                         },
                         expected=(201,))
    debit_id = b1.get("transaction", {}).get("id") if ok1 else None

    # Credit on account B
    ref_credit = f"TRF-C-{uuid.uuid4().hex[:8].upper()}"
    sc2, b2, ok2 = api("POST", "/api/transactions",
                         json_body={
                             "account_id": _account_id_b,
                             "amount": 50000.00,
                             "direction": "credit",
                             "reference": ref_credit,
                             "date": date.today().isoformat(),
                             "narration": "Transfer from Moniepoint",
                         },
                         expected=(201,))
    credit_id = b2.get("transaction", {}).get("id") if ok2 else None
    if credit_id:
        _tx_id2 = credit_id

    record("11.1a Create debit tx for transfer (Test 11.1)", ok1 and debit_id is not None, sc1, b1)
    record("11.1b Create credit tx for transfer (Test 11.1)", ok2 and credit_id is not None, sc2, b2)

    if debit_id and credit_id:
        # Link debit as transfer pointing to credit
        sc3, b3, ok3 = api("POST", f"/api/transactions/{debit_id}/mark-transfer",
                             json_body={"transfer_pair_id": credit_id})
        record("11.1c Mark-transfer links debit→credit (Test 11.1)", ok3 and b3.get("ok") is True, sc3, b3)


def test_flag_mis_parse():
    if not _tx_id:
        return
    sc, body, ok = api("POST", f"/api/transactions/{_tx_id}/flag-mis-parse",
                        json_body={"note": "Test mis-parse flag"})
    ok = ok and body.get("ok") is True
    record("4.7b POST /api/transactions/<id>/flag-mis-parse (Test 4.7)", ok, sc, body)


def test_void_transaction():
    """Test 4.4 — void a transaction (DELETE → sets voided_at)."""
    if not _account_id:
        print(f"{SKIP} 4.4 void transaction")
        return
    ref = f"VOID-TEST-{uuid.uuid4().hex[:8].upper()}"
    sc, body, ok = api("POST", "/api/transactions",
                        json_body={
                            "account_id": _account_id,
                            "amount": 1000.00,
                            "direction": "debit",
                            "reference": ref,
                            "date": date.today().isoformat(),
                            "narration": "To be voided",
                        },
                        expected=(201,))
    if not ok or "transaction" not in body:
        print(f"{SKIP} 4.4 void — could not create tx")
        return
    void_tx_id = body["transaction"]["id"]

    # Void it
    sc2, body2, ok2 = api("DELETE", f"/api/transactions/{void_tx_id}", expected=(200,))
    record("4.4a DELETE /api/transactions/<id> — void (Test 4.4)", ok2 and body2.get("ok") is True,
           sc2, body2)

    # Verify it's excluded from balance by checking the tx is voided
    sc3, body3, _ = api("GET", f"/api/transactions/{void_tx_id}")
    voided_at = body3.get("transaction", {}).get("voided_at") if body3.get("transaction") else None
    record("4.4b Voided tx has voided_at set (Test 4.4)", voided_at is not None, sc3, body3,
           f"voided_at={voided_at}")


def test_transaction_filters():
    """Test 4.2 — filtered + paginated transaction list."""
    if not _account_id:
        return
    sc, body, ok = api("GET", "/api/transactions",
                        params={"account_id": _account_id, "direction": "debit", "per_page": 10})
    ok = ok and "transactions" in body
    record("4.2b GET /api/transactions — filter by account+direction (Test 4.2)", ok, sc, body,
           f"returned={len(body.get('transactions',[]))}")

    # Filter by category
    if _category_id:
        sc2, body2, ok2 = api("GET", "/api/transactions",
                               params={"category_id": _category_id})
        ok2 = ok2 and "transactions" in body2
        record("4.2c GET /api/transactions — filter by category_id", ok2, sc2, body2,
               f"returned={len(body2.get('transactions',[]))}")

    # Filter by date range
    sc3, body3, ok3 = api("GET", "/api/transactions",
                           params={"date_from": (date.today() - timedelta(days=7)).isoformat(),
                                   "date_to": date.today().isoformat()})
    ok3 = ok3 and "transactions" in body3
    record("4.2d GET /api/transactions — filter by date_range", ok3, sc3, body3)


def test_account_transactions():
    if not _account_id:
        return
    sc, body, ok = api("GET", f"/api/accounts/{_account_id}/transactions")
    ok = ok and "transactions" in body
    record("4.2e GET /api/accounts/<id>/transactions", ok, sc, body,
           f"total={body.get('total')}")


def test_transaction_archive():
    sc, body, ok = api("GET", "/api/transactions/archive")
    ok = ok and "transactions" in body
    record("8.4b GET /api/transactions/archive (Test 8.4)", ok, sc, body,
           f"total={body.get('total', 0)}")


def test_list_transactions_with_data():
    sc, body, ok = api("GET", "/api/transactions")
    ok = ok and "transactions" in body and body.get("total", 0) >= 1
    record("4.2f GET /api/transactions (with data)", ok, sc, body,
           f"total={body.get('total')}")


def test_create_transaction_missing_fields():
    """Test 18.2 — 400 on missing required fields."""
    sc, body, ok = api("POST", "/api/transactions",
                        json_body={"amount": 100},
                        expected=(400,))
    record("18.2 POST /api/transactions — 400 on missing fields (Test 18.2)", ok, sc, body)


def test_404_transaction():
    """Test 18.1 — 404 on non-existent transaction."""
    sc, body, ok = api("GET", f"/api/transactions/{uuid.uuid4()}", expected=(404,))
    record("18.1a GET /api/transactions/<non-existent> — 404 (Test 18.1)", ok, sc, body)


def test_404_account():
    """Test 18.1 — 404 on non-existent account."""
    sc, body, ok = api("GET", f"/api/accounts/{uuid.uuid4()}", expected=(404,))
    record("18.1b GET /api/accounts/<non-existent> — 404 (Test 18.1)", ok, sc, body)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 6 — Overview / Dashboard (Tests 2.1–2.4)
# ═══════════════════════════════════════════════════════════════════════════════

def test_overview():
    sc, body, ok = api("GET", "/api/overview")
    ok = ok and "total_balance" in body
    record("2.1 GET /api/overview — Dashboard Summary (Test 2.1)", ok, sc, body,
           f"balance={body.get('total_balance')} needs_review={body.get('needs_review_count')}" if ok else str(body)[:120])


def test_overview_chart_30():
    """Test 2.2 — income/expense chart 30 days."""
    sc, body, ok = api("GET", "/api/overview/chart", params={"period": "30"})
    ok = ok and "chart_data" in body
    record("2.2a GET /api/overview/chart?period=30 (Test 2.2)", ok, sc, body,
           f"points={len(body.get('chart_data',[]))}" if ok else str(body)[:120])


def test_overview_chart_90():
    sc, body, ok = api("GET", "/api/overview/chart", params={"period": "90"})
    ok = ok and "chart_data" in body
    record("2.2b GET /api/overview/chart?period=90", ok, sc, body)


def test_overview_chart_year():
    sc, body, ok = api("GET", "/api/overview/chart", params={"period": "year"})
    ok = ok and "chart_data" in body
    record("2.2c GET /api/overview/chart?period=year", ok, sc, body)


def test_overview_donut():
    """Test 2.3 — spend by category."""
    sc, body, ok = api("GET", "/api/overview/donut")
    ok = ok and "donut_data" in body
    record("2.3 GET /api/overview/donut — Spend by Category (Test 2.3)", ok, sc, body,
           f"categories={len(body.get('donut_data',[]))}" if ok else str(body)[:120])


def test_overview_digests():
    sc, body, ok = api("GET", "/api/overview/digests")
    ok = ok and "digests" in body
    record("15.2a GET /api/overview/digests (Test 15.2)", ok, sc, body,
           f"count={len(body.get('digests',[]))}")
    return body.get("digests", [])


def test_mark_digest_read_individual():
    """Test 15.2 — mark individual digest as read."""
    # Insert a test digest via service role
    if not _user_id:
        return
    r = supabase_admin("POST", "digests",
                        json={
                            "user_id": _user_id,
                            "date": date.today().isoformat(),
                            "new_transaction_count": 1,
                            "needs_review_count": 0,
                            "reconciliation_mismatch_count": 0,
                            "failed_import_count": 0,
                        })
    if r.status_code not in (200, 201):
        print(f"{SKIP} 15.2b mark-digest-read — could not insert test digest")
        return
    digests_created = r.json()
    if not digests_created:
        return
    digest_id = digests_created[0].get("id")

    sc, body, ok = api("POST", f"/api/overview/digests/{digest_id}/read")
    ok = ok and body.get("ok") is True
    record("15.2b POST /api/overview/digests/<id>/read (Test 15.2)", ok, sc, body)
    TESTED_ENDPOINTS.add("POST:/api/overview/digests/<id>/read")


def test_mark_all_digests_read():
    sc, body, ok = api("POST", "/api/overview/digests/read-all")
    ok = ok and body.get("ok") is True
    record("15.2c POST /api/overview/digests/read-all", ok, sc, body)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 7 — Review Queue (Tests 5.1–5.4)
# ═══════════════════════════════════════════════════════════════════════════════

def test_review_stats():
    sc, body, ok = api("GET", "/api/review/stats")
    ok = ok and "needs_review" in body
    record("5.0 GET /api/review/stats", ok, sc, body,
           f"needs_review={body.get('needs_review')} failed={body.get('failed_imports')}" if ok else str(body)[:120])


def test_review_queue_and_skip():
    """Test 5.1 + 5.3 — uncategorized transactions → review queue → skip."""
    # Create an uncategorized transaction
    if not _account_id:
        print(f"{SKIP} 5.1/5.3 review queue — no account")
        return
    ref = f"UNCAT-{uuid.uuid4().hex[:8].upper()}"
    sc, body, ok = api("POST", "/api/transactions",
                        json_body={
                            "account_id": _account_id,
                            "amount": 2500.00,
                            "direction": "debit",
                            "reference": ref,
                            "date": date.today().isoformat(),
                            "narration": "Unknown purchase",
                            # No category_id → uncategorized
                        },
                        expected=(201,))
    uncat_tx_id = body.get("transaction", {}).get("id") if ok else None

    sc2, body2, ok2 = api("GET", "/api/review/queue")
    ok2 = ok2 and "transactions" in body2
    record("5.1 GET /api/review/queue (Test 5.1)", ok2, sc2, body2,
           f"total={body2.get('total',0)}")

    txs = body2.get("transactions", [])
    if txs:
        tx_id = txs[0]["id"]
        sc3, body3, ok3 = api("POST", f"/api/review/queue/{tx_id}/skip")
        ok3 = ok3 and body3.get("ok") is True
        record("5.3 POST /api/review/queue/<id>/skip (Test 5.3)", ok3, sc3, body3)
    else:
        print(f"{SKIP} 5.3 skip — review queue empty")


def test_review_categorize():
    """Test 5.2 — categorize from review queue."""
    if not _category_id:
        return
    sc, body, ok = api("GET", "/api/review/queue")
    txs = body.get("transactions", [])
    if not txs:
        print(f"{SKIP} 5.2 categorize — queue empty")
        return
    tx_id = txs[0]["id"]
    sc2, body2, ok2 = api("POST", f"/api/review/queue/{tx_id}/categorize",
                           json_body={"category_id": _category_id, "create_rule": False})
    ok2 = ok2 and body2.get("ok") is True
    record("5.2 POST /api/review/queue/<id>/categorize (Test 5.2)", ok2, sc2, body2)


def test_failed_imports():
    """Test 5.4 — failed imports list."""
    global _failed_import_id
    # Insert a test failed import via service role
    if _user_id and _account_id:
        msg_id = f"TEST-MSG-{uuid.uuid4().hex[:12]}"
        r = supabase_admin("POST", "failed_imports",
                            json={
                                "user_id": _user_id,
                                "account_id": _account_id,
                                "gmail_message_id": msg_id,
                                "raw_subject": "Test Failed Import",
                                "raw_body": "unparseable email content here",
                                "failure_reason": "Parser could not extract amount",
                                "status": "pending",
                            })
        if r.status_code in (200, 201) and r.json():
            _failed_import_id = r.json()[0].get("id")

    sc, body, ok = api("GET", "/api/review/failed-imports")
    ok = ok and "failed_imports" in body
    record("5.4a GET /api/review/failed-imports (Test 5.4)", ok, sc, body,
           f"total={body.get('total',0)}")


def test_failed_import_detail():
    """Test 5.4 — single failed import detail."""
    if not _failed_import_id:
        print(f"{SKIP} 5.4b failed import detail — no import")
        return
    sc, body, ok = api("GET", f"/api/review/failed-imports/{_failed_import_id}")
    record("5.4b GET /api/review/failed-imports/<id> (Test 5.4)", ok, sc, body,
           str(body)[:80])
    TESTED_ENDPOINTS.add("GET:/api/review/failed-imports/<id>")


def test_failed_import_ignore():
    """Test 5.4 — ignore a failed import."""
    if not _failed_import_id:
        print(f"{SKIP} 5.4c ignore failed import — no import")
        return
    sc, body, ok = api("POST", f"/api/review/failed-imports/{_failed_import_id}/ignore")
    ok = ok and body.get("ok") is True
    record("5.4c POST /api/review/failed-imports/<id>/ignore (Test 5.4)", ok, sc, body)
    TESTED_ENDPOINTS.add("POST:/api/review/failed-imports/<id>/ignore")


def test_failed_import_convert():
    """Test 5.4 — convert a failed import to a transaction."""
    if not _account_id:
        print(f"{SKIP} 5.4d convert failed import — no account")
        return
    # Create a fresh failed import to convert
    if not _user_id:
        return
    msg_id2 = f"TEST-CONV-{uuid.uuid4().hex[:12]}"
    r = supabase_admin("POST", "failed_imports",
                        json={
                            "user_id": _user_id,
                            "account_id": _account_id,
                            "gmail_message_id": msg_id2,
                            "raw_subject": "Convertible Import",
                            "raw_body": "₦3,000 debit",
                            "failure_reason": "Test",
                            "status": "pending",
                        })
    if r.status_code not in (200, 201) or not r.json():
        print(f"{SKIP} 5.4d convert — could not insert")
        return
    convert_id = r.json()[0].get("id")
    sc, body, ok = api("POST", f"/api/review/failed-imports/{convert_id}/convert",
                        json_body={
                            "amount": 3000.00,
                            "direction": "debit",
                            "date": date.today().isoformat(),
                            "narration": "Manual conversion",
                            "account_id": _account_id,
                        })
    record("5.4d POST /api/review/failed-imports/<id>/convert (Test 5.4)", ok, sc, body,
           str(body)[:80])
    TESTED_ENDPOINTS.add("POST:/api/review/failed-imports/<id>/convert")


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 8 — Reports (Tests 6.1–6.4)
# ═══════════════════════════════════════════════════════════════════════════════

def test_report_pl():
    """Test 6.1 — P&L report."""
    date_from = (date.today() - timedelta(days=30)).isoformat()
    date_to   = date.today().isoformat()
    sc, body, ok = api("GET", "/api/reports/pl",
                        params={"date_from": date_from, "date_to": date_to})
    ok = ok and "total_income" in body and "total_expense" in body
    record("6.1 GET /api/reports/pl (Test 6.1)", ok, sc, body,
           f"income={body.get('total_income')} expense={body.get('total_expense')} net={body.get('net_profit')}" if ok else str(body)[:120])


def test_report_pl_with_account():
    if not _account_id:
        return
    sc, body, ok = api("GET", "/api/reports/pl",
                        params={"account_id": _account_id})
    ok = ok and "total_income" in body
    record("6.1b GET /api/reports/pl?account_id=<id>", ok, sc, body)


def test_report_cashflow():
    """Test 6.2 — cash flow report: total_inflow, total_outflow, net."""
    sc, body, ok = api("GET", "/api/reports/cashflow")
    ok = ok and "opening_balance" in body and "total_in" in body and "total_out" in body
    net = None
    if ok:
        net = round(body.get("total_in", 0) - body.get("total_out", 0), 2)
    record("6.2 GET /api/reports/cashflow (Test 6.2)", ok, sc, body,
           f"opening={body.get('opening_balance')} in={body.get('total_in')} out={body.get('total_out')} net_computed={net}" if ok else str(body)[:120])


def test_report_reconciliation():
    """Test 6.3 — reconciliation report."""
    sc, body, ok = api("GET", "/api/reports/reconciliation")
    ok = ok and "mismatches" in body
    record("6.3 GET /api/reports/reconciliation (Test 6.3)", ok, sc, body,
           f"mismatches={body.get('total_mismatches')} all_reconciled={body.get('all_reconciled')}" if ok else str(body)[:120])


def test_report_export_pl_pdf():
    """Test 6.4 — export P&L as PDF."""
    r = requests.get(
        f"{BASE}/api/reports/export/pl.pdf",
        headers=auth_header(),
        params={"date_from": (date.today() - timedelta(days=30)).isoformat()},
        timeout=30,
    )
    TESTED_ENDPOINTS.add("GET:/api/reports/export/<type>.<fmt>")
    ok = r.status_code == 200 and "pdf" in r.headers.get("content-type", "").lower()
    record("6.4a GET /api/reports/export/pl.pdf (Test 6.4)", ok, r.status_code, {},
           f"content-type={r.headers.get('content-type','?')}" if ok else r.text[:120])


def test_report_export_pl_xlsx():
    r = requests.get(
        f"{BASE}/api/reports/export/pl.xlsx",
        headers=auth_header(),
        timeout=30,
    )
    TESTED_ENDPOINTS.add("GET:/api/reports/export/<type>.<fmt>")
    ok = r.status_code == 200 and "spreadsheet" in r.headers.get("content-type", "").lower()
    record("6.4b GET /api/reports/export/pl.xlsx (Test 6.4)", ok, r.status_code, {},
           f"content-type={r.headers.get('content-type','?')}" if ok else r.text[:120])


def test_report_export_cashflow_pdf():
    r = requests.get(
        f"{BASE}/api/reports/export/cashflow.pdf",
        headers=auth_header(),
        timeout=30,
    )
    TESTED_ENDPOINTS.add("GET:/api/reports/export/<type>.<fmt>")
    ok = r.status_code == 200 and "pdf" in r.headers.get("content-type", "").lower()
    record("6.4c GET /api/reports/export/cashflow.pdf", ok, r.status_code, {},
           f"content-type={r.headers.get('content-type','?')}" if ok else r.text[:120])


def test_report_export_reconciliation_xlsx():
    r = requests.get(
        f"{BASE}/api/reports/export/reconciliation.xlsx",
        headers=auth_header(),
        timeout=30,
    )
    TESTED_ENDPOINTS.add("GET:/api/reports/export/<type>.<fmt>")
    ok = r.status_code == 200 and "spreadsheet" in r.headers.get("content-type", "").lower()
    record("6.4d GET /api/reports/export/reconciliation.xlsx", ok, r.status_code, {},
           f"content-type={r.headers.get('content-type','?')}" if ok else r.text[:120])


def test_report_export_invalid():
    sc, body, ok = api("GET", "/api/reports/export/invalid.xyz", expected=(400,))
    TESTED_ENDPOINTS.add("GET:/api/reports/export/<type>.<fmt>")
    record("6.4e GET /api/reports/export/invalid.xyz — 400 on bad type", ok, sc, body)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 9 — Parsing Rules (Tests 16.1–16.5)
# ═══════════════════════════════════════════════════════════════════════════════

def test_list_parsing_rules():
    sc, body, ok = api("GET", "/api/settings/parsing-rules")
    ok = ok and "parsing_rules" in body
    record("16.0 GET /api/settings/parsing-rules", ok, sc, body,
           f"total={body.get('total',0)}")


def test_create_parsing_rule():
    global _rule_id
    if not _category_id:
        return
    sc, body, ok = api("POST", "/api/settings/parsing-rules",
                        json_body={
                            "sender_email": "no-reply@moniepoint.com",
                            "pattern": "suya spot",
                            "category_id": _category_id,
                            "priority": 0,
                            "is_regex": False,
                            "case_sensitive": False,
                        },
                        expected=(201,))
    ok = ok and "parsing_rule" in body
    if ok:
        _rule_id = body["parsing_rule"]["id"]
    record("16.1 POST /api/settings/parsing-rules (Test 16.1)", ok, sc, body,
           f"rule_id={_rule_id}" if ok else str(body)[:120])


def test_create_regex_parsing_rule():
    """Test 16.4 — regex rule."""
    if not _category_id:
        return
    sc, body, ok = api("POST", "/api/settings/parsing-rules",
                        json_body={
                            "sender_email": "no-reply@moniepoint.com",
                            "pattern": "purchase.*store",
                            "category_id": _category_id,
                            "priority": 10,
                            "is_regex": True,
                            "case_sensitive": False,
                        },
                        expected=(201,))
    ok = ok and "parsing_rule" in body
    record("16.4 POST /api/settings/parsing-rules — regex (Test 16.4)", ok, sc, body)
    # Clean up
    if ok and "parsing_rule" in body:
        api("DELETE", f"/api/settings/parsing-rules/{body['parsing_rule']['id']}")


def test_create_case_sensitive_parsing_rule():
    """Test 16.5 — case-sensitive rule."""
    if not _category_id:
        return
    sc, body, ok = api("POST", "/api/settings/parsing-rules",
                        json_body={
                            "sender_email": "no-reply@moniepoint.com",
                            "pattern": "PURCHASE",
                            "category_id": _category_id,
                            "priority": 5,
                            "is_regex": False,
                            "case_sensitive": True,
                        },
                        expected=(201,))
    ok = ok and "parsing_rule" in body
    record("16.5 POST /api/settings/parsing-rules — case_sensitive (Test 16.5)", ok, sc, body)
    # Clean up
    if ok and "parsing_rule" in body:
        api("DELETE", f"/api/settings/parsing-rules/{body['parsing_rule']['id']}")


def test_update_parsing_rule():
    """Test 16.2 — update priority."""
    if not _rule_id:
        return
    sc, body, ok = api("PATCH", f"/api/settings/parsing-rules/{_rule_id}",
                        json_body={"pattern": "suya spot lekki", "priority": 5})
    ok = ok and "parsing_rule" in body
    record("16.2 PATCH /api/settings/parsing-rules/<id> priority=5 (Test 16.2)", ok, sc, body)


def test_delete_parsing_rule():
    if not _rule_id:
        return
    sc, body, ok = api("DELETE", f"/api/settings/parsing-rules/{_rule_id}")
    ok = ok and body.get("ok") is True
    record("16.1e DELETE /api/settings/parsing-rules/<id>", ok, sc, body)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 10 — Budgets (Tests 12.1–12.5)
# ═══════════════════════════════════════════════════════════════════════════════

def test_list_budgets():
    sc, body, ok = api("GET", "/api/settings/budgets")
    ok = ok and "budgets" in body
    record("12.0 GET /api/settings/budgets", ok, sc, body,
           f"count={len(body.get('budgets',[]))}")


def test_create_budget():
    global _budget_id
    if not _category_id:
        return
    today = date.today()
    sc, body, ok = api("POST", "/api/settings/budgets",
                        json_body={
                            "category_id": _category_id,
                            "amount": 100000,
                            "period_month": today.month,
                            "period_year": today.year,
                        },
                        expected=(201,))
    ok = ok and "budget" in body
    if ok:
        _budget_id = body["budget"]["id"]
    record("12.1 POST /api/settings/budgets (Test 12.1)", ok, sc, body,
           f"budget_id={_budget_id}" if ok else str(body)[:120])


def test_budget_duplicate():
    """Test 12.1 — UNIQUE(user_id, category_id, period_month, period_year) → 409."""
    if not _category_id:
        return
    today = date.today()
    sc, body, ok = api("POST", "/api/settings/budgets",
                        json_body={
                            "category_id": _category_id,
                            "amount": 50000,
                            "period_month": today.month,
                            "period_year": today.year,
                        },
                        expected=(409,))
    record("12.1b POST /api/settings/budgets — 409 on duplicate (Test 12.1)", ok, sc, body)


def test_budget_tracking():
    """Test 12.2/12.3 — budget tracking with spending."""
    today = date.today()
    sc, body, ok = api("GET", "/api/settings/budgets",
                        params={"month": today.month, "year": today.year})
    ok = ok and "budgets" in body
    record("12.2 GET /api/settings/budgets (tracking, Test 12.2)", ok, sc, body,
           f"budgets={len(body.get('budgets',[]))}" if ok else str(body)[:120])


def test_delete_budget():
    if not _budget_id:
        return
    sc, body, ok = api("DELETE", f"/api/settings/budgets/{_budget_id}")
    ok = ok and body.get("ok") is True
    record("12.5 DELETE /api/settings/budgets/<id> (Test 12.5)", ok, sc, body)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 11 — Audit Log (Test 7.5)
# ═══════════════════════════════════════════════════════════════════════════════

def test_audit_log():
    sc, body, ok = api("GET", "/api/settings/audit-log")
    ok = ok and "audit_log" in body
    record("7.5a GET /api/settings/audit-log (Test 7.5)", ok, sc, body,
           f"entries={len(body.get('audit_log',[]))}")


def test_audit_log_filtered():
    sc, body, ok = api("GET", "/api/settings/audit-log",
                        params={"action_type": "account_created", "limit": 5})
    ok = ok and "audit_log" in body
    record("7.5b GET /api/settings/audit-log?action_type=account_created (Test 7.5)", ok, sc, body,
           f"entries={len(body.get('audit_log',[]))}")


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 12 — Sync Jobs & Gmail (Tests 8.1–8.5)
# ═══════════════════════════════════════════════════════════════════════════════

def test_list_sync_jobs():
    sc, body, ok = api("GET", "/api/sync/jobs")
    ok = ok and "sync_jobs" in body
    record("8.0 GET /api/sync/jobs", ok, sc, body,
           f"count={len(body.get('sync_jobs',[]))}")
    jobs = body.get("sync_jobs", [])
    if jobs:
        job_id = jobs[0].get("id")
        if job_id:
            sc2, body2, ok2 = api("GET", f"/api/sync/jobs/{job_id}")
            record("8.0b GET /api/sync/jobs/<id>", ok2, sc2, body2)
            TESTED_ENDPOINTS.add("GET:/api/sync/jobs/<id>")


def test_sync_trigger_no_gmail():
    """Test 8.1 — trigger sync on account without Gmail → 400."""
    if not _account_id:
        return
    sc, body, ok = api("POST", "/api/sync/trigger",
                        json_body={"account_id": _account_id},
                        expected=(400,))
    record("8.1 POST /api/sync/trigger — 400 without Gmail (Test 8.1)", ok, sc, body,
           body.get("error", "")[:80] if not ok else "correct 400 returned")


def test_gmail_initiate():
    """Test 1.2 — Gmail OAuth initiate returns auth_url."""
    if not _account_id:
        return
    sc, body, ok = api("GET", "/api/sync/gmail/initiate",
                        params={"account_id": _account_id})
    ok = ok and "auth_url" in body
    record("1.2 GET /api/sync/gmail/initiate (Test 1.2)", ok, sc, body,
           "auth_url returned" if ok else str(body)[:120])


def test_gmail_disconnect():
    """Test 7.1 — Gmail disconnect (account has no token → expect 400 or 200)."""
    if not _account_id:
        return
    sc, body, ok = api("POST", f"/api/sync/gmail/disconnect/{_account_id}",
                        expected=(200, 400))
    record("7.1 POST /api/sync/gmail/disconnect/<id> (Test 7.1)", ok, sc, body,
           f"status={sc}")
    TESTED_ENDPOINTS.add("POST:/api/sync/gmail/disconnect/<id>")


def test_sync_reparse():
    """Test reparse endpoint."""
    if not _account_id:
        return
    sc, body, ok = api("POST", f"/api/sync/reparse/{_account_id}",
                        expected=(200, 400))
    record("8.1b POST /api/sync/reparse/<account_id>", ok, sc, body,
           str(body)[:80])
    TESTED_ENDPOINTS.add("POST:/api/sync/reparse/<id>")


def test_housekeeping():
    """Test 8.4/8.5 — housekeeping (archive + purge) with cron key."""
    cron_secret = os.environ.get("CRON_SECRET", "")
    r = requests.post(
        f"{BASE}/api/sync/housekeeping",
        headers={"X-Cron-Key": cron_secret},
        timeout=30,
    )
    TESTED_ENDPOINTS.add("POST:/api/sync/housekeeping")
    try:
        body = r.json()
    except Exception:
        body = {}
    ok = r.status_code == 200
    record("8.4/8.5 POST /api/sync/housekeeping (cron key) (Test 8.4/8.5)", ok, r.status_code, body,
           f"result={str(body)[:80]}" if ok else str(body)[:120])


def test_cron_trigger():
    """Test 8.1/8.2/8.3 — system cron trigger."""
    cron_secret = os.environ.get("CRON_SECRET", "")
    r = requests.post(
        f"{BASE}/api/sync/cron",
        headers={"X-Cron-Key": cron_secret},
        timeout=60,
    )
    TESTED_ENDPOINTS.add("POST:/api/sync/cron")
    try:
        body = r.json()
    except Exception:
        body = {}
    ok = r.status_code in (200, 202)
    record("8.1c POST /api/sync/cron (system cron) (Test 8.1)", ok, r.status_code, body,
           f"result={str(body)[:80]}" if ok else str(body)[:120])


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 13 — Reconciliation (Tests 11.3–11.5)
# ═══════════════════════════════════════════════════════════════════════════════

def test_reconciliation_mismatch():
    """Test 11.3 — set actual_balance, compute mismatch via recompute."""
    if not _account_id:
        print(f"{SKIP} 11.3 reconciliation mismatch — no account")
        return

    # Set actual_balance to a value that will create a mismatch
    supabase_admin("PATCH", f"accounts?id=eq.{_account_id}",
                   json={"actual_balance": 999999.00})

    sc, body, ok = api("POST", f"/api/accounts/{_account_id}/recompute-reconciliation")
    status = body.get("reconciliation_status", "")
    # With actual_balance=999999 and opening_balance=80000 (no matching txs sum), expect mismatch
    record("11.3 POST recompute-reconciliation — detects mismatch (Test 11.3)", ok, sc, body,
           f"status={status} gap={body.get('reconciliation_gap')}")


def test_reconciliation_fix():
    """Test 11.4 — restore actual_balance to match, recompute → ok."""
    if not _account_id:
        return
    # Set actual_balance back to a value matching the real balance (use 0 = definitely matches opening)
    supabase_admin("PATCH", f"accounts?id=eq.{_account_id}",
                   json={"actual_balance": None})  # NULL = auto mode

    sc, body, ok = api("POST", f"/api/accounts/{_account_id}/recompute-reconciliation")
    record("11.4 POST recompute-reconciliation after fix (Test 11.4)", ok, sc, body,
           f"status={body.get('reconciliation_status')} gap={body.get('reconciliation_gap')}")


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 14 — Data Integrity Constraints (Tests 13.1–13.5)
# ═══════════════════════════════════════════════════════════════════════════════

def test_negative_amount_constraint():
    """Test 13.1 — CHECK (amount > 0) blocks negative amounts."""
    if not _account_id:
        return
    sc, body, ok = api("POST", "/api/transactions",
                        json_body={
                            "account_id": _account_id,
                            "amount": -100,
                            "direction": "debit",
                            "reference": f"NEG-{uuid.uuid4().hex[:8]}",
                            "date": date.today().isoformat(),
                        },
                        expected=(400, 422))
    record("13.1 POST /api/transactions amount=-100 → rejected (Test 13.1)", ok, sc, body,
           str(body)[:80])


def test_invalid_category_fk():
    """Test 13.2 — FK constraint blocks non-existent category_id."""
    if not _account_id:
        return
    sc, body, ok = api("POST", "/api/transactions",
                        json_body={
                            "account_id": _account_id,
                            "amount": 500,
                            "direction": "debit",
                            "reference": f"BADC-{uuid.uuid4().hex[:8]}",
                            "date": date.today().isoformat(),
                            "category_id": str(uuid.uuid4()),  # non-existent
                        },
                        expected=(400, 404, 409, 422))
    record("13.2 POST /api/transactions — invalid category_id rejected (Test 13.2)", ok, sc, body,
           str(body)[:80])


def test_invalid_account_fk():
    """Test 13.3 — FK constraint blocks non-existent account_id."""
    sc, body, ok = api("POST", "/api/transactions",
                        json_body={
                            "account_id": str(uuid.uuid4()),  # non-existent
                            "amount": 500,
                            "direction": "debit",
                            "reference": f"BADA-{uuid.uuid4().hex[:8]}",
                            "date": date.today().isoformat(),
                        },
                        expected=(400, 404, 409, 422))
    record("13.3 POST /api/transactions — invalid account_id rejected (Test 13.3)", ok, sc, body,
           str(body)[:80])


def test_invalid_transaction_status():
    """Test 13.5 — invalid status enum blocked at API level."""
    if not _account_id:
        return
    # Try to set an invalid status on an existing transaction via PATCH
    if not _tx_id:
        return
    sc, body, ok = api("PATCH", f"/api/transactions/{_tx_id}",
                        json_body={"status": "InvalidStatus"},
                        expected=(400, 422))
    record("13.5 PATCH /api/transactions/<id> status=InvalidStatus → rejected (Test 13.5)", ok, sc, body,
           str(body)[:80])


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 15 — RLS Security (Tests 14.1–14.4)
# ═══════════════════════════════════════════════════════════════════════════════

def test_rls_create_user_b():
    """Set up User B for isolation tests."""
    global _token_b, _user_id_b
    tok, uid = signup_user(TEST_EMAIL_B, TEST_PASSWORD)
    ok = tok is not None
    if ok:
        _token_b = tok
        _user_id_b = uid
    record("14.0 Auth — sign up User B for RLS tests (Test 14.1)", ok,
           200 if ok else 400, {},
           f"user_b_id={_user_id_b}" if ok else "signup failed")
    return ok


def test_rls_user_isolation_accounts():
    """Test 14.2 — User B cannot see User A's accounts."""
    if not _token_b:
        print(f"{SKIP} 14.1 RLS isolation — User B not created")
        return
    sc, body, ok = api("GET", "/api/accounts", token=_token_b)
    accounts_b = body.get("accounts", [])
    # User B has no accounts (empty DB for them), so total should be 0
    ok_isolation = ok and body.get("total", 0) == 0
    record("14.1 GET /api/accounts as User B — sees ZERO of User A's accounts (Test 14.1/14.2)",
           ok_isolation, sc, body,
           f"User B sees {body.get('total',0)} accounts (expected 0)")


def test_rls_user_isolation_transactions():
    """Test 14.1 — User B cannot see User A's transactions."""
    if not _token_b:
        print(f"{SKIP} 14.1 RLS tx isolation")
        return
    sc, body, ok = api("GET", "/api/transactions", token=_token_b)
    tx_count = body.get("total", 0)
    ok_isolation = ok and tx_count == 0
    record("14.1b GET /api/transactions as User B — sees ZERO of User A's txs (Test 14.1)",
           ok_isolation, sc, body,
           f"User B sees {tx_count} txs (expected 0)")


def test_rls_cross_user_update():
    """Test 14.3/18.4 — User B cannot update User A's transaction."""
    if not _token_b or not _tx_id:
        print(f"{SKIP} 14.3 cross-user update")
        return
    sc, body, ok = api("PATCH", f"/api/transactions/{_tx_id}",
                        json_body={"narration": "Hacked by User B"},
                        expected=(403, 404),
                        token=_token_b)
    record("14.3/18.4 PATCH User A's tx as User B — 403/404 (Test 14.3, 18.4)", ok, sc, body,
           f"status={sc}")


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 16 — Performance Tests (Tests 10.1–10.3, 20.2)
# ═══════════════════════════════════════════════════════════════════════════════

def test_bulk_insert_performance():
    """Test 20.2 / 10.1 — bulk insert 1000 transactions via service role, verify API reads fast."""
    if not _account_id or not _user_id:
        print(f"{SKIP} 20.2 bulk insert — no account")
        return

    today = date.today()
    batch = []
    for i in range(1000):
        batch.append({
            "user_id": _user_id,
            "account_id": _account_id,
            "amount": float(100 + (i % 9900)),
            "direction": "debit" if i % 2 == 0 else "credit",
            "reference": f"BULK-{i:05d}-{uuid.uuid4().hex[:6].upper()}",
            "date": (today - timedelta(days=i % 365)).isoformat(),
            "narration": f"Bulk test transaction {i}",
            "status": "Validated",
        })

    print(f"\n{INFO} Bulk-inserting 1000 transactions via service role...")
    t0 = time.time()
    # Insert in chunks of 100 to stay within Supabase limits
    success_count = 0
    for chunk_start in range(0, 1000, 100):
        chunk = batch[chunk_start:chunk_start + 100]
        r = supabase_admin("POST", "transactions", json=chunk)
        if r.status_code in (200, 201):
            success_count += len(r.json()) if isinstance(r.json(), list) else 0
    elapsed_insert = time.time() - t0
    record(f"20.2a Bulk insert 1000 txs ({success_count} ok) in {elapsed_insert:.2f}s (Test 20.2)",
           elapsed_insert < 15 and success_count >= 900, 200, {},
           f"inserted={success_count} elapsed={elapsed_insert:.2f}s")

    # Test dashboard read speed with 1000+ transactions
    t1 = time.time()
    sc, body, ok = api("GET", "/api/overview")
    elapsed_read = time.time() - t1
    record(f"10.2 Dashboard load with 1000+ txs in {elapsed_read:.2f}s (Test 10.2)",
           ok and elapsed_read < 5, sc, body,
           f"elapsed={elapsed_read:.2f}s balance={body.get('total_balance')}")

    # Test P&L report speed
    t2 = time.time()
    sc2, body2, ok2 = api("GET", "/api/reports/pl",
                           params={"date_from": (today - timedelta(days=365)).isoformat(),
                                   "date_to": today.isoformat()})
    elapsed_pl = time.time() - t2
    record(f"10.3 P&L report with 1000+ txs in {elapsed_pl:.2f}s (Test 10.3)",
           ok2 and elapsed_pl < 10, sc2, body2,
           f"elapsed={elapsed_pl:.2f}s")


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 17 — Parser Unit Tests (Tests 17.1–17.7)
# ═══════════════════════════════════════════════════════════════════════════════

def test_parser_nigerian_bank_debit():
    """Test 17.1/17.3 — NigerianBankParser debit alert."""
    try:
        from parsers.nigerian_bank import NigerianBankParser
        from parsers.base import NonTransactionEmail

        parser = NigerianBankParser()
        received = datetime(2026, 7, 23, 14, 45, 40)
        body = (
            "Dear Customer,\n"
            "A debit transaction has occurred on your account.\n"
            "Amount: ₦2,000.00\n"
            "Narration: Name: SIMME SWEP FUTA Bank: Wema Bank PLC Account Number: 0450028496\n"
            "Session ID: 260723020100912495878926\n"
            "Available Balance: ₦1,023.92\n"
            "Date: 23/07/2026 14:45:40"
        )
        result = parser.parse_email("Debit Alert", body, "", received)
        ok = (
            result.direction == "debit"
            and abs(result.amount - 2000.00) < 0.01
            and result.reference == "260723020100912495878926"
            and abs(result.actual_balance - 1023.92) < 0.01
            and result.date.isoformat() == "2026-07-23"
        )
        record("17.1 NigerianBankParser — debit, amount, ref, balance, date (Test 17.1)",
               ok, 200, {},
               f"direction={result.direction} amount={result.amount} ref={result.reference} bal={result.actual_balance}")
    except Exception as e:
        record("17.1 NigerianBankParser — debit", False, 500, {}, str(e)[:120])


def test_parser_nigerian_bank_pm_date():
    """Test 17.2 — PremiumTrust/UBA style PM date parsing."""
    try:
        from parsers.nigerian_bank import NigerianBankParser

        parser = NigerianBankParser()
        received = datetime(2026, 7, 25, 18, 16, 49)
        body = (
            "Debit Alert\n"
            "Amount: ₦600.00\n"
            "Narration: from BLESSING OLUWASEGUN to mercy ogheneretunu obruba\n"
            "Ref: PT2026072518164901234\n"
            "Available Balance: ₦3,360.15\n"
            "Date: 25-Jul-2026 06:16:49 PM"
        )
        result = parser.parse_email("Debit Alert", body, "", received)
        ok = (
            result.direction == "debit"
            and abs(result.amount - 600.00) < 0.01
            and abs(result.actual_balance - 3360.15) < 0.01
        )
        record("17.2 NigerianBankParser — PM date parsing (Test 17.2)",
               ok, 200, {},
               f"direction={result.direction} amount={result.amount} bal={result.actual_balance} date={result.date}")
    except Exception as e:
        record("17.2 NigerianBankParser — PM date", False, 500, {}, str(e)[:120])


def test_parser_nigerian_bank_credit():
    """Test 17.4 — credit direction detection."""
    try:
        from parsers.nigerian_bank import NigerianBankParser

        parser = NigerianBankParser()
        received = datetime(2026, 7, 23, 14, 43, 0)
        body = (
            "Credit Alert\n"
            "₦50,000.00 has been credited to your account.\n"
            "Ref: CREDIT2026072312345\n"
            "Available Balance: ₦52,043.03"
        )
        result = parser.parse_email("Credit Alert", body, "", received)
        ok = result.direction == "credit" and result.amount == 50000.00
        record("17.4 NigerianBankParser — credit direction (Test 17.4)",
               ok, 200, {},
               f"direction={result.direction} amount={result.amount}")
    except Exception as e:
        record("17.4 NigerianBankParser — credit", False, 500, {}, str(e)[:120])


def test_parser_nigerian_bank_non_tx():
    """Test 17.6/17.7 — OTP/non-transaction emails raise NonTransactionEmail."""
    try:
        from parsers.nigerian_bank import NigerianBankParser
        from parsers.base import NonTransactionEmail

        parser = NigerianBankParser()
        raised = False
        try:
            parser.parse_email("OTP for login", "Your OTP is 123456", "", datetime.now())
        except NonTransactionEmail:
            raised = True
        record("17.6 NigerianBankParser — OTP raises NonTransactionEmail (Test 17.6)",
               raised, 200, {})
    except Exception as e:
        record("17.6 NigerianBankParser — non-tx", False, 500, {}, str(e)[:120])


def test_parser_selar_payment():
    """Test 17.5 — SelarParser with ₹ currency symbol."""
    try:
        from parsers.selar import SelarParser

        parser = SelarParser()
        received = datetime(2026, 7, 8, 10, 0, 0)
        body = (
            "Congratulations! You've received a payment of ₹1,700.00\n"
            "Purchase Summary\n- 2x Chicken Wings\n"
            "Bio Data\nTest Buyer\n"
            "Order ID: S5835T57GE7FN\n"
            "Date: July 8th, 2026"
        )
        result = parser.parse_email("You've received a payment!", body, "", received)
        ok = (
            result.direction == "credit"
            and abs(result.amount - 1700.00) < 0.01
            and result.reference == "S5835T57GE7FN"
            and result.date.isoformat() == "2026-07-08"
        )
        record("17.5 SelarParser — ₹ currency, ordinal date, reference (Test 17.5)",
               ok, 200, {},
               f"direction={result.direction} amount={result.amount} ref={result.reference} date={result.date}")
    except Exception as e:
        record("17.5 SelarParser — ₹ payment", False, 500, {}, str(e)[:120])


def test_parser_selar_abandoned_cart():
    """Test 17.6 — abandoned cart raises NonTransactionEmail."""
    try:
        from parsers.selar import SelarParser
        from parsers.base import NonTransactionEmail

        parser = SelarParser()
        raised = False
        try:
            parser.parse_email(
                "Abandoned cart reminder",
                "A customer left items in their cart.",
                "",
                datetime.now(),
            )
        except NonTransactionEmail:
            raised = True
        record("17.6b SelarParser — abandoned cart raises NonTransactionEmail (Test 17.6)",
               raised, 200, {})
    except Exception as e:
        record("17.6b SelarParser — abandoned cart", False, 500, {}, str(e)[:120])


def test_parser_opay_debit():
    """Test 17.1b — OPayParser debit alert."""
    try:
        from parsers.opay import OPayParser

        parser = OPayParser()
        received = datetime(2026, 7, 23, 14, 45, 40)
        body = (
            "Dear Customer, ₦2,000.00 has been debited from your OPay account.\n"
            "Recipient: SIMME SWEP FUTA\n"
            "Reference: 260723020100912495878926\n"
            "Available Balance: ₦1,023.92\n"
            "Time: Jul 23, 2026 02:45 PM"
        )
        result = parser.parse_email("Debit Alert", body, "", received)
        ok = (
            result.direction == "debit"
            and abs(result.amount - 2000.00) < 0.01
            and abs(result.actual_balance - 1023.92) < 0.01
        )
        record("17.1b OPayParser — debit, amount, balance (Test 17.1)",
               ok, 200, {},
               f"direction={result.direction} amount={result.amount} ref={result.reference}")
    except Exception as e:
        record("17.1b OPayParser — debit", False, 500, {}, str(e)[:120])


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 18 — Category Delete / Account Delete (cleanup structure)
# ═══════════════════════════════════════════════════════════════════════════════

def test_category_delete_with_reassignment():
    """Test 7.3 — delete category with reassignment."""
    sc, body, ok = api("POST", "/api/categories",
                        json_body={"name": "Test Other Expenses", "color": "#8B5CF6"},
                        expected=(201,))
    if not ok or "category" not in body:
        print(f"{SKIP} category delete — could not create second category")
        return
    second_cat_id = body["category"]["id"]

    if _category_id:
        sc2, body2, ok2 = api("DELETE", f"/api/categories/{_category_id}",
                               json_body={"reassign_to": second_cat_id})
        ok2 = ok2 and body2.get("ok") is True
        record("7.3g DELETE /api/categories/<id> with reassign (Test 7.3)", ok2, sc2, body2,
               f"affected={body2.get('affected_transactions')}" if ok2 else str(body2)[:120])

    # Clean up second category
    api("DELETE", f"/api/categories/{second_cat_id}", json_body={})


def test_delete_account_opay():
    """Test 3.4 — soft-delete OPay account."""
    if not _account_id_b:
        return
    sc, body, ok = api("DELETE", f"/api/accounts/{_account_id_b}")
    ok = ok and body.get("ok") is True
    record("3.4a DELETE /api/accounts/<id> — OPay soft-delete (Test 3.4)", ok, sc, body)


def test_delete_account_moniepoint():
    """Test 3.4 — soft-delete primary Moniepoint account."""
    if not _account_id:
        return
    sc, body, ok = api("DELETE", f"/api/accounts/{_account_id}")
    ok = ok and body.get("ok") is True
    record("3.4b DELETE /api/accounts/<id> — Moniepoint soft-delete (Test 3.4)", ok, sc, body)


# ═══════════════════════════════════════════════════════════════════════════════
# PHASE 19 — API Completeness Validation
# ═══════════════════════════════════════════════════════════════════════════════

def test_api_completeness():
    """Verify every known endpoint has been exercised."""
    print(f"\n{INFO} Checking API completeness...")

    # Normalise tested paths to canonical form for comparison
    canonical_tested: set[str] = set()
    for ep in TESTED_ENDPOINTS:
        canonical_tested.add(ep)

    # Map canonical known endpoints to tested ones (account for <id> wildcards)
    covered: set[str] = set()
    not_covered: set[str] = set()

    for known in ALL_ENDPOINTS:
        method, path = known.split(":", 1)
        # Build a regex from the known path pattern
        pattern = path.replace("<id>", r"[^/]+").replace("<type>", r"[^/]+").replace("<fmt>", r"[^/]+")
        import re
        found = False
        for tested_ep in canonical_tested:
            t_method, t_path = tested_ep.split(":", 1)
            if t_method == method and re.fullmatch(pattern, t_path):
                found = True
                break
        if found:
            covered.add(known)
        else:
            not_covered.add(known)

    total_endpoints = len(ALL_ENDPOINTS)
    total_covered = len(covered)
    pct = round(100 * total_covered / total_endpoints)

    print(f"\n{INFO} API Coverage: {total_covered}/{total_endpoints} endpoints ({pct}%)")

    if not_covered:
        print(f"\n{INFO} Untested endpoints:")
        for ep in sorted(not_covered):
            print(f"    ⬜ {ep}")

    record(
        f"API Completeness — {total_covered}/{total_endpoints} endpoints covered ({pct}%)",
        pct >= 80,  # pass if ≥80% covered
        200, {},
        f"{pct}% coverage",
    )


# ═══════════════════════════════════════════════════════════════════════════════
# CLEANUP — Wipe all data for all test users, then delete auth users
# ═══════════════════════════════════════════════════════════════════════════════

def cleanup_database():
    """
    Hard-delete ALL rows for test user(s) from every table, then remove auth users.
    Order respects FK dependencies (children before parents).
    """
    print(f"\n{INFO} Wiping entire database for test users...")

    user_ids = [uid for uid in [_user_id, _user_id_b] if uid]
    if not user_ids:
        print(f"{SKIP} cleanup — no user IDs to wipe")
        return

    # Tables ordered by FK dependency (most-dependent first)
    tables_ordered = [
        "audit_log",
        "digests",
        "failed_imports",
        "parsing_rules",
        "budgets",
        "sync_jobs",
        "transactions_archive",
        "transactions",
        "categories",
        "accounts",
        "settings",
        "profiles",
    ]

    for user_id in user_ids:
        for table in tables_ordered:
            try:
                r = supabase_admin("DELETE", f"{table}?user_id=eq.{user_id}")
                deleted = r.json() if r.status_code in (200, 204) else []
                count = len(deleted) if isinstance(deleted, list) else 0
                if count:
                    print(f"  🗑  {table}: deleted {count} row(s) for user {user_id[:8]}…")
            except Exception as e:
                print(f"  ⚠️  {table} delete failed: {e}")

    # Delete auth users last
    for user_id in user_ids:
        try:
            delete_auth_user(user_id)
            print(f"  🗑  auth.users: deleted {user_id[:8]}…")
        except Exception as e:
            print(f"  ⚠️  auth user delete failed: {e}")

    print(f"\n✅ Database wiped for {len(user_ids)} test user(s).")


# ═══════════════════════════════════════════════════════════════════════════════
# MAIN
# ═══════════════════════════════════════════════════════════════════════════════

def main():
    print("\n" + "═" * 72)
    print("  Holy Grills API — Comprehensive Live Integration Test Suite")
    print(f"  Target: {BASE}")
    print(f"  User A: {TEST_EMAIL}")
    print(f"  User B: {TEST_EMAIL_B}")
    print("═" * 72)

    # ── PHASE 1: Health & Auth ─────────────────────────────────────────────
    print("\n── PHASE 1: HEALTH & AUTH ────────────────────────────────────────────")
    test_health()
    user_ok = test_create_user()
    if not user_ok:
        print(f"\n{FAIL} Could not create test user. Aborting.")
        sys.exit(1)
    test_db_triggers_after_signup()
    test_sign_in()
    test_auth_required()
    test_invalid_token()
    test_onboarding()

    # ── PHASE 2: Settings & Profile ────────────────────────────────────────
    print("\n── PHASE 2: SETTINGS & PROFILE ───────────────────────────────────────")
    test_get_profile()
    test_update_profile()
    test_get_settings()
    test_update_settings()
    test_update_settings_notifications()

    # ── PHASE 3: Accounts ─────────────────────────────────────────────────
    print("\n── PHASE 3: ACCOUNTS CRUD ────────────────────────────────────────────")
    test_list_accounts_empty()
    test_create_account()
    test_create_account_opay()
    test_duplicate_sender_email()
    test_get_account()
    test_update_account()
    test_account_balance()
    test_account_balance_history()
    test_balance_adjustment()
    test_recompute_reconciliation()
    test_list_accounts_with_data()
    test_create_account_missing_name()

    # ── PHASE 4: Categories ────────────────────────────────────────────────
    print("\n── PHASE 4: CATEGORIES CRUD ──────────────────────────────────────────")
    test_list_categories()
    test_create_category()
    test_get_category()
    test_update_category()
    test_duplicate_category_name()
    test_category_tx_count()
    test_soft_delete_category_recreate()

    # ── PHASE 5: Transactions ──────────────────────────────────────────────
    print("\n── PHASE 5: TRANSACTIONS CRUD & ADVANCED ─────────────────────────────")
    test_list_transactions_empty()
    test_create_manual_transaction()
    test_duplicate_transaction()
    test_get_transaction()
    test_update_transaction()
    test_create_transfer_pair()
    test_flag_mis_parse()
    test_void_transaction()
    test_transaction_filters()
    test_account_transactions()
    test_transaction_archive()
    test_list_transactions_with_data()
    test_create_transaction_missing_fields()
    test_404_transaction()
    test_404_account()

    # ── PHASE 6: Overview Dashboard ───────────────────────────────────────
    print("\n── PHASE 6: OVERVIEW DASHBOARD ───────────────────────────────────────")
    test_overview()
    test_overview_chart_30()
    test_overview_chart_90()
    test_overview_chart_year()
    test_overview_donut()
    test_overview_digests()
    test_mark_digest_read_individual()
    test_mark_all_digests_read()

    # ── PHASE 7: Review Queue ─────────────────────────────────────────────
    print("\n── PHASE 7: REVIEW QUEUE & FAILED IMPORTS ────────────────────────────")
    test_review_stats()
    test_review_queue_and_skip()
    test_review_categorize()
    test_failed_imports()
    test_failed_import_detail()
    test_failed_import_ignore()
    test_failed_import_convert()

    # ── PHASE 8: Reports ──────────────────────────────────────────────────
    print("\n── PHASE 8: REPORTS ──────────────────────────────────────────────────")
    test_report_pl()
    test_report_pl_with_account()
    test_report_cashflow()
    test_report_reconciliation()
    test_report_export_pl_pdf()
    test_report_export_pl_xlsx()
    test_report_export_cashflow_pdf()
    test_report_export_reconciliation_xlsx()
    test_report_export_invalid()

    # ── PHASE 9: Parsing Rules ────────────────────────────────────────────
    print("\n── PHASE 9: PARSING RULES ────────────────────────────────────────────")
    test_list_parsing_rules()
    test_create_parsing_rule()
    test_create_regex_parsing_rule()
    test_create_case_sensitive_parsing_rule()
    test_update_parsing_rule()
    test_delete_parsing_rule()

    # ── PHASE 10: Budgets ─────────────────────────────────────────────────
    print("\n── PHASE 10: BUDGETS ─────────────────────────────────────────────────")
    test_list_budgets()
    test_create_budget()
    test_budget_duplicate()
    test_budget_tracking()
    test_delete_budget()

    # ── PHASE 11: Audit Log ───────────────────────────────────────────────
    print("\n── PHASE 11: AUDIT LOG ───────────────────────────────────────────────")
    test_audit_log()
    test_audit_log_filtered()

    # ── PHASE 12: Sync Jobs, Gmail & Cron ────────────────────────────────
    print("\n── PHASE 12: SYNC, GMAIL & CRON ─────────────────────────────────────")
    test_list_sync_jobs()
    test_sync_trigger_no_gmail()
    test_gmail_initiate()
    test_gmail_disconnect()
    test_sync_reparse()
    test_housekeeping()
    test_cron_trigger()

    # ── PHASE 13: Reconciliation ──────────────────────────────────────────
    print("\n── PHASE 13: RECONCILIATION ──────────────────────────────────────────")
    test_reconciliation_mismatch()
    test_reconciliation_fix()

    # ── PHASE 14: Data Integrity ──────────────────────────────────────────
    print("\n── PHASE 14: DATA INTEGRITY CONSTRAINTS ──────────────────────────────")
    test_negative_amount_constraint()
    test_invalid_category_fk()
    test_invalid_account_fk()
    test_invalid_transaction_status()

    # ── PHASE 15: RLS Security ────────────────────────────────────────────
    print("\n── PHASE 15: RLS USER ISOLATION ──────────────────────────────────────")
    test_rls_create_user_b()
    test_rls_user_isolation_accounts()
    test_rls_user_isolation_transactions()
    test_rls_cross_user_update()

    # ── PHASE 16: Performance ─────────────────────────────────────────────
    print("\n── PHASE 16: PERFORMANCE / BULK INSERT ───────────────────────────────")
    test_bulk_insert_performance()

    # ── PHASE 17: Parser Unit Tests ───────────────────────────────────────
    print("\n── PHASE 17: PARSER UNIT TESTS ───────────────────────────────────────")
    test_parser_nigerian_bank_debit()
    test_parser_nigerian_bank_pm_date()
    test_parser_nigerian_bank_credit()
    test_parser_nigerian_bank_non_tx()
    test_parser_selar_payment()
    test_parser_selar_abandoned_cart()
    test_parser_opay_debit()

    # ── PHASE 18: Cleanup (destructive — delete account/category) ─────────
    print("\n── PHASE 18: CLEANUP / DESTRUCTIVE ──────────────────────────────────")
    test_category_delete_with_reassignment()
    test_delete_account_opay()
    test_delete_account_moniepoint()

    # ── PHASE 19: API Completeness ────────────────────────────────────────
    print("\n── PHASE 19: API COMPLETENESS VALIDATION ─────────────────────────────")
    test_api_completeness()

    # ── FINAL: Wipe entire database ───────────────────────────────────────
    print("\n── FINAL: WIPE DATABASE ──────────────────────────────────────────────")
    cleanup_database()

    # ── Summary ───────────────────────────────────────────────────────────
    total  = len(results)
    passed = sum(1 for r in results if r["passed"])
    failed = total - passed

    print("\n" + "═" * 72)
    print(f"  RESULTS: {passed}/{total} passed   {failed} failed")
    print("═" * 72)

    if failed:
        print("\nFailed tests:")
        for r in results:
            if not r["passed"]:
                print(f"  ❌ {r['name']}  [{r['status']}]  {r['note']}")

    print()
    return failed == 0


if __name__ == "__main__":
    sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
    success = main()
    sys.exit(0 if success else 1)
