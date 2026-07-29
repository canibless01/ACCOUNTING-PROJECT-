"""
Overview / Dashboard endpoints.
Provides all aggregate data the Overview page needs in a single call.
"""
from __future__ import annotations

from datetime import date, timedelta

from flask import Blueprint, g, jsonify, request

from auth_middleware import require_auth
from db import get_admin_client
from services.balance import get_all_balances, get_total_balance
from services.digest import get_unread_digests, mark_all_digests_read, mark_digest_read

bp = Blueprint("overview", __name__, url_prefix="/api/overview")


@bp.get("")
@require_auth
def get_overview():
    """
    Combined overview dashboard endpoint.
    Returns: total_balance, today's income/expense/net, recent 10 transactions,
             needs_review count, unread digest count.
    """
    user_id = g.user_id
    client = get_admin_client()
    today = date.today().isoformat()

    # ── Total balance across all accounts ─────────────────────────────────────
    accounts = get_all_balances(user_id)
    total_balance = sum(float(a.get("calculated_balance") or 0) for a in accounts)

    # ── Today's income and expense ─────────────────────────────────────────────
    today_txs = (
        client.table("transactions")
        .select("direction,amount,type")
        .eq("user_id", user_id)
        .eq("date", today)
        .neq("type", "transfer")
        .neq("status", "Raw")
        .execute()
    ).data or []

    today_income = sum(float(t["amount"]) for t in today_txs if t["direction"] == "credit")
    today_expense = sum(float(t["amount"]) for t in today_txs if t["direction"] == "debit")
    today_net = today_income - today_expense

    # ── Needs-review count ─────────────────────────────────────────────────────
    review_result = (
        client.table("transactions")
        .select("id", count="exact")
        .eq("user_id", user_id)
        .eq("needs_review", True)
        .execute()
    )
    needs_review_count = review_result.count or 0

    # ── Recent 10 transactions ─────────────────────────────────────────────────
    recent_txs = (
        client.table("transactions")
        .select(
            "id,account_id,amount,direction,type,narration,date,status,"
            "needs_review,category_id,categories(name,color),accounts(name)"
        )
        .eq("user_id", user_id)
        .order("date", desc=True)
        .order("created_at", desc=True)
        .limit(10)
        .execute()
    ).data or []

    # ── Unread digests ─────────────────────────────────────────────────────────
    unread_digests = get_unread_digests(user_id)
    unread_digest_count = len(unread_digests)

    return jsonify({
        "total_balance": round(total_balance, 2),
        "today_income": round(today_income, 2),
        "today_expense": round(today_expense, 2),
        "today_net": round(today_net, 2),
        "needs_review_count": needs_review_count,
        "recent_transactions": recent_txs,
        "accounts": accounts,
        "unread_digest_count": unread_digest_count,
        "latest_digest": unread_digests[0] if unread_digests else None,
    })


@bp.get("/chart")
@require_auth
def get_chart_data():
    """
    Time-series income vs expense for line graph.
    Query param: period = '30' | '90' | 'year' (default: '30')
    """
    user_id = g.user_id
    client = get_admin_client()
    period = request.args.get("period", "30")

    today = date.today()
    if period == "year":
        start_date = date(today.year, 1, 1)
    else:
        days = int(period) if period.isdigit() else 30
        start_date = today - timedelta(days=days)

    txs = (
        client.table("transactions")
        .select("date,direction,amount")
        .eq("user_id", user_id)
        .neq("type", "transfer")
        .neq("status", "Raw")
        .gte("date", start_date.isoformat())
        .lte("date", today.isoformat())
        .order("date", desc=False)
        .execute()
    ).data or []

    # Group by date
    daily: dict[str, dict] = {}
    current = start_date
    while current <= today:
        daily[current.isoformat()] = {"date": current.isoformat(), "income": 0.0, "expense": 0.0}
        current += timedelta(days=1)

    for tx in txs:
        d = tx["date"]
        if d in daily:
            if tx["direction"] == "credit":
                daily[d]["income"] += float(tx["amount"])
            else:
                daily[d]["expense"] += float(tx["amount"])

    return jsonify({"chart_data": list(daily.values()), "period": period})


@bp.get("/donut")
@require_auth
def get_category_spend():
    """
    Category-wise spend for the current month (donut chart data).
    Only expenses, excluding transfers.
    """
    user_id = g.user_id
    client = get_admin_client()
    today = date.today()
    month_start = date(today.year, today.month, 1).isoformat()

    txs = (
        client.table("transactions")
        .select("amount,category_id,categories(name,color)")
        .eq("user_id", user_id)
        .eq("direction", "debit")
        .neq("type", "transfer")
        .neq("status", "Raw")
        .gte("date", month_start)
        .execute()
    ).data or []

    grouped: dict[str, dict] = {}
    for tx in txs:
        cat = tx.get("categories")
        if cat:
            key = tx["category_id"]
            if key not in grouped:
                grouped[key] = {
                    "category_id": key,
                    "category_name": cat["name"],
                    "color": cat.get("color", "#6B7280"),
                    "total": 0.0,
                }
            grouped[key]["total"] += float(tx["amount"])
        else:
            if "uncategorized" not in grouped:
                grouped["uncategorized"] = {
                    "category_id": None,
                    "category_name": "Uncategorized",
                    "color": "#9CA3AF",
                    "total": 0.0,
                }
            grouped["uncategorized"]["total"] += float(tx["amount"])

    result = sorted(grouped.values(), key=lambda x: x["total"], reverse=True)
    return jsonify({"donut_data": result, "month": month_start[:7]})


@bp.get("/digests")
@require_auth
def list_digests():
    """Return all digests for the user, newest first."""
    user_id = g.user_id
    client = get_admin_client()
    rows = (
        client.table("digests")
        .select("*")
        .eq("user_id", user_id)
        .order("date", desc=True)
        .limit(30)
        .execute()
    ).data or []
    return jsonify({"digests": rows})


@bp.post("/digests/<digest_id>/read")
@require_auth
def mark_digest_read_ep(digest_id):
    mark_digest_read(g.user_id, digest_id)
    return jsonify({"ok": True})


@bp.post("/digests/read-all")
@require_auth
def mark_all_read():
    mark_all_digests_read(g.user_id)
    return jsonify({"ok": True})
