"""
Abstract base parser.
Every sender-specific parser subclasses this and implements parse_email().
"""
from __future__ import annotations

import hashlib
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Optional


@dataclass
class ParsedTransaction:
    """Result of a successful email parse."""
    reference: str                  # Unique transaction reference / ID
    amount: float                   # Absolute value (always positive)
    direction: str                  # 'credit' or 'debit'
    date: date                      # Transaction date
    date_time: Optional[datetime]   # Full datetime if available
    narration: str                  # Description / narration text
    actual_balance: Optional[float] # Balance shown in email after the tx (if present)
    raw_subject: str                # Email subject (for audit)
    parser_version: int             # Parser version that produced this result
    extra: dict = field(default_factory=dict)  # Any extra extracted fields

    def fingerprint(self, account_id: str) -> str:
        """Build duplicate-detection fingerprint."""
        raw = f"{account_id}|{self.reference}|{self.amount:.2f}|{self.date.isoformat()}"
        return hashlib.md5(raw.encode()).hexdigest()


class ParseError(Exception):
    """Raised when an email cannot be parsed as a transaction."""
    pass


class NonTransactionEmail(Exception):
    """Raised when the email is intentionally not a transaction (e.g. abandoned cart)."""
    pass


class BaseParser(ABC):
    """
    Base class for all sender parsers.
    Subclasses must implement parse_email().
    """
    VERSION: int = 1  # Bump this in subclasses when regex patterns change

    @abstractmethod
    def parse_email(self, subject: str, body_plain: str, body_html: str, received_at: datetime) -> ParsedTransaction:
        """
        Parse the email and return a ParsedTransaction.
        Raise ParseError if parsing fails.
        Raise NonTransactionEmail if this email should be silently skipped.
        """
        raise NotImplementedError

    @staticmethod
    def clean_amount(raw: str) -> float:
        """Convert '₦1,234,567.89' or '1234567.89' → float."""
        cleaned = raw.replace("₦", "").replace(",", "").replace(" ", "").strip()
        return float(cleaned)

    @staticmethod
    def normalize_reference(ref: str) -> str:
        """Strip leading/trailing whitespace and uppercase the reference."""
        return ref.strip().upper()

    @staticmethod
    def _first_match(pattern_list, text: str) -> re_match_type | None:
        """Try each compiled regex in order; return the first match."""
        import re
        for pat in pattern_list:
            m = re.search(pat, text, re.IGNORECASE | re.DOTALL)
            if m:
                return m
        return None


# Type alias used internally
import re
re_match_type = type(re.match("", ""))
