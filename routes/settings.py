"""
Settings endpoints.
User settings, parsing rules management, budget management, audit log.
"""
from __future__ import annotations

from flask import Blueprint, g, jsonify, request

from auth_middleware import require_auth
from db import get_admin_client
from services.audit import get_audit_log, log_audit
from services.categorization import upsert_parsing_rule

bp = Blueprint("settings", __name__, url_prefix="/api/settings")


# ── User settings ──────────────────────────────────────────────────────────────

@bp.get("")
@require_auth
def get_settings():
    """Return the user's settings row, with frontend-compatible field aliases."""
    client = get_admin_client()
    rows = (
        client.table("settings")
        .select("*")
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Settings not found"}), 404
    row = dict(rows[0])
    # Add aliases so the frontend's camelCase normaliser maps to the right fields:
    # reconciliation_threshold → reconciliationThreshold
    # notification_digest_enabled → notificationDigestEnabled
    row["reconciliation_threshold"] = row.get("reconciliation_threshold_absolute")
    row["notification_digest_enabled"] = row.get("digest_enabled")
    return jsonify({"settings": row})


@bp.patch("")
@require_auth
def update_settings():
    """
    Update user settings.
    Body: {reconciliation_threshold_absolute?, reconciliation_threshold_percent?,
           digest_enabled?, digest_time?, notification_email?, timezone?}
    """
    user_id = g.user_id
    client = get_admin_client()
    data = request.get_json(silent=True) or {}

    # Accept frontend aliases (camelCase-denormalised names) and map to real columns
    field_aliases = {
        "reconciliation_threshold": "reconciliation_threshold_absolute",
        "notification_digest_enabled": "digest_enabled",
    }
    for alias, real_name in field_aliases.items():
        if alias in data and real_name not in data:
            data[real_name] = data.pop(alias)

    allowed = [
        "reconciliation_threshold_absolute",
        "reconciliation_threshold_percent",
        "digest_enabled",
        "digest_time",
        "notification_email",
        "timezone",
    ]
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    # Validate threshold values
    if "reconciliation_threshold_absolute" in updates:
        try:
            updates["reconciliation_threshold_absolute"] = float(updates["reconciliation_threshold_absolute"])
        except (TypeError, ValueError):
            return jsonify({"error": "reconciliation_threshold_absolute must be a number"}), 400

    if "reconciliation_threshold_percent" in updates:
        try:
            pct = float(updates["reconciliation_threshold_percent"])
            if not 0 < pct <= 1:
                return jsonify({"error": "reconciliation_threshold_percent must be between 0 and 1"}), 400
            updates["reconciliation_threshold_percent"] = pct
        except (TypeError, ValueError):
            return jsonify({"error": "reconciliation_threshold_percent must be a number"}), 400

    result = (
        client.table("settings")
        .update(updates)
        .eq("user_id", user_id)
        .execute()
    )
    log_audit(
        user_id=user_id,
        action_type="settings_updated",
        description=f"Settings updated: {list(updates.keys())}",
        ip_address=request.remote_addr,
    )
    row = dict(result.data[0]) if result.data else {}
    # Return the same aliases as GET so the frontend normaliser maps correctly
    row["reconciliation_threshold"] = row.get("reconciliation_threshold_absolute")
    row["notification_digest_enabled"] = row.get("digest_enabled")
    return jsonify({"settings": row})


# ── User profile ───────────────────────────────────────────────────────────────

@bp.get("/profile")
@require_auth
def get_profile():
    client = get_admin_client()
    rows = (
        client.table("profiles")
        .select("*")
        .eq("id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Profile not found"}), 404
    return jsonify({"profile": rows[0]})


@bp.patch("/profile")
@require_auth
def update_profile():
    data = request.get_json() or {}
    # Accept camelCase aliases sent by the frontend after denormalisation
    field_map = {
        "full_name":    "full_name",
        "display_name": "full_name",   # frontend sends displayName → display_name
    }
    updates: dict = {}
    for k, v in data.items():
        col = field_map.get(k)
        if col and v is not None:
            updates[col] = v
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400
    client = get_admin_client()
    result = client.table("profiles").update(updates).eq("id", g.user_id).execute()
    return jsonify({"profile": result.data[0] if result.data else {}})


# ── Parsing rules ──────────────────────────────────────────────────────────────

@bp.get("/parsing-rules")
@require_auth
def list_parsing_rules():
    """Return all parsing rules for the user."""
    client = get_admin_client()
    rows = (
        client.table("parsing_rules")
        .select("*,categories(name,color)")
        .eq("user_id", g.user_id)
        .order("sender_email", desc=False)
        .order("hit_count", desc=True)
        .execute()
    ).data or []
    # Add `sender` alias so the frontend can access rule.sender / rule.senderEmail
    for row in rows:
        row["sender"] = row.get("sender_email", "")
    return jsonify({"parsing_rules": rows, "total": len(rows)})


@bp.post("/parsing-rules")
@require_auth
def create_parsing_rule():
    """
    Manually create a parsing rule.
    Body: {sender_email|sender, pattern, category_id, is_regex?}
    Accepts 'sender' as an alias for 'sender_email' (frontend compatibility).
    """
    data = request.get_json(silent=True) or {}
    # Accept 'sender' as alias for 'sender_email' (frontend sends 'sender')
    sender_email = (data.get("sender_email") or data.get("sender", "")).strip()
    if not sender_email:
        return jsonify({"error": "'sender_email' is required"}), 400
    if not data.get("pattern"):
        return jsonify({"error": "'pattern' is required"}), 400
    if not data.get("category_id"):
        return jsonify({"error": "'category_id' is required"}), 400

    rule = upsert_parsing_rule(
        user_id=g.user_id,
        sender_email=sender_email,
        pattern=data["pattern"],
        category_id=data["category_id"],
        created_from="manual",
    )
    log_audit(
        user_id=g.user_id,
        action_type="parsing_rule_created",
        entity_type="parsing_rule",
        entity_id=rule.get("id"),
        description=f"Parsing rule created: '{data['pattern']}' → {data['category_id']}",
        ip_address=request.remote_addr,
    )
    rule["sender"] = rule.get("sender_email", "")
    return jsonify({"parsing_rule": rule}), 201


@bp.patch("/parsing-rules/<rule_id>")
@require_auth
def update_parsing_rule(rule_id):
    client = get_admin_client()
    rows = (
        client.table("parsing_rules")
        .select("id")
        .eq("id", rule_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Rule not found"}), 404

    data = request.get_json() or {}
    allowed = ["pattern", "category_id", "is_active", "is_regex", "case_sensitive"]
    updates = {k: v for k, v in data.items() if k in allowed and v is not None}
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    # Bump version on update
    ver_row = (
        client.table("parsing_rules")
        .select("version")
        .eq("id", rule_id)
        .limit(1)
        .execute()
    ).data
    if ver_row:
        updates["version"] = ver_row[0]["version"] + 1

    result = client.table("parsing_rules").update(updates).eq("id", rule_id).execute()
    log_audit(
        user_id=g.user_id,
        action_type="parsing_rule_updated",
        entity_type="parsing_rule",
        entity_id=rule_id,
        description=f"Parsing rule updated",
        ip_address=request.remote_addr,
    )
    rule = result.data[0] if result.data else {}
    if rule:
        rule["sender"] = rule.get("sender_email", "")
    return jsonify({"parsing_rule": rule})


@bp.delete("/parsing-rules/<rule_id>")
@require_auth
def delete_parsing_rule(rule_id):
    client = get_admin_client()
    rows = (
        client.table("parsing_rules")
        .select("id")
        .eq("id", rule_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Rule not found"}), 404
    client.table("parsing_rules").delete().eq("id", rule_id).execute()
    return jsonify({"ok": True})


@bp.post("/parsing-rules/seed")
@require_auth
def seed_parsing_rules():
    """
    Seed default parsing rules for common Nigerian bank / payment senders.
    Safe to call multiple times — skips rules that already exist for the user.
    """
    client = get_admin_client()
    user_id = g.user_id

    # Look up the user's categories by name so we can assign sensible defaults
    cats = (
        client.table("categories")
        .select("id,name")
        .eq("user_id", user_id)
        .execute()
    ).data or []
    cat_map = {c["name"].lower(): c["id"] for c in cats}

    def _cat(preferred: list[str]) -> str | None:
        for name in preferred:
            if name.lower() in cat_map:
                return cat_map[name.lower()]
        # fall back to first available category
        return cats[0]["id"] if cats else None

    DEFAULT_RULES = [
        # Moniepoint
        {"sender_email": "alerts@moniepoint.com",      "pattern": "debit|credit|transfer|transaction", "cats": ["bank charges", "transfers", "miscellaneous"]},
        {"sender_email": "noreply@moniepoint.com",     "pattern": "debit|credit|transfer|transaction", "cats": ["bank charges", "transfers", "miscellaneous"]},
        {"sender_email": "no-reply@moniepoint.com",    "pattern": "debit|credit|transfer|transaction", "cats": ["bank charges", "transfers", "miscellaneous"]},
        # OPay
        {"sender_email": "noreply@opay.com",           "pattern": "debit|credit|transfer|transaction", "cats": ["bank charges", "transfers", "miscellaneous"]},
        {"sender_email": "alerts@opay.com",            "pattern": "debit|credit|transfer|transaction", "cats": ["bank charges", "transfers", "miscellaneous"]},
        # UBA
        {"sender_email": "alerts@ubagroup.com",        "pattern": "debit|credit|transfer",             "cats": ["bank charges", "transfers", "miscellaneous"]},
        {"sender_email": "no-reply@ubagroup.com",      "pattern": "debit|credit|transfer",             "cats": ["bank charges", "transfers", "miscellaneous"]},
        # GTBank
        {"sender_email": "alerts@gtbank.com",          "pattern": "debit|credit",                      "cats": ["bank charges", "transfers", "miscellaneous"]},
        {"sender_email": "no-reply@gtbank.com",        "pattern": "debit|credit",                      "cats": ["bank charges", "transfers", "miscellaneous"]},
        # Access Bank
        {"sender_email": "alerts@accessbankplc.com",   "pattern": "debit|credit|transfer",             "cats": ["bank charges", "transfers", "miscellaneous"]},
        # First Bank
        {"sender_email": "alerts@firstbanknigeria.com","pattern": "debit|credit|transfer",             "cats": ["bank charges", "transfers", "miscellaneous"]},
        # Zenith Bank
        {"sender_email": "alerts@zenithbank.com",      "pattern": "debit|credit|transfer",             "cats": ["bank charges", "transfers", "miscellaneous"]},
        # Kuda
        {"sender_email": "noreply@kuda.com",           "pattern": "debit|credit|transfer",             "cats": ["bank charges", "transfers", "miscellaneous"]},
        # Selar
        {"sender_email": "noreply@selar.co",           "pattern": "sale|payment|order|purchase",       "cats": ["sales revenue", "revenue", "income"]},
        {"sender_email": "hello@selar.co",             "pattern": "sale|payment|order|purchase",       "cats": ["sales revenue", "revenue", "income"]},
        # Paystack
        {"sender_email": "no-reply@paystack.com",      "pattern": "payment|charge|transfer|settled",   "cats": ["sales revenue", "revenue", "income"]},
        {"sender_email": "notifications@paystack.com", "pattern": "payment|charge|transfer|settled",   "cats": ["sales revenue", "revenue", "income"]},
        # Flutterwave
        {"sender_email": "noreply@flutterwave.com",    "pattern": "payment|charge|transfer|payout",    "cats": ["sales revenue", "revenue", "income"]},
        {"sender_email": "no-reply@flutterwave.com",   "pattern": "payment|charge|transfer|payout",    "cats": ["sales revenue", "revenue", "income"]},
        # Payaza
        {"sender_email": "noreply@payaza.africa",      "pattern": "payment|transaction|transfer",      "cats": ["sales revenue", "revenue", "income"]},
    ]

    # Fetch existing rules to skip duplicates
    existing = (
        client.table("parsing_rules")
        .select("sender_email")
        .eq("user_id", user_id)
        .execute()
    ).data or []
    existing_emails = {r["sender_email"] for r in existing}

    created = 0
    skipped = 0
    for rule in DEFAULT_RULES:
        email = rule["sender_email"]
        if email in existing_emails:
            skipped += 1
            continue
        category_id = _cat(rule["cats"])
        if not category_id:
            skipped += 1
            continue
        try:
            upsert_parsing_rule(
                user_id=user_id,
                sender_email=email,
                pattern=rule["pattern"],
                category_id=category_id,
                created_from="auto",
            )
            existing_emails.add(email)
            created += 1
        except Exception:
            skipped += 1

    return jsonify({"ok": True, "created": created, "skipped": skipped})


# ── Budgets ────────────────────────────────────────────────────────────────────

@bp.get("/budgets")
@require_auth
def list_budgets():
    client = get_admin_client()
    rows = (
        client.table("budgets")
        .select("*,categories(name,color)")
        .eq("user_id", g.user_id)
        .order("period_year", desc=True)
        .order("period_month", desc=True)
        .execute()
    ).data or []
    return jsonify({"budgets": rows})


@bp.post("/budgets")
@require_auth
def create_or_update_budget():
    """Upsert a budget for a category + month + year."""
    data = request.get_json() or {}
    for field in ["category_id", "amount", "period_month", "period_year"]:
        if data.get(field) is None:
            return jsonify({"error": f"'{field}' is required"}), 400

    client = get_admin_client()
    # Check for existing budget for this user/category/period → 409 on duplicate
    existing = (
        client.table("budgets")
        .select("id")
        .eq("user_id", g.user_id)
        .eq("category_id", data["category_id"])
        .eq("period_month", int(data["period_month"]))
        .eq("period_year", int(data["period_year"]))
        .limit(1)
        .execute()
    ).data
    if existing:
        return jsonify({"error": "A budget for this category and period already exists"}), 409

    row = {
        "user_id": g.user_id,
        "category_id": data["category_id"],
        "amount": float(data["amount"]),
        "period_month": int(data["period_month"]),
        "period_year": int(data["period_year"]),
    }
    result = client.table("budgets").insert(row).execute()
    log_audit(
        user_id=g.user_id,
        action_type="budget_created",
        entity_type="budget",
        entity_id=result.data[0]["id"] if result.data else None,
        description=f"Budget upserted: {data['period_year']}-{data['period_month']:02d}",
        ip_address=request.remote_addr,
    )
    return jsonify({"budget": result.data[0] if result.data else {}}), 201


@bp.delete("/budgets/<budget_id>")
@require_auth
def delete_budget(budget_id):
    client = get_admin_client()
    client.table("budgets").delete().eq("id", budget_id).eq("user_id", g.user_id).execute()
    return jsonify({"ok": True})


# ── Transfer Rules ─────────────────────────────────────────────────────────────

@bp.get("/transfer-rules")
@require_auth
def list_transfer_rules():
    """Return all transfer detection rules for the user."""
    client = get_admin_client()
    rows = (
        client.table("transfer_rules")
        .select(
            "*,"
            "from_account:from_account_id(id,name),"
            "to_account:to_account_id(id,name)"
        )
        .eq("user_id", g.user_id)
        .order("created_at", desc=False)
        .execute()
    ).data or []
    return jsonify({"transfer_rules": rows})


@bp.post("/transfer-rules")
@require_auth
def create_transfer_rule():
    """
    Create a transfer detection rule.
    Body: {name?, from_account_id?, debit_pattern, to_account_id?, credit_pattern, time_window_minutes?}
    """
    client = get_admin_client()
    data = request.get_json(silent=True) or {}

    debit_pattern  = (data.get("debit_pattern") or "").strip()
    credit_pattern = (data.get("credit_pattern") or "").strip()
    if not debit_pattern:
        return jsonify({"error": "'debit_pattern' is required"}), 400
    if not credit_pattern:
        return jsonify({"error": "'credit_pattern' is required"}), 400

    try:
        window = int(data.get("time_window_minutes") or 60)
        if window < 1:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "'time_window_minutes' must be a positive integer"}), 400

    row = {
        "user_id":              g.user_id,
        "name":                 (data.get("name") or "").strip() or None,
        "from_account_id":      data.get("from_account_id") or None,
        "debit_pattern":        debit_pattern,
        "to_account_id":        data.get("to_account_id") or None,
        "credit_pattern":       credit_pattern,
        "time_window_minutes":  window,
        "is_active":            True,
    }
    result = client.table("transfer_rules").insert(row).execute()
    created = result.data[0] if result.data else row
    log_audit(
        user_id=g.user_id,
        action_type="settings_updated",
        description=f"Transfer rule created: '{debit_pattern}' → '{credit_pattern}'",
        ip_address=request.remote_addr,
    )
    return jsonify({"transfer_rule": created}), 201


@bp.put("/transfer-rules/<rule_id>")
@require_auth
def update_transfer_rule(rule_id):
    """Update an existing transfer rule."""
    client = get_admin_client()
    rows = (
        client.table("transfer_rules")
        .select("id")
        .eq("id", rule_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Rule not found"}), 404

    data = request.get_json(silent=True) or {}
    allowed = [
        "name", "from_account_id", "debit_pattern",
        "to_account_id", "credit_pattern", "time_window_minutes", "is_active",
    ]
    updates = {k: v for k, v in data.items() if k in allowed}
    if not updates:
        return jsonify({"error": "Nothing to update"}), 400

    if "time_window_minutes" in updates:
        try:
            updates["time_window_minutes"] = int(updates["time_window_minutes"])
        except (TypeError, ValueError):
            return jsonify({"error": "'time_window_minutes' must be an integer"}), 400

    result = client.table("transfer_rules").update(updates).eq("id", rule_id).execute()
    return jsonify({"transfer_rule": result.data[0] if result.data else {}})


@bp.delete("/transfer-rules/<rule_id>")
@require_auth
def delete_transfer_rule(rule_id):
    """Delete a transfer detection rule."""
    client = get_admin_client()
    rows = (
        client.table("transfer_rules")
        .select("id")
        .eq("id", rule_id)
        .eq("user_id", g.user_id)
        .limit(1)
        .execute()
    ).data
    if not rows:
        return jsonify({"error": "Rule not found"}), 404
    client.table("transfer_rules").delete().eq("id", rule_id).execute()
    return jsonify({"ok": True})


# ── Audit log ──────────────────────────────────────────────────────────────────

@bp.get("/audit-log")
@require_auth
def view_audit_log():
    """
    Read-only audit log.
    Query params: limit, offset, action_type, entity_type, entity_id
    """
    limit = min(int(request.args.get("limit", 50)), 200)
    offset = int(request.args.get("offset", 0))
    entries = get_audit_log(
        user_id=g.user_id,
        limit=limit,
        offset=offset,
        entity_type=request.args.get("entity_type"),
        entity_id=request.args.get("entity_id"),
        action_type=request.args.get("action_type"),
    )
    return jsonify({"audit_log": entries, "limit": limit, "offset": offset})
