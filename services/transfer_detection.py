"""
Transfer Detection Service
==========================
After every sync, scans the user's transactions and automatically links
debit/credit pairs that match user-defined transfer rules.

How it works
------------
1. Load all active transfer_rules for the user.
2. For each rule, fetch unlinked debits matching the debit_pattern on from_account.
3. Fetch unlinked credits matching credit_pattern on to_account.
4. Match pairs by identical amount + timestamp proximity (within time_window_minutes).
5. Link each matched pair:
   - Set type = 'transfer' on both.
   - Set needs_review = False on both (they skip the Review Queue).
   - Set status = 'Categorized' on both.
   - Cross-link via is_transfer_pair_id.

The detection is idempotent — already-linked transactions (is_transfer_pair_id IS NOT NULL)
are excluded from every query, so re-running never creates duplicate links.
"""
from __future__ import annotations

import logging
from collections import defaultdict
from datetime import datetime, timezone
from typing import Optional

from db import get_admin_client

logger = logging.getLogger(__name__)


def detect_and_link_transfers(user_id: str) -> dict:
    """
    Run all active transfer rules for *user_id*.

    Returns:
        {"rules_checked": int, "pairs_linked": int, "errors": list[str]}
    """
    client = get_admin_client()

    rules = (
        client.table("transfer_rules")
        .select("*")
        .eq("user_id", user_id)
        .eq("is_active", True)
        .execute()
    ).data or []

    total_linked = 0
    errors: list[str] = []

    for rule in rules:
        try:
            linked = _apply_rule(client, user_id, rule)
            total_linked += linked
            if linked:
                logger.info(
                    "Transfer rule %s linked %d pair(s) for user %s",
                    rule.get("id"), linked, user_id,
                )
        except Exception as exc:
            logger.error("Transfer rule %s error: %s", rule.get("id"), exc)
            errors.append(str(exc))

    return {
        "rules_checked": len(rules),
        "pairs_linked": total_linked,
        "errors": errors,
    }


# ── Internal helpers ───────────────────────────────────────────────────────────

def _apply_rule(client, user_id: str, rule: dict) -> int:
    """Match and link unlinked transaction pairs for one rule. Returns pairs linked."""
    from_account_id: Optional[str] = rule.get("from_account_id")
    to_account_id: Optional[str]   = rule.get("to_account_id")
    debit_pattern: str  = (rule.get("debit_pattern") or "").strip()
    credit_pattern: str = (rule.get("credit_pattern") or "").strip()
    window_minutes: int = int(rule.get("time_window_minutes") or 60)

    if not debit_pattern or not credit_pattern:
        return 0

    # ── Fetch candidate debits ────────────────────────────────────────────────
    debit_q = (
        client.table("transactions")
        .select("id,amount,date,date_time")
        .eq("user_id", user_id)
        .eq("direction", "debit")
        .is_("is_transfer_pair_id", "null")
        .is_("voided_at", "null")
        .ilike("narration", f"%{debit_pattern}%")
        .order("date", desc=True)
        .limit(500)
    )
    if from_account_id:
        debit_q = debit_q.eq("account_id", from_account_id)

    debits = debit_q.execute().data or []
    if not debits:
        return 0

    # ── Fetch candidate credits ───────────────────────────────────────────────
    credit_q = (
        client.table("transactions")
        .select("id,amount,date,date_time")
        .eq("user_id", user_id)
        .eq("direction", "credit")
        .is_("is_transfer_pair_id", "null")
        .is_("voided_at", "null")
        .ilike("narration", f"%{credit_pattern}%")
        .order("date", desc=True)
        .limit(500)
    )
    if to_account_id:
        credit_q = credit_q.eq("account_id", to_account_id)

    credits = credit_q.execute().data or []
    if not credits:
        return 0

    # ── Index credits by amount for O(1) lookup ───────────────────────────────
    credits_by_amount: dict[float, list[dict]] = defaultdict(list)
    for c in credits:
        credits_by_amount[round(float(c["amount"]), 2)].append(c)

    used_credit_ids: set[str] = set()
    linked_count = 0

    for debit in debits:
        amount_key = round(float(debit["amount"]), 2)
        candidates = credits_by_amount.get(amount_key, [])

        best_credit = None
        best_delta: Optional[float] = None
        debit_dt = _parse_dt(debit.get("date_time") or debit.get("date"))

        for credit in candidates:
            if credit["id"] in used_credit_ids:
                continue
            credit_dt = _parse_dt(credit.get("date_time") or credit.get("date"))
            delta_min = abs((credit_dt - debit_dt).total_seconds()) / 60.0
            if delta_min <= window_minutes:
                if best_delta is None or delta_min < best_delta:
                    best_credit = credit
                    best_delta = delta_min

        if best_credit:
            used_credit_ids.add(best_credit["id"])
            _link_pair(client, debit["id"], best_credit["id"])
            linked_count += 1

    return linked_count


def _link_pair(client, debit_id: str, credit_id: str) -> None:
    """
    Cross-link a debit and credit as a matched transfer pair.
    Both transactions are marked type=transfer, needs_review=False, status=Categorized.
    """
    common = {
        "type": "transfer",
        "needs_review": False,
        "status": "Categorized",
    }
    client.table("transactions").update(
        {**common, "is_transfer_pair_id": credit_id}
    ).eq("id", debit_id).execute()

    client.table("transactions").update(
        {**common, "is_transfer_pair_id": debit_id}
    ).eq("id", credit_id).execute()


def _parse_dt(dt_str: Optional[str]) -> datetime:
    """
    Parse an ISO-8601 datetime string or a YYYY-MM-DD date string to a
    timezone-aware datetime (UTC).  Falls back to now() on any failure.
    """
    if not dt_str:
        return datetime.now(timezone.utc)
    try:
        # Supabase returns TIMESTAMPTZ as ISO 8601 with +00:00 or Z suffix.
        s = dt_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, AttributeError):
        pass
    # Date-only (YYYY-MM-DD) → midnight UTC
    try:
        from datetime import date as dt_date
        d = dt_date.fromisoformat(dt_str[:10])
        return datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    except (ValueError, AttributeError):
        return datetime.now(timezone.utc)
