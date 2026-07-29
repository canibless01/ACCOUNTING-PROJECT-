"""
Reports endpoints.
P&L, Cash Flow, Reconciliation Summary, and PDF/Excel export.
"""
from __future__ import annotations

import io
from datetime import date, timedelta

from flask import Blueprint, Response, g, jsonify, request, send_file

from auth_middleware import require_auth
from db import get_admin_client
from services.balance import get_account_balance, get_reconciliation_summary
from services.exports import generate_excel_report, generate_pdf_report

bp = Blueprint("reports", __name__, url_prefix="/api/reports")


def _parse_date_range(args) -> tuple[str, str]:
    """Parse date_from / date_to from query params, defaulting to current month."""
    today = date.today()
    default_from = date(today.year, today.month, 1).isoformat()
    default_to = today.isoformat()
    return args.get("date_from", default_from), args.get("date_to", default_to)


@bp.get("/pl")
@require_auth
def profit_and_loss():
    """
    Profit & Loss statement for a date range.
    Transfers explicitly excluded.
    Query params: date_from, date_to, account_id (optional)
    """
    user_id = g.user_id
    client = get_admin_client()
    date_from, date_to = _parse_date_range(request.args)
    account_id = request.args.get("account_id")

    def _build_q(direction: str):
        q = (
            client.table("transactions")
            .select("amount,category_id,categories(name,color)")
            .eq("user_id", user_id)
            .eq("direction", direction)
            .neq("type", "transfer")
            .neq("status", "Raw")
            .gte("date", date_from)
            .lte("date", date_to)
        )
        if account_id:
            q = q.eq("account_id", account_id)
        return q.execute().data or []

    income_txs = _build_q("credit")
    expense_txs = _build_q("debit")

    # Group by category
    def group_by_category(txs):
        grouped: dict = {}
        uncategorized = 0.0
        for tx in txs:
            cat = tx.get("categories")
            amount = float(tx["amount"])
            if cat:
                cid = tx["category_id"]
                if cid not in grouped:
                    grouped[cid] = {
                        "category_id": cid,
                        "category_name": cat["name"],
                        "color": cat.get("color", "#6B7280"),
                        "total": 0.0,
                    }
                grouped[cid]["total"] += amount
            else:
                uncategorized += amount
        result = sorted(grouped.values(), key=lambda x: x["total"], reverse=True)
        if uncategorized > 0:
            result.append({
                "category_id": None,
                "category_name": "Uncategorized",
                "color": "#9CA3AF",
                "total": uncategorized,
            })
        return result

    income_by_cat = group_by_category(income_txs)
    expense_by_cat = group_by_category(expense_txs)
    total_income = sum(r["total"] for r in income_by_cat)
    total_expense = sum(r["total"] for r in expense_by_cat)
    net_profit = total_income - total_expense

    return jsonify({
        "report_type": "pl",
        "date_range": {"from": date_from, "to": date_to},
        "income_by_category": income_by_cat,
        "expense_by_category": expense_by_cat,
        "total_income": round(total_income, 2),
        "total_expense": round(total_expense, 2),
        "net_profit": round(net_profit, 2),
        "transfers_excluded": True,
        "account_id": account_id,
    })


