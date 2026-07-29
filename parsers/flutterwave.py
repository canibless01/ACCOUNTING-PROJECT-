"""
Flutterwave email parser.
Handles payment notification emails from Flutterwave.

Sample Flutterwave payment email:
  Subject: Payment Notification
  From: no-reply@flutterwave.com

  Hi [Business Name],

  You have received a new payment.

  Customer name: John Doe
  Customer email: john@example.com
  Amount: NGN 10,000.00
  Reference: FLW-PROD-XXXXXXXXXX
  Date: July 25, 2026
  Status: Successful

Sample Flutterwave (template B — subject includes FLW Ref):
  Subject: Payment Notification - FLW Ref: FLW-LOCAL-XXXXX
  Amount charged: ₦5,000
  Customer: Jane Smith
  Flutterwave Ref: FLW-PROD-XXXXX
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from parsers.base import BaseParser, NonTransactionEmail, ParseError, ParsedTransaction


class FlutterwaveParser(BaseParser):
    VERSION = 1

    # ── Non-transaction subjects ───────────────────────────────────────────────
    _NON_TX_SUBJECTS = [
        "failed",
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
        "transfer",
        "subscription",
    ]

    # ── Positive payment keywords ──────────────────────────────────────────────
    _PAYMENT_SUBJECTS = [
        "payment notification",
        "payment received",
        "payment successful",
        "new payment",
        "successful payment",
        "flw ref",
        "flw-prod",
        "flw-local",
        "you have received",
    ]

    # ── Amount ─────────────────────────────────────────────────────────────────
    _AMOUNT_PATTERNS = [
        r"Amount\s+charged[:\s]+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"Amount[:\s]+(?:NGN|USD|GBP|EUR)?\s*([\d,]+\.?\d*)",
        r"NGN\s*([\d,]+\.?\d*)",
        r"[₦N\$€£₹]\s*([\d,]+\.?\d*)",
        r"Total[:\s]+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
    ]

    # ── Reference ──────────────────────────────────────────────────────────────
    _REF_PATTERNS = [
        r"(FLW-PROD-[A-Za-z0-9]+)",
        r"(FLW-LOCAL-[A-Za-z0-9]+)",
        r"(FLW-[A-Za-z0-9\-]+)",
        r"Flutterwave\s+Ref[:\s]+([A-Za-z0-9\-]+)",
        r"Reference[:\s]+([A-Za-z0-9_\-]+)",
        r"Transaction\s+(?:ID|Ref)[:\s]+([A-Za-z0-9_\-]+)",
    ]

    # ── Customer / narration ───────────────────────────────────────────────────
    _CUSTOMER_PATTERNS = [
        r"Customer\s+name[:\s]+(.+?)(?:\n|Email|Reference|Amount|Date|$)",
        r"Customer[:\s]+(.+?)(?:\n|Email|Reference|Amount|Date|$)",
        r"Payer[:\s]+(.+?)(?:\n|Email|Reference|Amount|Date|$)",
        r"Sender[:\s]+(.+?)(?:\n|Email|Reference|Amount|Date|$)",
    ]

    _PRODUCT_PATTERNS = [
        r"(?:for|Description|Narration|Item|Product)[:\s]+(.+?)(?:\n|Reference|Customer|Date|$)",
        r"Payment\s+for\s+(.+?)(?:\n|Reference|Customer|Date|$)",
    ]

    # ── Date ───────────────────────────────────────────────────────────────────
    _DATE_PATTERNS = [
        r"Date[:\s]+(\w+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4}(?:\s+\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?)?)",
        r"Date[:\s]+(\d{1,2}/\d{1,2}/\d{4}(?:\s+\d{1,2}:\d{2}(?::\d{2})?)?)",
        r"Date[:\s]+(\d{1,2}\s+\w{3},?\s*\d{4})",
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
    ]

    _DATE_FORMATS = [
        "%B %d, %Y %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y",
        "%b %d, %Y",
        "%d %b, %Y",
        "%d %b %Y",
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
                    f"Flutterwave: skipping non-transaction email '{subject}' (matched '{skip_kw}')"
                )

        is_payment = any(kw in combined_lower for kw in self._PAYMENT_SUBJECTS)
        # Also accept if we find a FLW reference pattern anywhere
        has_flw_ref = bool(re.search(r"\bFLW-", combined_lower, re.IGNORECASE))
        if not is_payment and not has_flw_ref:
            raise NonTransactionEmail(
                f"Flutterwave: no payment keywords or FLW reference found in '{subject}'"
            )

        # Also check that we have a "Successful" status if status is mentioned
        status_match = re.search(r"Status[:\s]+(\w+)", body, re.IGNORECASE)
        if status_match and status_match.group(1).lower() not in ("successful", "success", "completed"):
            raise NonTransactionEmail(
                f"Flutterwave: payment status is '{status_match.group(1)}', not successful"
            )

        # All Flutterwave payment notifications are income (credits to the business)
        direction = "credit"

        # ── Amount ────────────────────────────────────────────────────────────
        amount = self._extract_amount(body)
        if amount is None:
            raise ParseError("Flutterwave: could not extract payment amount")

        # ── Reference ─────────────────────────────────────────────────────────
        reference = self._extract_reference(body) or self._extract_reference(subject)
        if not reference:
            reference = f"FLW-{received_at.strftime('%Y%m%d%H%M%S')}-{int(amount)}"

        # ── Narration ─────────────────────────────────────────────────────────
        customer = self._extract_first(self._CUSTOMER_PATTERNS, body)
        product = self._extract_first(self._PRODUCT_PATTERNS, body)

        if product and customer:
            narration = f"{product.strip()} — {customer.strip()}"
        elif customer:
            narration = f"Flutterwave payment from {customer.strip()}"
        elif product:
            narration = f"Flutterwave: {product.strip()}"
        else:
            narration = "Flutterwave payment received"

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

    def _extract_reference(self, text: str) -> Optional[str]:
        for pattern in self._REF_PATTERNS:
            m = re.search(pattern, text, re.IGNORECASE)
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
                val = re.sub(r"\s*[\(<]?[\w.]+@[\w.]+[\)>]?\s*$", "", val).strip()
                if val and 2 <= len(val) <= 150:
                    return val
        return None

    def _extract_date(self, body: str, fallback: datetime):
        for pattern in self._DATE_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                date_str = m.group(1).strip()
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
