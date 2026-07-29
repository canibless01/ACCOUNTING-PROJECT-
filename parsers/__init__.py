"""
Parser registry.

Architecture
------------
Two categories of email senders exist:

1. Nigerian bank accounts (Moniepoint, OPay, GTBank, Access, UBA, Zenith,
   First Bank, Fidelity, Stanbic, Polaris, Sterling, FCMB, Wema, Union,
   Heritage, Keystone, Ecobank, CitiBank Nigeria — and any future bank).

   ALL of these use the same NIBSS-standard transaction alert format
   (DR/CR, ₦ amount, Available Balance, Date/Time, Narration).
   They are routed to NigerianBankParser — ONE parser for all of them.

   To add a new bank: the user simply enters the sender email in the account
   settings (e.g. alerts@newbank.com). No code change needed.

2. E-commerce / payment gateways (Selar, Paystack, Flutterwave, Remita …)
   These do NOT use DR/CR or Available Balance — they send "order received"
   emails. They need their own custom parsers registered below.

Fallback rule
-------------
Any sender NOT explicitly registered as an e-commerce sender is assumed to be
a bank and routed to NigerianBankParser. The NigerianBankParser raises
NonTransactionEmail if the email doesn't look like a bank alert (no ₦/DR/CR),
so marketing or welcome emails are silently skipped without creating a
failed_import record.
"""
from parsers.base import BaseParser
from parsers.flutterwave import FlutterwaveParser
from parsers.nigerian_bank import NigerianBankParser
from parsers.paystack import PaystackParser
from parsers.selar import SelarParser


# ── E-commerce senders — explicit custom parsers ───────────────────────────────
# These senders are EXCLUDED from the universal bank fallback.
ECOMMERCE_SENDER_PARSERS: dict[str, type[BaseParser]] = {
    # Selar.co — e-commerce/digital products platform
    "noreply@selar.co":              SelarParser,
    "hello@selar.co":                SelarParser,
    "payments@selar.co":             SelarParser,
    "support@selar.co":              SelarParser,
    "sales@selar.co":                SelarParser,

    # Paystack — payment gateway
    "no-reply@paystack.com":         PaystackParser,
    "noreply@paystack.com":          PaystackParser,
    "support@paystack.com":          PaystackParser,
    "notifications@paystack.com":    PaystackParser,

    # Flutterwave — payment gateway
    "no-reply@flutterwave.com":      FlutterwaveParser,
    "noreply@flutterwave.com":       FlutterwaveParser,
    "support@flutterwave.com":       FlutterwaveParser,
    "hello@flutterwave.com":         FlutterwaveParser,
}