@bp.get("/cashflow")
@require_auth
def cash_flow():
    """
    Cash flow statement for a date range.
    Shows opening balance, total in, total out, closing balance vs actual.
    Query params: date_from, date_to, account_id (required)
    """
    user_id = g.user_id
    client = get_admin_client()
    date_from, date_to = _parse_date_range(request.args)
    account_id = request.args.get("account_id")

    # Get all active accounts if no specific account
    if account_id:
        accounts = (
            client.table("accounts")
            .select("id,name,opening_balance,actual_balance,actual_balance_at")
            .eq("id", account_id)
            .eq("user_id", user_id)
            .execute()
        ).data or []
    else:
        accounts = (
            client.table("accounts")
            .select("id,name,opening_balance,actual_balance,actual_balance_at")
            .eq("user_id", user_id)
            .eq("is_active", True)
            .execute()
        ).data or []

    # Compute opening balance at date_from for each account
    # (opening_balance + all transactions before date_from)
    def get_opening_at(acc_id: str, from_date: str) -> float:
        acc_rows = (
            client.table("accounts")
            .select("opening_balance")
            .eq("id", acc_id)
            .limit(1)
            .execute()
        ).data
        ob = float(acc_rows[0]["opening_balance"]) if acc_rows else 0.0

        prior = (
            client.table("transactions")
            .select("direction,amount")
            .eq("account_id", acc_id)
            .neq("status", "Raw")
            .lt("date", from_date)
            .execute()
        ).data or []
        return ob + sum(
            float(t["amount"]) if t["direction"] == "credit" else -float(t["amount"])
            for t in prior
        )

    account_flows = []
    for acc in accounts:
        acc_id = acc["id"]

        opening = get_opening_at(acc_id, date_from)

        txs = (
            client.table("transactions")
            .select("direction,amount")
            .eq("account_id", acc_id)
            .neq("status", "Raw")
            .gte("date", date_from)
            .lte("date", date_to)
            .execute()
        ).data or []

        total_in = sum(float(t["amount"]) for t in txs if t["direction"] == "credit")
        total_out = sum(float(t["amount"]) for t in txs if t["direction"] == "debit")
        closing = opening + total_in - total_out
        actual = float(acc["actual_balance"]) if acc.get("actual_balance") is not None else None

        account_flows.append({
            "account_id": acc_id,
            "account_name": acc["name"],
            "opening_balance": round(opening, 2),
            "total_in": round(total_in, 2),
            "total_out": round(total_out, 2),
            "closing_balance": round(closing, 2),
            "actual_closing": round(actual, 2) if actual is not None else None,
            "variance": round(closing - actual, 2) if actual is not None else None,
        })

    # Combined totals
    combined_opening = sum(a["opening_balance"] for a in account_flows)
    combined_in = sum(a["total_in"] for a in account_flows)
    combined_out = sum(a["total_out"] for a in account_flows)
    combined_closing = sum(a["closing_balance"] for a in account_flows)
    combined_actual = sum(a["actual_closing"] for a in account_flows if a["actual_closing"] is not None) or None

    return jsonify({
        "report_type": "cashflow",
        "date_range": {"from": date_from, "to": date_to},
        "accounts": account_flows,
        "opening_balance": round(combined_opening, 2),
        "total_in": round(combined_in, 2),
        "total_out": round(combined_out, 2),
        "closing_balance": round(combined_closing, 2),
        "actual_closing": round(combined_actual, 2) if combined_actual is not None else None,
    })


@bp.get("/reconciliation")
@require_auth
def reconciliation_summary():
    """Return a list of accounts currently showing a reconciliation mismatch."""
    mismatches = get_reconciliation_summary(g.user_id)
    return jsonify({
        "report_type": "reconciliation",
        "mismatches": mismatches,
        "total_mismatches": len(mismatches),
        "all_reconciled": len(mismatches) == 0,
    })


@bp.get("/export/<report_type>.<fmt>")
@require_auth
def export_report(report_type: str, fmt: str):
    """
    Export a report as PDF or Excel.
    report_type: 'pl' | 'cashflow' | 'reconciliation'
    fmt: 'pdf' | 'xlsx'
    Query params: same as the respective report endpoint.
    """
    if report_type not in ("pl", "cashflow", "reconciliation"):
        return jsonify({"error": "Invalid report type"}), 400
    if fmt not in ("pdf", "xlsx"):
        return jsonify({"error": "Format must be 'pdf' or 'xlsx'"}), 400

    # Re-run the appropriate report logic
    if report_type == "pl":
        report_data = _get_pl_data()
    elif report_type == "cashflow":
        report_data = _get_cashflow_data()
    else:
        report_data = _get_reconciliation_data()

    user_info = {"user_id": g.user_id, "email": g.user_email}

    if fmt == "pdf":
        pdf_bytes = generate_pdf_report(report_type, report_data, user_info)
        return Response(
            pdf_bytes,
            mimetype="application/pdf",
            headers={
                "Content-Disposition": f'attachment; filename="holy-grills-{report_type}.pdf"'
            },
        )
    else:
        xlsx_bytes = generate_excel_report(report_type, report_data, user_info)
        return Response(
            xlsx_bytes,
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={
                "Content-Disposition": f'attachment; filename="holy-grills-{report_type}.xlsx"'
            },
        )


