"""
Moniepoint email parser.
Handles credit and debit transaction notification emails from Moniepoint.

Sample Moniepoint credit email:
  Subject: Credit Alert - Moniepoint
  Body: You have received a credit of ₦15,000.00 into your Moniepoint account.
        Account Number: 8012345678
        Amount: ₦15,000.00
        Narration: Transfer from JOHN DOE
        Transaction Ref: TXN20240115123456
        Available Balance: ₦45,230.50
        Date: 15/01/2024 14:35:22

Sample Moniepoint debit email:
  Subject: Debit Alert - Moniepoint
  Body: A debit of ₦5,000.00 has been made from your Moniepoint account.
        Narration: POS Purchase at SHOPRITE
        Transaction Ref: TXN20240115124000
        Available Balance: ₦40,230.50
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from parsers.base import BaseParser, NonTransactionEmail, ParseError, ParsedTransaction


class MoniepointParser(BaseParser):
    VERSION = 2

    # ── Amount patterns ───────────────────────────────────────────────────────
    _AMOUNT_PATTERNS = [
        r"(?:credit|debit|amount)\s+of\s+[₦N]?\s*([\d,]+\.?\d*)",
        r"[₦N]\s*([\d,]+\.?\d*)\s+(?:has been|into|from)",
        r"Amount[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
        r"Sum[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
    ]

    # ── Direction patterns ────────────────────────────────────────────────────
    _CREDIT_PATTERNS = [
        r"credit",
        r"received",
        r"inflow",
        r"money received",
        r"you have been credited",
    ]
    _DEBIT_PATTERNS = [
        r"debit",
        r"debited",
        r"outflow",
        r"transfer out",
        r"payment made",
        r"POS purchase",
        r"withdrawal",
    ]

    # ── Reference patterns ────────────────────────────────────────────────────
    _REF_PATTERNS = [
        r"Transaction\s+Ref(?:erence)?[:\s]+([A-Z0-9/_\-]+)",
        r"Ref(?:erence)?[:\s]+([A-Z0-9/_\-]+)",
        r"TXN[:\s]*([\d]+)",
        r"Transaction\s+ID[:\s]+([A-Z0-9/_\-]+)",
        r"Session\s+ID[:\s]+([A-Z0-9/_\-]+)",
    ]

    # ── Narration patterns ────────────────────────────────────────────────────
    _NARRATION_PATTERNS = [
        r"Narration[:\s]+(.+?)(?:\n|Transaction|Ref|Balance|Date|$)",
        r"Description[:\s]+(.+?)(?:\n|Transaction|Ref|Balance|Date|$)",
        r"Remark[:\s]+(.+?)(?:\n|Transaction|Ref|Balance|Date|$)",
        r"(?:Transfer from|Transfer to|Payment from|Payment to)[:\s]+(.+?)(?:\n|$)",
    ]

    # ── Balance patterns ──────────────────────────────────────────────────────
    _BALANCE_PATTERNS = [
        r"Available\s+Balance[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
        r"Closing\s+Balance[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
        r"Account\s+Balance[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
        r"Balance[:\s]+[₦N]?\s*([\d,]+\.?\d*)",
    ]

    # ── Date patterns ─────────────────────────────────────────────────────────
    _DATE_PATTERNS = [
        r"Date[:\s]+(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)",
        r"Date[:\s]+(\d{1,2}-\w+-\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)",
        r"Transaction\s+Date[:\s]+(\d{1,2}/\d{1,2}/\d{4})",
        r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})",
    ]

    # ── Non-transaction email keywords (skip these) ───────────────────────────
    _NON_TX_SUBJECTS = [
        "welcome",
        "your account has been created",
        "password reset",
        "login otp",
        "verification",
        "kyc",
        "upgrade",
        "monthly statement",
        "newsletter",
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

        # ── Filter non-transaction emails ─────────────────────────────────────
        for skip_kw in self._NON_TX_SUBJECTS:
            if skip_kw in subject_lower:
                raise NonTransactionEmail(f"Moniepoint: skipping non-transaction email '{subject}'")

        if not any(kw in subject_lower or kw in body.lower() for kw in ["credit", "debit", "alert"]):
            raise NonTransactionEmail(f"Moniepoint: no transaction keywords found in '{subject}'")

        # ── Direction ─────────────────────────────────────────────────────────
        combined = f"{subject} {body}"
        direction = self._extract_direction(combined)

        # ── Amount ────────────────────────────────────────────────────────────
        amount = self._extract_amount(body)
        if amount is None:
            raise ParseError(f"Moniepoint: could not extract amount from body")

        # ── Reference ─────────────────────────────────────────────────────────
        reference = self._extract_reference(body)
        if not reference:
            # Fall back: build from subject + received_at
            reference = f"MNPT-{received_at.strftime('%Y%m%d%H%M%S')}-{int(amount)}"

        # ── Narration ─────────────────────────────────────────────────────────
        narration = self._extract_narration(body) or (
            f"{'Credit' if direction == 'credit' else 'Debit'} via Moniepoint"
        )

        # ── Balance ───────────────────────────────────────────────────────────
        actual_balance = self._extract_balance(body)

        # ── Date ──────────────────────────────────────────────────────────────
        tx_datetime, tx_date = self._extract_date(body, received_at)

        return ParsedTransaction(
            reference=self.normalize_reference(reference),
            amount=amount,
            direction=direction,
            date=tx_date,
            date_time=tx_datetime,
            narration=narration.strip(),
            actual_balance=actual_balance,
            raw_subject=subject,
            parser_version=self.VERSION,
        )

    # ── Private helpers ───────────────────────────────────────────────────────

    def _extract_direction(self, text: str) -> str:
        text_lower = text.lower()
        credit_score = sum(1 for kw in self._CREDIT_PATTERNS if re.search(kw, text_lower))
        debit_score = sum(1 for kw in self._DEBIT_PATTERNS if re.search(kw, text_lower))
        return "credit" if credit_score >= debit_score else "debit"

    def _extract_amount(self, body: str) -> Optional[float]:
        for pattern in self._AMOUNT_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                try:
                    return self.clean_amount(m.group(1))
                except ValueError:
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

    def _extract_narration(self, body: str) -> Optional[str]:
        for pattern in self._NARRATION_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                narration = m.group(1).strip()
                if narration and len(narration) >= 2:
                    return narration[:200]
        return None

    def _extract_balance(self, body: str) -> Optional[float]:
        for pattern in self._BALANCE_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                try:
                    return self.clean_amount(m.group(1))
                except ValueError:
                    continue
        return None

    def _extract_date(self, body: str, fallback: datetime) -> tuple[Optional[datetime], "date"]:
        date_formats = [
            # 24-hour
            "%d/%m/%Y %H:%M:%S",
            "%d/%m/%Y %H:%M",
            "%d-%b-%Y %H:%M:%S",
            "%d-%b-%Y %H:%M",
            "%d/%m/%Y",
            "%Y-%m-%dT%H:%M:%S",
            # 12-hour AM/PM
            "%d/%m/%Y %I:%M:%S %p",
            "%d/%m/%Y %I:%M %p",
            "%d-%b-%Y %I:%M:%S %p",
            "%d-%b-%Y %I:%M %p",
        ]
        for pattern in self._DATE_PATTERNS:
            m = re.search(pattern, body, re.IGNORECASE)
            if m:
                date_str = m.group(1).strip()
                # Normalise AM/PM to uppercase with a single space before it
                date_str = re.sub(r"\s*([AaPp][Mm])$", lambda x: " " + x.group(1).upper(), date_str)
                date_str = re.sub(r"\s{2,}", " ", date_str).strip()
                for fmt in date_formats:
                    try:
                        dt = datetime.strptime(date_str, fmt)
                        return dt, dt.date()
                    except ValueError:
                        continue
        return fallback, fallback.date()

    @staticmethod
    def _strip_html(html: str) -> str:
        """Very basic HTML tag stripper."""
        if not html:
            return ""
        return re.sub(r"<[^>]+>", " ", html)