# ── Known bank senders — all route to NigerianBankParser ─────────────────────
# Listing these explicitly is optional (the fallback handles unknown bank senders
# automatically), but it makes the routing intent explicit and speeds up lookups.
BANK_SENDER_PARSERS: dict[str, type[BaseParser]] = {
    # Moniepoint
    "alerts@moniepoint.com":            NigerianBankParser,
    "no-reply@moniepoint.com":          NigerianBankParser,
    "notification@moniepoint.com":      NigerianBankParser,
    "noreply@moniepoint.com":           NigerianBankParser,
    # OPay
    "alerts@opay-inc.com":              NigerianBankParser,
    "noreply@opay-inc.com":             NigerianBankParser,
    "transaction@opay-inc.com":         NigerianBankParser,
    "info@opay-inc.com":                NigerianBankParser,
    # GTBank
    "alerts@gtbank.com":                NigerianBankParser,
    "gtbanknotification@gtbank.com":    NigerianBankParser,
    "noreply@gtbank.com":               NigerianBankParser,
    # Access Bank
    "alerts@accessbankplc.com":         NigerianBankParser,
    "noreply@accessbankplc.com":        NigerianBankParser,
    "notification@accessbankplc.com":   NigerianBankParser,
    # UBA
    "alerts@ubagroup.com":              NigerianBankParser,
    "uba.alert@ubagroup.com":           NigerianBankParser,
    "noreply@ubagroup.com":             NigerianBankParser,
    "no-reply@ubagroup.com":            NigerianBankParser,
    # Zenith Bank
    "alerts@zenithbank.com":            NigerianBankParser,
    "noreply@zenithbank.com":           NigerianBankParser,
    # First Bank
    "firstalerts@firstbanknigeria.com": NigerianBankParser,
    "alerts@firstbanknigeria.com":      NigerianBankParser,
    "noreply@firstbanknigeria.com":     NigerianBankParser,
    # Fidelity Bank
    "alerts@fidelitybank.ng":           NigerianBankParser,
    "noreply@fidelitybank.ng":          NigerianBankParser,
    # Stanbic IBTC
    "alerts@stanbicibtcbank.com":       NigerianBankParser,
    "noreply@stanbicibtcbank.com":      NigerianBankParser,
    # Polaris Bank
    "alerts@polarisbank.com":           NigerianBankParser,
    "noreply@polarisbank.com":          NigerianBankParser,
    # Sterling Bank
    "alerts@sterlingbank.com":          NigerianBankParser,
    "noreply@sterlingbank.com":         NigerianBankParser,
    # FCMB
    "alerts@fcmb.com":                  NigerianBankParser,
    "noreply@fcmb.com":                 NigerianBankParser,
    # Wema Bank
    "alerts@wemabank.com":              NigerianBankParser,
    "noreply@wemabank.com":             NigerianBankParser,
    # Union Bank
    "alerts@unionbankng.com":           NigerianBankParser,
    "noreply@unionbankng.com":          NigerianBankParser,
    # Heritage Bank
    "alerts@hbNigeria.com":             NigerianBankParser,
    # Keystone Bank
    "alerts@keystonebankng.com":        NigerianBankParser,
    # Ecobank Nigeria
    "alerts@ecobank.com":               NigerianBankParser,
    "noreply@ecobank.com":              NigerianBankParser,
    # Kuda Bank
    "hello@kuda.com":                   NigerianBankParser,
    "noreply@kuda.com":                 NigerianBankParser,
    # PalmPay
    "noreply@palmpay-inc.com":          NigerianBankParser,
    "alerts@palmpay-inc.com":           NigerianBankParser,
    # Carbon (One Finance)
    "alerts@carbon.ng":                 NigerianBankParser,
    "noreply@carbon.ng":                NigerianBankParser,
    # VFD Microfinance Bank
    "alerts@vfd.ng":                    NigerianBankParser,
    # PremiumTrust Bank
    "enotification@premiumtrustbank.com": NigerianBankParser,
    "alerts@premiumtrustbank.com":        NigerianBankParser,
    "noreply@premiumtrustbank.com":       NigerianBankParser,
}

# Combined lookup (e-commerce takes precedence over bank lookup)
_ALL_PARSERS: dict[str, type[BaseParser]] = {
    **BANK_SENDER_PARSERS,
    **ECOMMERCE_SENDER_PARSERS,
}

# Set of e-commerce domains (used to block them from the bank fallback)
_ECOMMERCE_DOMAINS: frozenset[str] = frozenset(
    addr.split("@")[-1] for addr in ECOMMERCE_SENDER_PARSERS
)


def get_parser(sender_email: str) -> BaseParser:
    """
    Return an instantiated parser for the given sender email.

    Resolution order:
      1. Exact email match in the combined registry.
      2. Domain-level fuzzy match in the combined registry
         (handles subdomain variants, e.g. uk.alerts@moniepoint.com).
      3. Universal fallback → NigerianBankParser, UNLESS the sender domain
         belongs to a known e-commerce platform (in which case return None
         so the sync engine can log a failed_import with a useful message).

    This function never returns None for bank senders — NigerianBankParser
    is the catch-all. It returns None only when the sender is a known
    e-commerce domain with no registered parser yet.
    """
    normalized = sender_email.lower().strip()
    sender_domain = normalized.split("@")[-1] if "@" in normalized else normalized

    # 1. Exact match
    cls = _ALL_PARSERS.get(normalized)
    if cls:
        return cls()

    # 2. Domain-level fuzzy match
    for known_addr, parser_cls in _ALL_PARSERS.items():
        known_domain = known_addr.split("@")[-1]
        if known_domain == sender_domain:
            return parser_cls()

    # 3. Fallback — if the domain is a known e-commerce platform but has no
    #    registered parser, return None so the sync engine can surface a
    #    helpful "no e-commerce parser" error rather than garbled bank data.
    if sender_domain in _ECOMMERCE_DOMAINS:
        return None  # type: ignore[return-value]

    # 4. Universal bank fallback — handles any bank not listed above.
    return NigerianBankParser()
