"""
Rule-based categorization engine.

Rule precedence (highest → lowest):
  1. parsing_rules for this sender, ordered by priority DESC, then pattern length DESC
     (manual rules with a higher priority integer win over auto-generated rules)
  2. Category keyword matching across all categories (global fallback)
"""
from __future__ import annotations

import re
from typing import Optional

from db import db_select, get_admin_client


def categorize_transaction(
    user_id: str,
    sender_email: str,
    narration: str,
    direction: str,
) -> tuple[Optional[str], bool]:
    """
    Try to find a matching category for the given narration.

    Returns (category_id, matched) where:
      - category_id is the UUID of the matched category, or None
      - matched is True if a rule was found, False → needs_review should be set
    """
    if not narration:
        return None, False

    narration_lower = narration.lower()

    # ── 1. Sender-specific parsing rules (most specific) ──────────────────────
    rules = _get_active_rules(user_id, sender_email)
    for rule in rules:
        if _rule_matches(rule, narration_lower):
            _increment_hit_count(rule["id"])
            return rule["category_id"], True

    # ── 2. Global category keyword fallback ────────────────────────────────────
    categories = db_select("categories", {"user_id": user_id})
    for cat in categories:
        # Skip soft-deleted categories
        if cat.get("deleted_at"):
            continue
        keywords: list = cat.get("keywords") or []
        applies_to = cat.get("applies_to", "both")
        direction_type = "income" if direction == "credit" else "expense"
        if applies_to not in ("both", direction_type):
            continue
        for kw in keywords:
            if kw and kw.lower() in narration_lower:
                return cat["id"], True

    return None, False


def apply_manual_categorization(
    user_id: str,
    transaction_id: str,
    category_id: str,
    narration: str,
    sender_email: str,
    also_create_rule: bool = True,
) -> None:
    """
    Apply a manual category to a transaction and optionally upsert a parsing_rule
    so the same narration auto-matches next time.
    """
    client = get_admin_client()

    client.table("transactions").update({
        "category_id": category_id,
        "status": "Categorized",
        "needs_review": False,
    }).eq("id", transaction_id).eq("user_id", user_id).execute()

    if also_create_rule and narration and sender_email:
        upsert_parsing_rule(
            user_id=user_id,
            sender_email=sender_email,
            pattern=narration.strip().lower()[:200],
            category_id=category_id,
            created_from="manual",
        )


def upsert_parsing_rule(
    user_id: str,
    sender_email: str,
    pattern: str,
    category_id: str,
    created_from: str = "auto",
    priority: int = 0,
) -> dict:
    """
    Insert or update a parsing rule. Bumps version on update.
    Manual rules default to priority=10 to beat auto-generated ones.
    """
    if created_from == "manual" and priority == 0:
        priority = 10

    client = get_admin_client()
    existing = (
        client.table("parsing_rules")
        .select("id,version,priority")
        .eq("user_id", user_id)
        .eq("sender_email", sender_email)
        .eq("pattern", pattern)
        .limit(1)
        .execute()
    )
    if existing.data:
        row = existing.data[0]
        result = (
            client.table("parsing_rules")
            .update({
                "category_id": category_id,
                "created_from": created_from,
                "version": row["version"] + 1,
                "is_active": True,
                # Only escalate priority, never silently lower it
                "priority": max(row.get("priority", 0), priority),
            })
            .eq("id", row["id"])
            .execute()
        )
        return result.data[0] if result.data else {}
    else:
        result = (
            client.table("parsing_rules")
            .insert({
                "user_id": user_id,
                "sender_email": sender_email,
                "pattern": pattern,
                "category_id": category_id,
                "created_from": created_from,
                "version": 1,
                "is_active": True,
                "priority": priority,
            })
            .execute()
        )
        return result.data[0] if result.data else {}


def _get_active_rules(user_id: str, sender_email: str) -> list[dict]:
    """
    Fetch active parsing rules for a user + sender.
    Order: priority DESC first, then pattern length DESC (more specific first).
    """
    client = get_admin_client()
    result = (
        client.table("parsing_rules")
        .select("id,pattern,category_id,is_regex,case_sensitive,priority")
        .eq("user_id", user_id)
        .eq("sender_email", sender_email)
        .eq("is_active", True)
        .execute()
    )
    rows = result.data or []
    # Sort: higher priority first, then longer patterns (more specific) first
    return sorted(
        rows,
        key=lambda r: (r.get("priority", 0), len(r.get("pattern", ""))),
        reverse=True,
    )


def _rule_matches(rule: dict, narration_lower: str) -> bool:
    """Test a single rule against the lowercased narration."""
    pattern = rule.get("pattern", "")
    is_regex = rule.get("is_regex", False)
    case_sensitive = rule.get("case_sensitive", False)

    if not pattern:
        return False

    pat = pattern if case_sensitive else pattern.lower()

    if is_regex:
        try:
            flags = 0 if case_sensitive else re.IGNORECASE
            return bool(re.search(pat, narration_lower, flags))
        except re.error:
            return False
    else:
        return pat in narration_lower


def _increment_hit_count(rule_id: str) -> None:
    """Best-effort atomic increment of rule hit_count via DB RPC."""
    try:
        from db import get_admin_client as _get
        _get().rpc("increment_rule_hit_count", {"p_rule_id": rule_id}).execute()
    except Exception:
        pass  # Non-critical; never block the transaction insert
