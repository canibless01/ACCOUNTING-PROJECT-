"""
OPay email parser.
Handles credit and debit transaction notification emails from OPay Nigeria.

Sample OPay credit email:
  Subject: Credit Alert
  Body: Dear Customer, ₦10,000.00 has been credited to your OPay account.
        Sender: JANE DOE
        Reference: OPY2024011512345678
        Available Balance: ₦32,450.00
        Time: Jan 15, 2024 02:35 PM

Sample OPay debit email:
  Subject: Debit Alert
  Body: Dear Customer, ₦2,500.00 has been debited from your OPay account.
        Recipient: MTN AIRTIME
        Reference: OPY2024011512399999
        Available Balance: ₦29,950.00
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from parsers.base import BaseParser, NonTransactionEmail, ParseError, ParsedTransaction


class OPayParser(BaseParser):
    VERSION = 2

    _NON_TX_SUBJECTS = [
        "welcome",
        "otp",
        "password",
        "verification",
        "promotion",
        "congratulations",
        "kyc",
        "account upgrade",
        "statement",
    ]

    _AMOUNT_PATTERNS = [
        r"[₦N]\s*([\d,]+\.?\d*)\s+has been (?:credit|debit)",
        r"(?:credit|debit)ed\s+[₦N]?\s*([\d,]+\.?\d*)",
        r"Amount[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
        r"[₦N]([\d,]+\.?\d*)",
    ]

    _REF_PATTERNS = [
        r"Reference[:\s]+([A-Z0-9_\-]+)",
        r"Ref[:\s]+([A-Z0-9_\-]+)",
        r"Transaction\s+ID[:\s]+([A-Z0-9_\-]+)",
        r"Order\s+ID[:\s]+([A-Z0-9_\-]+)",
        r"(OPY[\d]+)",
        r"(OPay[\d]+)",
    ]

    _NARRATION_PATTERNS = [
        r"(?:Sender|From)[:\s]+(.+?)(?:\n|Reference|Ref|Balance|$)",
        r"(?:Recipient|To|Beneficiary)[:\s]+(.+?)(?:\n|Reference|Ref|Balance|$)",
        r"Narration[:\s]+(.+?)(?:\n|Reference|Ref|Balance|$)",
        r"Description[:\s]+(.+?)(?:\n|Reference|Ref|Balance|$)",
        r"Remark[:\s]+(.+?)(?:\n|Reference|Ref|Balance|$)",
    ]

    _BALANCE_PATTERNS = [
        r"Available\s+Balance[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
        r"Wallet\s+Balance[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
        r"Balance[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
    ]

    _DATE_PATTERNS = [
        r"Time[:\s]+(\w+ \d+,\s*\d{4}\s+\d{1,2}:\d{2}\s*[AP]M)",
        r"Date[:\s]+(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?)",
        r"Date[:\s]+(\w+\s+\d{1,2},\s*\d{4})",
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
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

        for skip_kw in self._NON_TX_SUBJECTS:
            if skip_kw in subject_lower:
                raise NonTransactionEmail(f"OPay: skipping non-transaction email '{subject}'")

        combined = f"{subject} {body}"
        combined_lower = combined.lower()

        if not any(kw in combined_lower for kw in ["credit", "debit", "transaction", "transfer"]):
            raise NonTransactionEmail(f"OPay: no transaction keywords in '{subject}'")

        direction = self._extract_direction(combined_lower)
        amount = self._extract_amount(body)
        if amount is None:
            raise ParseError("OPay: could not extract amount")

        reference = self._extract_reference(body)
        if not reference:
            reference = f"OPAY-{received_at.strftime('%Y%m%d%H%M%S')}-{int(amount)}"

        narration = self._extract_narration(body, direction)
        actual_balance = self._extract_balance(body)
        tx_datetime, tx_date = self._extract_date(body, received_at)

        return ParsedTransaction(
            reference=self.normalize_reference(reference),
            amount=amount,
            direction=direction,
            date=tx_date,
            date_time=tx_datetime,
            narration=narration.strip()[:200],
            actual_balance=actual_balance,
            raw_subject=subject,
            parser_version=self.VERSION,
        )

    def _extract_direction(self, text_lower: str) -> str:
        credit_hits = text_lower.count("credit")
        debit_hits = text_lower.count("debit")
        received = "received" in text_lower or "credited" in text_lower
        sent = "debited" in text_lower or "sent" in text_lower or "paid" in text_lower
        if credit_hits > debit_hits or received:
            return "credit"
        return "debit"

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

    def _extract_narration(self, body: str, direction: str) -> str:
        for pattern in self._NARRATION_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                narr = m.group(1).strip()
                if narr and len(narr) >= 2:
                    return narr
        return f"{'Inflow' if direction == 'credit' else 'Outflow'} via OPay"

    def _extract_balance(self, body: str) -> Optional[float]:
        for pattern in self._BALANCE_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                try:
                    return self.clean_amount(m.group(1))
                except ValueError:
                    continue
        return None

    def _extract_date(self, body: str, fallback: datetime):
        date_formats = [
            "%b %d, %Y %I:%M %p",
            "%B %d, %Y %I:%M %p",
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%B %d, %Y",
            "%Y-%m-%dT%H:%M:%S",
        ]
        for pattern in self._DATE_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                date_str = m.group(1).strip()
                # normalise spacing around AM/PM
                date_str = re.sub(r"\s+", " ", date_str)
                for fmt in date_formats:
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
        return re.sub(r"<[^>]+>", " ", html)