# ── Internal helpers that duplicate the query logic for exports ────────────────

def _get_pl_data() -> dict:
    user_id = g.user_id
    client = get_admin_client()
    date_from, date_to = _parse_date_range(request.args)
    account_id = request.args.get("account_id")

    def _build_q(direction):
        q = (
            client.table("transactions")
            .select("amount,category_id,categories(name,color)")
            .eq("user_id", user_id)
            .eq("direction", direction)
            .neq("type", "transfer")
            .neq("status", "Raw")
            .gte("date", date_from)
            .lte("date", date_to)
        )
        if account_id:
            q = q.eq("account_id", account_id)
        return q.execute().data or []

    def group(txs):
        g_map: dict = {}
        unc = 0.0
        for tx in txs:
            cat = tx.get("categories")
            amt = float(tx["amount"])
            if cat:
                cid = tx["category_id"]
                if cid not in g_map:
                    g_map[cid] = {"category_id": cid, "category_name": cat["name"],
                                  "color": cat.get("color", "#6B7280"), "total": 0.0}
                g_map[cid]["total"] += amt
            else:
                unc += amt
        result = sorted(g_map.values(), key=lambda x: x["total"], reverse=True)
        if unc > 0:
            result.append({"category_id": None, "category_name": "Uncategorized",
                           "color": "#9CA3AF", "total": unc})
        return result

    income_by_cat = group(_build_q("credit"))
    expense_by_cat = group(_build_q("debit"))
    total_income = sum(r["total"] for r in income_by_cat)
    total_expense = sum(r["total"] for r in expense_by_cat)
    return {
        "date_range": {"from": date_from, "to": date_to},
        "income_by_category": income_by_cat,
        "expense_by_category": expense_by_cat,
        "total_income": total_income,
        "total_expense": total_expense,
        "net_profit": total_income - total_expense,
        "transfers_excluded": True,
    }


def _get_cashflow_data() -> dict:
    from flask import current_app
    user_id = g.user_id
    client = get_admin_client()
    date_from, date_to = _parse_date_range(request.args)

    accounts = (
        client.table("accounts")
        .select("id,name,opening_balance,actual_balance")
        .eq("user_id", user_id).eq("is_active", True).execute()
    ).data or []

    total_in = 0.0
    total_out = 0.0
    opening = 0.0
    actual_total = None

    for acc in accounts:
        opening += float(acc.get("opening_balance") or 0)
        txs = (
            client.table("transactions")
            .select("direction,amount")
            .eq("account_id", acc["id"])
            .neq("status", "Raw")
            .gte("date", date_from)
            .lte("date", date_to)
            .execute()
        ).data or []
        for tx in txs:
            amt = float(tx["amount"])
            if tx["direction"] == "credit":
                total_in += amt
            else:
                total_out += amt
        if acc.get("actual_balance") is not None:
            actual_total = (actual_total or 0) + float(acc["actual_balance"])

    return {
        "date_range": {"from": date_from, "to": date_to},
        "opening_balance": opening,
        "total_in": total_in,
        "total_out": total_out,
        "closing_balance": opening + total_in - total_out,
        "actual_closing": actual_total,
    }


def _get_reconciliation_data() -> dict:
    mismatches = get_reconciliation_summary(g.user_id)
    return {"mismatches": mismatches}
