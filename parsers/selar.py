"""
Selar email parser.
Handles payment received notifications from Selar.co.
Filters out abandoned cart, product creation, and marketing emails.

Sample Selar payment email:
  Subject: You've received a payment!
  Body: Congratulations! You've received a payment of ₦5,000.00
        Product: Holy Grills Suya Pack
        Buyer: John Doe (john@example.com)
        Order ID: ORD-2024011512345
        Payment Method: Card
        Date: January 15, 2024
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from parsers.base import BaseParser, NonTransactionEmail, ParseError, ParsedTransaction


class SelarParser(BaseParser):
    VERSION = 2

    # ── Subject keywords that definitively mean NOT a transaction ─────────────
    _NON_TX_SUBJECTS = [
        "abandoned",
        "cart",
        "reminder",
        "checkout",
        "you have a new",
        "product created",
        "product published",
        "welcome to selar",
        "newsletter",
        "tips",
        "digest",
        "course",
        "marketing",
        "promotion",
        "verify",
        "password",
        "reset",
    ]

    # ── Subject keywords that positively identify a payment / sale email ──────
    _PAYMENT_SUBJECTS = [
        "received a payment",
        "new order",
        "new sale",
        "payment received",
        "purchase",
        "you made a sale",
    ]

    # ── Subject keywords that identify a withdrawal / payout email ────────────
    # These are DEBIT + TRANSFER (money leaving Selar wallet to user's bank)
    _WITHDRAWAL_SUBJECTS = [
        "withdrawal initiated",
        "withdrawal",
        "payout",
        "funds transfer",
    ]

    # ── Withdrawal-specific patterns ──────────────────────────────────────────
    _WITHDRAWAL_AMOUNT_PATTERNS = [
        r"Amount[:\s]+(?:NGN|₦|N)?\s*([\d,]+\.?\d*)",
        r"[₦N]\s*([\d,]+\.?\d*)",
        r"NGN\s*([\d,]+\.?\d*)",
    ]
    _WITHDRAWAL_BANK_PATTERNS = [
        r"Bank[:\s]+(.+?)(?:\n|Account|$)",
        r"Bank\s+Name[:\s]+(.+?)(?:\n|Account|$)",
    ]
    _WITHDRAWAL_ACCT_NAME_PATTERNS = [
        r"Account\s+Name[:\s]+(.+?)(?:\n|Account\s+Number|Amount|$)",
        r"Beneficiary[:\s]+(.+?)(?:\n|Account|Amount|$)",
    ]

    # Currency-flexible: handles ₦ (naira), N, $, €, £, ₹ and bare numbers under amount labels
    _AMOUNT_PATTERNS = [
        r"payment\s+of\s+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"received\s+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"Amount[:\s]+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"Total[:\s]+[₦N\$€£₹]?\s*([\d,]+\.?\d*)",
        r"[₦N\$€£₹]\s*([\d,]+\.?\d*)",
    ]

    _REF_PATTERNS = [
        r"Order\s+(?:ID|#|Number)[:\s]+([A-Z0-9_\-]+)",
        r"Transaction\s+(?:ID|Ref)[:\s]+([A-Z0-9_\-]+)",
        r"Ref(?:erence)?[:\s]+([A-Z0-9_\-]+)",
        r"(ORD-[\w\-]+)",
        r"(SEL-[\w\-]+)",
    ]

    _PRODUCT_PATTERNS = [
        # New Selar template: bullet list under "Purchase Summary"
        # e.g.  "Purchase Summary\n- 2x Chicken Wings"
        r"Purchase\s+Summary\s*\n[-*•]\s*(?:\d+x\s+)?(.+?)(?:\n|$)",
        r"Purchase\s+Summary\s*\n\s*(?:\d+x\s+)?(.+?)(?:\n|$)",
        # Classic label-based formats
        r"Product[:\s]+(.+?)(?:\n|Buyer|Order|Date|$)",
        r"Item[:\s]+(.+?)(?:\n|Buyer|Order|Date|$)",
        r"for\s+(?:purchasing\s+)?['\"]?(.+?)['\"]?\s*(?:\n|from|by|$)",
    ]

    _BUYER_PATTERNS = [
        # New Selar template: name appears on the line after "Bio Data"
        r"Bio\s+Data\s*\n(.+?)(?:\n|$)",
        # Classic label-based formats
        r"Buyer[:\s]+(.+?)(?:\n|Order|Date|Email|$)",
        r"Customer\s+Information.*?\n(?:Bio\s+Data\s*\n)?(.+?)(?:\n|Order|Date|Email|$)",
        r"Customer[:\s]+(.+?)(?:\n|Order|Date|Email|$)",
        r"From[:\s]+(.+?)(?:\n|Order|Date|Email|$)",
    ]

    _DATE_PATTERNS = [
        # With ordinal suffix: "July 8th, 2026" / "January 1st, 2026"
        r"Date[:\s]+(\w+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4})",
        r"Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
        r"on\s+(\w+\s+\d{1,2}(?:st|nd|rd|th)?,?\s*\d{4})",
    ]

    def parse_email(
        self,
        subject: str,
        body_plain: str,
        body_html: str,
        received_at: datetime,
    ) -> ParsedTransaction:
        subject_lower = subject.lower()

        # HTML-in-plain guard (same issue as PremiumTrust)
        raw_plain = body_plain.strip() if body_plain else ""
        if raw_plain and self._looks_like_html(raw_plain):
            body = self._strip_html(raw_plain)
        elif raw_plain:
            body = raw_plain
        else:
            body = self._strip_html(body_html)

        combined_lower = f"{subject_lower} {body.lower()}"

        # ── Filter non-transaction emails FIRST ───────────────────────────────
        for skip_kw in self._NON_TX_SUBJECTS:
            if skip_kw in subject_lower:
                raise NonTransactionEmail(
                    f"Selar: skipping non-transaction email '{subject}' (matched '{skip_kw}')"
                )

        # ── Withdrawal / payout emails → DEBIT + TRANSFER ─────────────────────
        is_withdrawal = any(kw in subject_lower for kw in self._WITHDRAWAL_SUBJECTS)
        if is_withdrawal:
            return self._parse_withdrawal(subject, body, received_at)

        # ── Require at least one positive payment keyword ──────────────────────
        is_payment = any(kw in combined_lower for kw in self._PAYMENT_SUBJECTS)
        if not is_payment:
            raise NonTransactionEmail(
                f"Selar: no payment keywords found in '{subject}'"
            )

        # ── All Selar sales emails are CREDITS (money coming IN) ──────────────
        direction = "credit"

        # ── Amount ────────────────────────────────────────────────────────────
        amount = self._extract_amount(body)
        if amount is None:
            raise ParseError("Selar: could not extract payment amount")

        # ── Reference ─────────────────────────────────────────────────────────
        reference = self._extract_reference(body)
        if not reference:
            reference = f"SELAR-{received_at.strftime('%Y%m%d%H%M%S')}-{int(amount)}"

        # ── Narration: "Product name - Buyer name" ────────────────────────────
        product = self._extract_pattern(self._PRODUCT_PATTERNS, body)
        buyer = self._extract_pattern(self._BUYER_PATTERNS, body)
        if product and buyer:
            narration = f"{product.strip()} — Buyer: {buyer.strip()}"
        elif product:
            narration = f"Selar sale: {product.strip()}"
        elif buyer:
            narration = f"Selar payment from {buyer.strip()}"
        else:
            narration = "Selar payment received"

        # ── Actual balance not normally in Selar emails ────────────────────────
        actual_balance = None

        # ── Date ──────────────────────────────────────────────────────────────
        tx_datetime, tx_date = self._extract_date(body, received_at)

        return ParsedTransaction(
            reference=self.normalize_reference(reference),
            amount=amount,
            direction=direction,
            date=tx_date,
            date_time=tx_datetime,
            narration=narration[:200],
            actual_balance=actual_balance,
            raw_subject=subject,
            parser_version=self.VERSION,
        )

    def _parse_withdrawal(
        self,
        subject: str,
        body: str,
        received_at: datetime,
    ) -> ParsedTransaction:
        """
        Parse a Selar withdrawal / payout email.

        These are transfers FROM the Selar wallet TO the user's bank account:
          direction = 'debit'
          type      = 'transfer'   (set via extra, applied by write_transaction)
          needs_review = False      (auto-handled — not an expense)

        Example email body (after HTML strip):
          Bank: PALMPAY
          Account Name: BLESSING NIFEMI OLUWASEGUN
          Account Number: 7018412704
          Amount: 20,000
          Currency: NGN
        """
        # ── Amount ─────────────────────────────────────────────────────────────
        amount: Optional[float] = None
        for pat in self._WITHDRAWAL_AMOUNT_PATTERNS:
            m = re.search(pat, body, re.IGNORECASE)
            if m:
                try:
                    val = self.clean_amount(m.group(1))
                    if val > 0:
                        amount = val
                        break
                except (ValueError, AttributeError):
                    continue
        if amount is None:
            raise ParseError("Selar withdrawal: could not extract amount")

        # ── Reference ──────────────────────────────────────────────────────────
        reference = self._extract_reference(body)
        if not reference:
            reference = f"SELAR-WD-{received_at.strftime('%Y%m%d%H%M%S')}-{int(amount)}"

        # ── Narration: "Selar withdrawal → PALMPAY - BLESSING NIFEMI OLUWASEGUN" ─
        bank = self._extract_pattern(self._WITHDRAWAL_BANK_PATTERNS, body)
        acct_name = self._extract_pattern(self._WITHDRAWAL_ACCT_NAME_PATTERNS, body)
        if bank and acct_name:
            narration = f"Selar withdrawal → {bank.strip()} - {acct_name.strip()}"
        elif bank:
            narration = f"Selar withdrawal → {bank.strip()}"
        elif acct_name:
            narration = f"Selar withdrawal to {acct_name.strip()}"
        else:
            narration = "Selar wallet withdrawal"

        # ── Date ───────────────────────────────────────────────────────────────
        tx_datetime, tx_date = self._extract_date(body, received_at)

        return ParsedTransaction(
            reference=self.normalize_reference(reference),
            amount=amount,
            direction="debit",
            date=tx_date,
            date_time=tx_datetime,
            narration=narration[:200],
            actual_balance=None,
            raw_subject=subject,
            parser_version=self.VERSION,
            extra={"type": "transfer"},  # write_transaction uses this to set tx type
        )

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
                if len(ref) >= 3:
                    return ref
        return None

    def _extract_pattern(self, patterns: list, body: str) -> Optional[str]:
        for pattern in patterns:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                value = m.group(1).strip()
                if value and len(value) >= 2:
                    return value
        return None

    def _extract_date(self, body: str, fallback: datetime):
        date_formats = [
            "%B %d, %Y",
            "%b %d, %Y",
            "%d/%m/%Y",
            "%Y-%m-%dT%H:%M:%S",
        ]
        for pattern in self._DATE_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
            if m:
                date_str = m.group(1).strip()
                # Strip ordinal suffixes: "8th" → "8", "1st" → "1", etc.
                date_str = re.sub(r"(\d+)(st|nd|rd|th)\b", r"\1", date_str, flags=re.IGNORECASE)
                # Normalise whitespace
                date_str = re.sub(r"\s+", " ", date_str).strip()
                for fmt in date_formats:
                    try:
                        dt = datetime.strptime(date_str, fmt)
                        return dt, dt.date()
                    except ValueError:
                        continue
        return fallback, fallback.date()

    @staticmethod
    def _strip_html(html: str) -> str:
        """Full HTML → plain text (same pipeline as NigerianBankParser)."""
        import html as _html_mod
        if not html:
            return ""
        html = re.sub(r"<head[^>]*>.*?</head>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        html = re.sub(r"<(?:br|p|div|tr|li|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
        html = re.sub(r"<(?:td|th)[^>]*>", " ", html, flags=re.IGNORECASE)
        html = re.sub(r"<[^>]+>", " ", html)
        html = _html_mod.unescape(html)
        html = re.sub(r"[ \t]+", " ", html)
        html = re.sub(r"\n{3,}", "\n\n", html)
        return html.strip()
