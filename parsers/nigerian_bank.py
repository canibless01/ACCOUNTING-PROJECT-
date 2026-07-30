"""
Universal Nigerian Bank Parser
==============================
Covers Moniepoint, OPay, GTBank, Access, UBA, Zenith, First Bank, Fidelity,
Stanbic, Polaris, Sterling, FCMB, Wema, Union Bank, Heritage, Keystone,
Ecobank Nigeria, CitiBank Nigeria, and any other bank running Finacle,
BankOne, or T24 core banking — because they all emit the same NIBSS-standard
transaction alert structure.

Architecture note
-----------------
You do NOT add a new parser for each new bank.
The user simply adds the sender email (e.g. alerts@gtbank.com) to an account.
This parser handles it automatically via universal pattern matching.

Custom parsers (SelarParser, PaystackParser, etc.) exist ONLY for e-commerce /
payment-gateway senders that do not use DR/CR / Available Balance conventions.
"""
from __future__ import annotations

import re
from datetime import datetime
from typing import Optional

from parsers.base import BaseParser, NonTransactionEmail, ParseError, ParsedTransaction


class NigerianBankParser(BaseParser):
    """
    Universal parser for Nigerian bank transaction alert emails.

    Detection strategy (in order):
      1. Direction  — scan for DR/CR/Debit/Credit/Inflow/Outflow keywords.
      2. Amount     — find the largest ₦/NGN-attached number in the body.
      3. Reference  — scan for labelled Session ID / Reference / Trx Ref / etc.
      4. Narration  — find Narration: / Description: / Remarks: label value.
      5. Balance    — find Available Bal / Ledger Bal / Avail Bal / Closing Bal.
      6. Date/Time  — find Date: / Time: label or any standard datetime pattern.

    If no direction AND no ₦ amount is found, the email is not a bank alert
    and NonTransactionEmail is raised (silently skipped, no failed_import).
    """

    VERSION = 1  # Bump when patterns change; logged on every transaction row.

    # ── Direction ──────────────────────────────────────────────────────────────
    # We score credit vs debit keywords. Whichever score is higher wins.
    # Ties default to debit (conservative — avoids false income inflation).
    _CREDIT_KW = re.compile(
        r"\b(CR|Cr|Credit(?:ed)?|Inflow|Received|Lodgement|Deposit(?:ed)?)\b",
        re.IGNORECASE,
    )
    _DEBIT_KW = re.compile(
        r"\b(DR|Dr|Debit(?:ed)?|Outflow|Withdrawal|Charge(?:d)?|Payment\s+Made|"
        r"Transfer\s+Out|POS\s+Purchase|ATM\s+Withdrawal)\b",
        re.IGNORECASE,
    )

    # Subject-level signal (highest confidence — many banks put "Credit Alert" in subject)
    _CREDIT_SUBJECT = re.compile(r"\b(credit|inflow|received)\b", re.IGNORECASE)
    _DEBIT_SUBJECT  = re.compile(r"\b(debit|outflow|withdrawal|charge)\b", re.IGNORECASE)

    # ── Amount ─────────────────────────────────────────────────────────────────
    # Match ₦1,234.56 or NGN1,234.56 or N1,234.56 (common bank shorthand)
    # Also matches "Amount: 1,234.56" labeled forms without currency prefix.
    _AMOUNT_PATTERNS = [
        # Currency-symbol attached: ₦1,234,567.89
        re.compile(r"[₦N]\s*([\d,]+\.?\d*)", re.IGNORECASE),
        # NGN prefix
        re.compile(r"NGN\s*([\d,]+\.?\d*)", re.IGNORECASE),
        # Labeled: Amount: 1,234.56
        re.compile(r"(?:Amount|Amt|Sum)[:\s]+[₦N]?\s*([\d,]+\.?\d*)", re.IGNORECASE),
    ]

    # ── Reference ──────────────────────────────────────────────────────────────
    _REF_PATTERNS = [
        re.compile(r"Session\s+ID[:\s]+([A-Z0-9/_\-]+)", re.IGNORECASE),
        re.compile(r"(?:Transaction|Trx|Trans)\s+(?:Ref(?:erence)?|ID)[:\s]+([A-Z0-9/_\-]+)", re.IGNORECASE),
        re.compile(r"Ref(?:erence)?[:\s]+([A-Z0-9/_\-]{4,})", re.IGNORECASE),
        re.compile(r"(?:Auth(?:orization)?|Approval)\s+Code[:\s]+([A-Z0-9]+)", re.IGNORECASE),
        re.compile(r"(?:Trace|Sequence)\s+(?:No\.?|Number)[:\s]+([A-Z0-9/_\-]+)", re.IGNORECASE),
        # Standalone codes that look like bank references (≥8 uppercase alphanumeric)
        re.compile(r"\b([A-Z]{2,4}[\d]{8,})\b"),
    ]

    # ── Narration ──────────────────────────────────────────────────────────────
    _NARRATION_PATTERNS = [
        re.compile(r"(?:Narration|Naration)[:\s]+(.+?)(?:\n|Ref|Balance|Available|Date|Amount|$)", re.IGNORECASE | re.DOTALL),
        re.compile(r"Description[:\s]+(.+?)(?:\n|Ref|Balance|Available|Date|Amount|$)", re.IGNORECASE | re.DOTALL),
        re.compile(r"Remarks?[:\s]+(.+?)(?:\n|Ref|Balance|Available|Date|Amount|$)", re.IGNORECASE | re.DOTALL),
        re.compile(r"Details?[:\s]+(.+?)(?:\n|Ref|Balance|Available|Date|Amount|$)", re.IGNORECASE | re.DOTALL),
        # Transfer from/to patterns common in bank alerts
        re.compile(r"(Transfer\s+(?:from|to|FROM|TO)\s+.+?)(?:\n|Ref|Balance|Date|$)", re.IGNORECASE | re.DOTALL),
        re.compile(r"(Payment\s+(?:from|to|FROM|TO)\s+.+?)(?:\n|Ref|Balance|Date|$)", re.IGNORECASE | re.DOTALL),
        # Sender/Recipient labels
        re.compile(r"(?:Sender|Originator|Beneficiary|Recipient)[:\s]+(.+?)(?:\n|Ref|Balance|Date|$)", re.IGNORECASE | re.DOTALL),
    ]

    # ── Balance ────────────────────────────────────────────────────────────────
    # NIBSS standard: every bank alert has one of these.
    # Handles both "Available Balance: ₦1,810.15" and PremiumTrust style
    # "Available Balance  :NGN 1,810.15" (colon and NGN after the label).
    _BAL_SUFFIX = r"[:\s]+[:\s]*(?:NGN|₦|N)?\s*([\d,]+\.?\d*)"
    _BALANCE_PATTERNS = [
        re.compile(r"Available\s+Bal(?:ance)?" + r"[:\s]+[:\s]*(?:NGN|₦|N)?\s*([\d,]+\.?\d*)", re.IGNORECASE),
        re.compile(r"Avail(?:able)?\s+Bal(?:ance)?" + r"[:\s]+[:\s]*(?:NGN|₦|N)?\s*([\d,]+\.?\d*)", re.IGNORECASE),
        re.compile(r"Ledger\s+Bal(?:ance)?" + r"[:\s]+[:\s]*(?:NGN|₦|N)?\s*([\d,]+\.?\d*)", re.IGNORECASE),
        re.compile(r"Closing\s+Bal(?:ance)?" + r"[:\s]+[:\s]*(?:NGN|₦|N)?\s*([\d,]+\.?\d*)", re.IGNORECASE),
        re.compile(r"Current\s+Bal(?:ance)?" + r"[:\s]+[:\s]*(?:NGN|₦|N)?\s*([\d,]+\.?\d*)", re.IGNORECASE),
        re.compile(r"Account\s+Bal(?:ance)?" + r"[:\s]+[:\s]*(?:NGN|₦|N)?\s*([\d,]+\.?\d*)", re.IGNORECASE),
        # Generic fallback — "Balance: ₦12,000" anywhere in body
        re.compile(r"Bal(?:ance)?" + r"[:\s]+[:\s]*(?:NGN|₦|N)?\s*([\d,]+\.?\d*)", re.IGNORECASE),
    ]

    # ── Date/Time ──────────────────────────────────────────────────────────────
    # _DATE_LABEL matches "Date", "Time", "Time of Transaction", "Value Date", etc.
    _DATE_LABEL = r"(?:Time\s+of\s+Transaction|Value\s+Date|Date|Time)"

    _DATETIME_PATTERNS = [
        # DD/MM/YYYY HH:MM:SS [AM/PM]  or  DD/MM/YYYY HH:MM [AM/PM]
        re.compile(r"(?:Time\s+of\s+Transaction|Date|Time)[:\s]+(\d{1,2}/\d{1,2}/\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)", re.IGNORECASE),
        # DD-Mon-YYYY HH:MM:SS [AM/PM]  — PremiumTrust: "28-JUL-2026 07:04:21 PM"
        re.compile(r"(?:Time\s+of\s+Transaction|Date|Time)[:\s]+(\d{1,2}-\w{3,9}-\d{4}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)", re.IGNORECASE),
        # DD-Mon-YY HH:MM:SS [AM/PM]  — 2-digit year fallback
        re.compile(r"(?:Time\s+of\s+Transaction|Date|Time)[:\s]+(\d{1,2}-\w{3,9}-\d{2}\s+\d{1,2}:\d{2}(?::\d{2})?(?:\s*[AaPp][Mm])?)", re.IGNORECASE),
        # ISO 8601
        re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})"),
        # Standalone date DD/MM/YYYY or DD-Mon-YYYY or DD-Mon-YY
        re.compile(r"(?:Value\s+Date|Date)[:\s]+(\d{1,2}/\d{1,2}/\d{4})", re.IGNORECASE),
        re.compile(r"(?:Value\s+Date|Date)[:\s]+(\d{1,2}-\w{3,9}-\d{4})", re.IGNORECASE),
        re.compile(r"(?:Value\s+Date|Date)[:\s]+(\d{1,2}-\w{3,9}-\d{2})", re.IGNORECASE),
        # "Jan 15, 2024 02:35 PM" or "January 15, 2024"
        re.compile(r"(?:Date|Time)[:\s]+(\w{3,9}\s+\d{1,2},\s*\d{4}(?:\s+\d{1,2}:\d{2}(?:\s*[AaPp][Mm])?)?)", re.IGNORECASE),
    ]
    _DATE_FORMATS = [
        # 24-hour formats — 4-digit year
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d-%b-%Y %H:%M:%S",
        "%d-%b-%Y %H:%M",
        "%d-%B-%Y %H:%M:%S",
        "%d-%B-%Y %H:%M",
        "%d/%m/%Y",
        "%d-%b-%Y",
        "%d-%B-%Y",
        "%Y-%m-%dT%H:%M:%S",
        # 12-hour AM/PM formats — 4-digit year (must come after 24-hour)
        "%d/%m/%Y %I:%M:%S %p",
        "%d/%m/%Y %I:%M %p",
        "%d-%b-%Y %I:%M:%S %p",
        "%d-%b-%Y %I:%M %p",
        "%d-%B-%Y %I:%M:%S %p",
        "%d-%B-%Y %I:%M %p",
        "%B %d, %Y %I:%M %p",
        "%b %d, %Y %I:%M %p",
        "%B %d, %Y",
        "%b %d, %Y",
        # 2-digit year variants (PremiumTrust Value Date: "28-JUL-26")
        "%d-%b-%y %H:%M:%S",
        "%d-%b-%y %H:%M",
        "%d-%b-%y %I:%M:%S %p",
        "%d-%b-%y %I:%M %p",
        "%d-%b-%y",
        "%d-%B-%y",
    ]

    # ── Non-transaction emails to silently skip ────────────────────────────────
    # These are common bank emails that are NOT transaction alerts.
    _SKIP_SUBJECT_KW = re.compile(
        r"\b(OTP|Password\s+Reset|Verify|KYC|Upgrade|Statement|Welcome|"
        r"Newsletter|Login\s+Alert|Sign(?:\s*|-)?in|PIN\s+Change|Scheduled\s+Maintenance|"
        r"Activate\s+Your|Account\s+Opening|Interest\s+Credit\s+Notice)\b",
        re.IGNORECASE,
    )

    # ── Minimum signal required to attempt parsing ─────────────────────────────
    # Body must have at least ONE of these to be treated as a transaction alert.
    _TX_SIGNAL = re.compile(
        r"[₦N]\s*[\d,]+|NGN\s*[\d,]+|\b(DR|CR|Debit|Credit|Inflow|Outflow)\b",
        re.IGNORECASE,
    )

    # ─────────────────────────────────────────────────────────────────────────
    def parse_email(
        self,
        subject: str,
        body_plain: str,
        body_html: str,
        received_at: datetime,
    ) -> ParsedTransaction:
        # PremiumTrust (and similar HTML-only banks) sometimes have Gmail return
        # the HTML source as the text/plain MIME part.  Detect this and strip it
        # so the regex patterns see clean text regardless of MIME structure.
        raw_plain = body_plain.strip() if body_plain else ""
        if raw_plain and self._looks_like_html(raw_plain):
            body = self._strip_html(raw_plain)
        elif raw_plain:
            body = raw_plain
        else:
            body = self._strip_html(body_html)
        combined = f"{subject}\n{body}"

        # ── 1. Skip obviously non-transaction emails ───────────────────────────
        if self._SKIP_SUBJECT_KW.search(subject):
            raise NonTransactionEmail(
                f"NigerianBankParser: skipping non-transaction email: '{subject}'"
            )

        # ── 2. Require minimum transaction signal in body ─────────────────────
        if not self._TX_SIGNAL.search(combined):
            raise NonTransactionEmail(
                f"NigerianBankParser: no transaction signals (₦/DR/CR) found in '{subject}'"
            )

        # ── 3. Direction ──────────────────────────────────────────────────────
        direction = self._detect_direction(subject, body)

        # ── 4. Amount ─────────────────────────────────────────────────────────
        amount = self._extract_amount(body)
        if amount is None or amount <= 0:
            raise ParseError(
                f"NigerianBankParser: could not extract a valid amount from email '{subject}'"
            )

        # Guard against misparses where a NUBAN account number, reference ID, or
        # balance (e.g. "N1234567890123") is picked up as the transaction amount.
        # ₦100 billion is the practical upper bound for a real personal transaction.
        # Anything above that is almost certainly a 10-13 digit numeric string that
        # happens to be preceded by "N" or "₦" — raise ParseError so it lands in
        # failed_imports rather than crashing the DB with a numeric overflow.
        if amount >= 100_000_000_000:
            raise ParseError(
                f"NigerianBankParser: parsed amount ₦{amount:,.2f} exceeds sanity limit "
                f"(likely a misparse of an account/reference number) in '{subject}'"
            )

        # ── 5. Reference ──────────────────────────────────────────────────────
        reference = self._extract_reference(body)
        if not reference:
            # Synthesize from received_at + amount (unique enough for our purposes)
            reference = f"BANK-{received_at.strftime('%Y%m%d%H%M%S')}-{int(amount)}"

        # ── 6. Narration ──────────────────────────────────────────────────────
        narration = self._extract_narration(body)
        if not narration:
            narration = f"{'Credit' if direction == 'credit' else 'Debit'} alert"

        # ── 7. Balance ────────────────────────────────────────────────────────
        actual_balance = self._extract_balance(body)

        # ── 8. Date ───────────────────────────────────────────────────────────
        tx_datetime, tx_date = self._extract_datetime(body, received_at)

        return ParsedTransaction(
            reference=self.normalize_reference(reference),
            amount=amount,
            direction=direction,
            date=tx_date,
            date_time=tx_datetime,
            narration=narration[:300].strip(),
            actual_balance=actual_balance,
            raw_subject=subject,
            parser_version=self.VERSION,
        )

    # ── Private helpers ────────────────────────────────────────────────────────

    def _detect_direction(self, subject: str, body: str) -> str:
        """
        Score credit vs debit keywords across subject + body.
        Subject keywords carry 3× weight (banks put 'Credit Alert' in subject explicitly).
        """
        credit_score = (
            3 * len(self._CREDIT_SUBJECT.findall(subject)) +
            len(self._CREDIT_KW.findall(body))
        )
        debit_score = (
            3 * len(self._DEBIT_SUBJECT.findall(subject)) +
            len(self._DEBIT_KW.findall(body))
        )
        return "credit" if credit_score > debit_score else "debit"

    def _extract_amount(self, body: str) -> Optional[float]:
        """
        Find ALL currency-attached numbers in the body, return the largest.
        The transaction amount is almost always the largest ₦ figure in the email.
        (Balance figures are also large, but the amount is usually stated first.)

        If labeled patterns match first, prefer the labeled value.
        """
        # Try labeled patterns first (most precise)
        for pat in self._AMOUNT_PATTERNS[2:]:  # Amount:/Amt:/Sum: patterns
            m = pat.search(body)
            if m:
                try:
                    val = self.clean_amount(m.group(1))
                    if val > 0:
                        return val
                except ValueError:
                    pass

        # Fall back: collect all ₦/NGN numbers and return the first (largest by position
        # heuristic — amount typically appears before balance in the email body)
        candidates = []
        for pat in self._AMOUNT_PATTERNS[:2]:
            for m in pat.finditer(body):
                try:
                    val = self.clean_amount(m.group(1))
                    if val > 0:
                        candidates.append((m.start(), val))
                except ValueError:
                    pass

        if not candidates:
            return None

        # Return the FIRST currency-attached number (appears before available balance)
        candidates.sort(key=lambda x: x[0])
        return candidates[0][1]

    def _extract_reference(self, body: str) -> Optional[str]:
        for pat in self._REF_PATTERNS:
            m = pat.search(body)
            if m:
                ref = m.group(1).strip()
                if len(ref) >= 4:
                    return ref
        return None

    def _extract_narration(self, body: str) -> Optional[str]:
        for pat in self._NARRATION_PATTERNS:
            m = pat.search(body)
            if m:
                text = m.group(1).strip()
                # Remove embedded newlines and collapse whitespace
                text = re.sub(r"\s+", " ", text).strip()
                if text and len(text) >= 2:
                    return text[:300]
        return None

    def _extract_balance(self, body: str) -> Optional[float]:
        for pat in self._BALANCE_PATTERNS:
            m = pat.search(body)
            if m:
                try:
                    val = self.clean_amount(m.group(1))
                    if val >= 0:
                        return val
                except ValueError:
                    continue
        return None

    def _extract_datetime(
        self, body: str, fallback: datetime
    ) -> tuple[Optional[datetime], "date"]:
        from datetime import date as dt_date
        for pat in self._DATETIME_PATTERNS:
            m = pat.search(body)
            if m:
                date_str = m.group(1).strip()
                # Normalise AM/PM: ensure uppercase and exactly one space before it.
                # Handles "6:16:49 PM", "6:16:49PM", "6:16:49 pm", "6:16:49pm" → "6:16:49 PM"
                date_str = re.sub(
                    r"\s*([AaPp][Mm])$",
                    lambda x: " " + x.group(1).upper(),
                    date_str,
                )
                # Collapse any remaining double spaces
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
        """
        Convert an HTML email body to plain text suitable for regex parsing.

        Critical steps (in order):
          1. Strip <head> — removes CSS/meta noise before any processing.
          2. Strip <script>/<style> blocks.
          3. Replace block-level / table-structure tags with whitespace so
             adjacent values don't run together (e.g. "Amount:₦500" vs "Amount: ₦500").
          4. Strip all remaining HTML tags.
          5. Decode HTML entities — THIS is the step that converts the encoded
             Naira symbol &#8358; back to ₦ (and &nbsp;, &amp;, etc.).
             Without this step every HTML-only bank email fails because the ₦
             regex never matches the raw entity string.
          6. Collapse whitespace.
        """
        import html as _html_mod
        if not html:
            return ""
        # 1. Remove entire <head> section (CSS, meta, title = pure noise for parsing)
        html = re.sub(r"<head[^>]*>.*?</head>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        # 2. Remove script/style blocks
        html = re.sub(r"<(script|style)[^>]*>.*?</(script|style)>", " ", html, flags=re.DOTALL | re.IGNORECASE)
        # 3a. Block-level tags → newline (preserves label-value line separation)
        html = re.sub(r"<(?:br|p|div|tr|li|h[1-6])[^>]*>", "\n", html, flags=re.IGNORECASE)
        # 3b. Table cells → space (so "Amount:₦500" doesn't become "Amount:₦500" with no gap)
        html = re.sub(r"<(?:td|th)[^>]*>", " ", html, flags=re.IGNORECASE)
        # 4. Strip all remaining tags
        html = re.sub(r"<[^>]+>", " ", html)
        # 5. Decode HTML entities: &#8358; → ₦, &nbsp; → space, &amp; → &, etc.
        html = _html_mod.unescape(html)
        # 6. Collapse horizontal whitespace; reduce excessive blank lines
        html = re.sub(r"[ \t]+", " ", html)
        html = re.sub(r"\n{3,}", "\n\n", html)
        return html.strip()
