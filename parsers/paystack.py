"""
Paystack email parser.
Handles payment received notifications from Paystack.

Sample Paystack payment email (template A — "someone paid you"):
  Subject: You have a new payment!
  From: no-reply@paystack.com

  Hello [Business],

  John Doe just paid ₦5,000.00 for Holy Grills Suya Pack.

  Reference: pay_abcdefgh12345678
  Payment method: Card
  Date: 25 Jul, 2026 3:45 PM
  Customer email: john@example.com

Sample Paystack payment email (template B — settlement):
  Subject: Payment Notification
  Amount: ₦10,000.00
  Customer: Jane Smith
  Reference: T123456789012345
  Date: July 25, 2026
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from parsers.base import BaseParser, NonTransactionEmail, ParseError, ParsedTransaction


class PaystackParser(BaseParser):
    VERSION = 1

    # ── Non-transaction subjects ───────────────────────────────────────────────
    _NON_TX_SUBJECTS = [
        "invoice",
        "failed",
        "abandoned",
        "refund",
        "chargeback",
        "dispute",
        "welcome",
        "otp",
        "password",
        "verification",
        "statement",
        "payout",
        "withdrawal",
        "newsletter",
        "tips",
        "promotion",
    ]

    # ── Positive payment keywords ──────────────────────────────────────────────
    _PAYMENT_SUBJECTS = [
        "new payment",
        "payment notification",
        "payment received",
        "payment successful",
        "you have a new payment",
        "just paid",
        "new transaction",
        "successful payment",
    ]

    # ── Amount ─────────────────────────────────────────────────────────────────
    _AMOUNT_PATTERNS = [
        r"just paid\s+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"payment\s+of\s+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"Amount[:\s]+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"Total[:\s]+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"[₦N\$€£₹]\s*([\d,]+\.?\d*)",
    ]

    # ── Reference ──────────────────────────────────────────────────────────────
    _REF_PATTERNS = [
        r"Reference[:\s]+([A-Za-z0-9_\-]+)",
        r"(pay_[A-Za-z0-9]+)",
        r"Transaction\s+(?:ID|Ref)[:\s]+([A-Za-z0-9_\-]+)",
        r"Ref(?:erence)?[:\s]+([A-Za-z0-9_\-]{6,})",
        r"\b(T\d{13,})\b",          # Paystack USSD references: T + 13 digits
    ]

    # ── Narration: customer name + product ─────────────────────────────────────
    _CUSTOMER_PATTERNS = [
        r"^(.+?)\s+just paid",       # "John Doe just paid ₦5,000.00"
        r"Customer(?:\s+name)?[:\s]+(.+?)(?:\n|Email|Reference|Ref|Date|$)",
        r"Payer[:\s]+(.+?)(?:\n|Email|Reference|Ref|Date|$)",
        r"From[:\s]+(.+?)(?:\n|Email|Reference|Ref|Date|$)",
    ]

    _PRODUCT_PATTERNS = [
        r"paid\s+[₦N\$€£₹]?[\d,]+\.?\d*\s+for\s+(.+?)(?:\n|Reference|Ref|Date|$)",
        r"for\s+(?:purchasing\s+)?['\"]?(.+?)['\"]?\s*(?:\n|Reference|Ref|Date|$)",
        r"Product[:\s]+(.+?)(?:\n|Reference|Customer|Date|$)",
        r"Item[:\s]+(.+?)(?:\n|Reference|Customer|Date|$)",
        r"Description[:\s]+(.+?)(?:\n|Reference|Customer|Date|$)",
    ]

    # ── Date ───────────────────────────────────────────────────────────────────
    _DATE_PATTERNS = [
        r"Date[:\s]+(\d{1,2}\s+\w{3},?\s*\d{4}\s+\d{1,2}:\d{2}\s*[AaPp][Mm])",
        r"Date[:\s]+(\w+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}(?:\s+\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?)?)",
        r"Date[:\s]+(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
    ]

    _DATE_FORMATS = [
        "%d %b, %Y %I:%M %p",
        "%d %b %Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%Y-%m-%dT%H:%M:%S",
    ]

    def parse_email(
        self,
        subject: str,
        body_plain: str,
        body_html: str,
        received_at: datetime,
    ) -> ParsedTransaction:
        subject_lower = subject.lower()
        body = body_plain or self._strip_html(body_html)
        combined_lower = f"{subject_lower} {body.lower()}"

        # ── Filter non-transaction emails ─────────────────────────────────────
        for skip_kw in self._NON_TX_SUBJECTS:
            if skip_kw in subject_lower:
                raise NonTransactionEmail(
                    f"Paystack: skipping non-transaction email '{subject}' (matched '{skip_kw}')"
                )

        is_payment = any(kw in combined_lower for kw in self._PAYMENT_SUBJECTS)
        if not is_payment:
            raise NonTransactionEmail(
                f"Paystack: no payment keywords found in '{subject}'"
            )

        # All Paystack payment notification emails are income (credits to the business)
        direction = "credit"

        # ── Amount ────────────────────────────────────────────────────────────
        amount = self._extract_amount(body)
        if amount is None:
            raise ParseError("Paystack: could not extract payment amount")

        # ── Reference ─────────────────────────────────────────────────────────
        reference = self._extract_reference(body)
        if not reference:
            reference = f"PST-{received_at.strftime('%Y%m%d%H%M%S')}-{int(amount)}"

        # ── Narration: "Product — Customer" ───────────────────────────────────
        customer = self._extract_first(self._CUSTOMER_PATTERNS, body)
        product = self._extract_first(self._PRODUCT_PATTERNS, body)

        if product and customer:
            narration = f"{product.strip()} — {customer.strip()}"
        elif product:
            narration = f"Paystack: {product.strip()}"
        elif customer:
            narration = f"Payment from {customer.strip()}"
        else:
            narration = "Paystack payment received"

        # ── Date ──────────────────────────────────────────────────────────────
        tx_datetime, tx_date = self._extract_date(body, received_at)

        return ParsedTransaction(
            reference=self.normalize_reference(reference),
            amount=amount,
            direction=direction,
            date=tx_date,
            date_time=tx_datetime,
            narration=narration[:200],
            actual_balance=None,
            raw_subject=subject,
            parser_version=self.VERSION,
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _extract_amount(self, body: str) -> Optional[float]:
        for pattern in self._AMOUNT_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                try:
                    val = self.clean_amount(m.group(1))
                    if val > 0:
                        return val
                except (ValueError, AttributeError):
                    continue
        return None

    def _extract_reference(self, body: str) -> Optional[str]:
        for pattern in self._REF_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                ref = m.group(1).strip()
                if len(ref) >= 4:
                    return ref
        return None

    def _extract_first(self, patterns: list, body: str) -> Optional[str]:
        for pattern in patterns:
            m = re.search(pattern, body, re.IGNORECASE | re.MULTILINE)
            if m:
                val = m.group(1).strip()
                # Strip trailing punctuation and email addresses
                val = re.sub(r"\s*[\(<]?[\w.]+@[\w.]+[\)>]?\s*$", "", val).strip()
                if val and 2 <= len(val) <= 150:
                    return val
        return None

    def _extract_date(self, body: str, fallback: datetime):
        for pattern in self._DATE_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                date_str = m.group(1).strip()
                # Strip ordinal suffixes and normalise AM/PM
                date_str = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", date_str, flags=re.IGNORECASE)
                date_str = re.sub(r"\s*([AaPp][Mm])$", lambda x: " " + x.group(1).upper(), date_str)
                date_str = re.sub(r"\s{2,}", " ", date_str).strip()
                for fmt in self._DATE_FORMATS:
                    try:
                        dt = datetime.strptime(date_str, fmt)
                        return dt, dt.date()
                    except ValueError:
                        continue
        return fallback, fallback.date()

    @staticmethod
    def _strip_html(html: str) -> str:
        if not html:
            return ""
        html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<(?:br|p|div|tr|li)[^>]*>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"[ \t]+", " ", html).strip()
