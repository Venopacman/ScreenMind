"""
Sensitive Data Filter
Detects and redacts credit cards, SSNs, API keys, passwords, emails, phone
numbers and IBANs from screen text and recorded UI events (typed text,
clipboard, element values) before it's stored in the database or passed
to AI models.
"""

import logging
import re
from typing import Optional

logger = logging.getLogger("screenmind.privacy.data_filter")


# ── Pattern Definitions ──────────────────────────────────────────────

def _luhn_check(number: str) -> bool:
    """Validate a credit card number using the Luhn algorithm."""
    digits = [int(d) for d in number if d.isdigit()]
    if len(digits) < 13 or len(digits) > 19:
        return False
    checksum = 0
    for i, d in enumerate(reversed(digits)):
        if i % 2 == 1:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


# ── Passwords ────────────────────────────────────────────────────────

_SECRET_LABEL = r"(?:password|passwd|passcode|pwd|secret|token|api.?key)"
_DASHES = "\\-\u2010\u2011\u2012\u2013\u2014\u2212"
_REDACTED_MARK = "[REDACTED:"

# "password: value", "DB_PASSWORD=value". The separator says a value
# follows, so any 4+ chars count. The value may be on the next line.
_PASSWORD_STRICT = re.compile(
    r"(?i)(?P<label>" + _SECRET_LABEL + r"\s*[:=]\s*)"
    r"(?P<value>[\"']?\S{4,})"
)

# "pwd - value", "password  value", and OCR putting the value on the next
# line ("pwd" / "- value"). Prose uses these forms too ("password reset"),
# so the value must also pass _looks_like_secret().
_PASSWORD_LOOSE = re.compile(
    r"(?i)(?<![a-z])(?P<label>" + _SECRET_LABEL + r"(?=[\s" + _DASHES + r"])"
    r"[ \t]*(?:\r?\n)?[ \t]*(?:[" + _DASHES + r"]+[ \t]*(?:\r?\n)?[ \t]*)?)"
    r"(?P<value>[^\s" + _DASHES + r"]\S{5,})"
)

# A label with nothing after it yet ("pwd -", "Password:"). The recorder
# uses it to join typed text split by Enter or a pause before the value.
_DANGLING_LABEL = re.compile(
    r"(?i)(?<![a-z])" + _SECRET_LABEL + r"[ \t]*(?:[:=" + _DASHES + r"]+[ \t]*)?$"
)

_SYMBOLS = re.compile(r"[!@#$%^&*_+=~|\\]")


def _looks_like_secret(value: str) -> bool:
    """Is a word after "password" without ":"/"=" a credential or prose?
    Credentials here have a digit or a symbol (abc123, Tr0ub4dor, hunter_x).
    Plain words don't, including CamelCase names ("Reset password - LinkedIn"),
    and neither do dates or URLs."""
    v = value.strip("\"'").rstrip(".,;:!?)]}>\"'")
    if len(v) < 6 or "REDACTED" in v:
        return False
    if "://" in v or v.lower().startswith("www."):
        return False
    if re.fullmatch(r"[\d.,:/\-]+", v) and not v.isdigit():
        return False  # dates, times, amounts
    return any(c.isdigit() for c in v) or bool(_SYMBOLS.search(v))


def _redact_passwords(text: str, replacement: str) -> tuple:
    """Replace password values, keeping the label ("pwd - [REDACTED:password]").
    Returns (text, count)."""
    count = 0

    def _sub(m, check):
        nonlocal count
        value = m.group("value")
        if _REDACTED_MARK in value or (check and not _looks_like_secret(value)):
            return m.group()
        count += 1
        return m.group("label") + replacement

    text = _PASSWORD_STRICT.sub(lambda m: _sub(m, False), text)
    text = _PASSWORD_LOOSE.sub(lambda m: _sub(m, True), text)
    return text, count


# ── Emails, phones, IBANs ────────────────────────────────────────────

# The lookbehind keeps "[REDACTED:email@example.com]" from being matched
# again when stored text is filtered a second time (export).
_EMAIL = re.compile(
    r"(?<!\[REDACTED:)\b[A-Za-z0-9._%+\-]+@(?P<domain>[A-Za-z0-9.\-]+\.[A-Za-z]{2,})\b"
)
# "icon@2x.png" is a file name, not an address.
_FILE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "svg", "webp", "ico", "pdf"}
# OCR reads Slack mentions as "Hi@first.lastname", so the "domain" can be a
# person's name. Keep the domain only for country codes (2 letters) and
# these common endings.
_KEEP_DOMAIN_TLDS = {
    "com", "org", "net", "edu", "gov", "mil", "int", "io", "ai", "dev", "app",
    "info", "biz", "tech", "cloud", "xyz", "online", "site", "email", "team",
}


def _redact_emails(text: str, replacement: str) -> tuple:
    """Replace emails but keep the domain ("[REDACTED:email@example.com]"),
    so internal and external contacts can still be told apart.
    Returns (text, count)."""
    count = 0

    def _sub(m):
        nonlocal count
        domain = m.group("domain").lower()
        tld = domain.rsplit(".", 1)[-1]
        if tld in _FILE_EXTENSIONS:
            return m.group()
        count += 1
        if len(tld) == 2 or tld in _KEEP_DOMAIN_TLDS:
            return replacement[:-1] + "@" + domain + "]"
        return replacement

    return _EMAIL.sub(_sub, text), count


# Only written forms that look like a phone number: with a "+" country
# code, brackets or separators. Bare digit runs are left alone, because
# screen text is full of IDs, amounts and timestamps.
_PHONE = re.compile(
    r"(?<![\w+\-/.:])(?:"
    r"\+\d{1,3}(?:[ .\-]?\(?\d{1,4}\)?){2,5}"          # +7 999 123-45-67, +1 (415) 555-0132
    r"|\(\d{3}\)[ .\-]?\d{3}[ .\-]\d{4}"                 # (415) 555-0132
    r"|\d{3}[.\-]\d{3}[.\-]\d{4}"                         # 415-555-0132
    r"|8 ?\(\d{3}\) ?\d{3}[ \-]?\d{2}[ \-]?\d{2}"         # 8 (999) 123-45-67
    r"|8[ \-]\d{3}[ \-]\d{3}[ \-]\d{2}[ \-]\d{2}"         # 8-999-123-45-67
    r")(?![\w\-/]|\.\w)"
)


def _phone_digits_ok(number: str) -> bool:
    digits = sum(c.isdigit() for c in number)
    return 8 <= digits <= 15


# Country code, check digits, then 4-char groups, spaced or not. The match
# may run into the next word ("... 00 TEST"), so _redact_ibans() tries
# shorter prefixes until the checksum fits.
_IBAN = re.compile(r"\b[A-Z]{2}\d{2}(?: ?[A-Z0-9]{4}){2,7}(?: ?[A-Z0-9]{1,4})?\b")


def _iban_ok(value: str) -> bool:
    """ISO 13616 check: move the first 4 chars to the end, letters to
    numbers (A=10), and the result mod 97 must be 1."""
    v = value.replace(" ", "")
    if not 15 <= len(v) <= 34:
        return False
    return int("".join(str(int(c, 36)) for c in v[4:] + v[:4])) % 97 == 1


def _redact_ibans(text: str, replacement: str) -> tuple:
    """Replace IBANs that pass the checksum. Returns (text, count)."""
    count = 0

    def _sub(m):
        nonlocal count
        value = m.group()
        parts = value.split(" ")
        for k in range(len(parts), 0, -1):
            candidate = " ".join(parts[:k])
            if _iban_ok(candidate):
                count += 1
                return replacement + value[len(candidate):]
        return value

    return _IBAN.sub(_sub, text), count


def dangling_secret_label(text: Optional[str]) -> Optional[str]:
    """The trailing label if text ends with one and no value ("pwd -"), else None."""
    if not text:
        return None
    m = _DANGLING_LABEL.search(text)
    return m.group() if m else None


# Applied in this order. IBAN goes before the card check, because a run of
# IBAN digits can look like a card number.
PATTERNS = {
    "iban": {
        "label": "IBAN",
        "regex": _IBAN,
        "replacement": "[REDACTED:iban]",
        "redactor": _redact_ibans,
    },
    "credit_card": {
        "label": "Credit Card",
        "regex": re.compile(
            r"\b(?:\d{4}[\s\-]?){3}\d{4}\b"
        ),
        "replacement": "[REDACTED:card]",
        "validator": _luhn_check,
    },
    "ssn": {
        "label": "SSN",
        "regex": re.compile(
            r"\b\d{3}-\d{2}-\d{4}\b"
        ),
        "replacement": "[REDACTED:ssn]",
    },
    "api_key": {
        "label": "API Key",
        "regex": re.compile(
            r"\b(?:"
            r"sk-ant-[A-Za-z0-9\-]{20,}"    # Anthropic (before sk- to prevent false match)
            r"|sk-[A-Za-z0-9]{20,}"          # OpenAI
            r"|sk_live_[A-Za-z0-9]{20,}"     # Stripe live
            r"|sk_test_[A-Za-z0-9]{20,}"     # Stripe test
            r"|pk_live_[A-Za-z0-9]{20,}"     # Stripe publishable
            r"|ghp_[A-Za-z0-9]{36,}"         # GitHub PAT
            r"|github_pat_[A-Za-z0-9_]{20,}" # GitHub fine-grained PAT
            r"|gho_[A-Za-z0-9]{36,}"         # GitHub OAuth
            r"|AKIA[A-Z0-9]{16}"             # AWS Access Key
            r"|xox[bps]-[A-Za-z0-9\-]{10,}"  # Slack
            r"|glpat-[A-Za-z0-9\-]{20,}"     # GitLab
            r"|AIza[A-Za-z0-9\-_]{35}"       # Google API
            r"|whsec_[A-Za-z0-9]{20,}"       # Webhook secrets
            r"|ntn_[A-Za-z0-9]{20,}"         # Notion
            r")\b",
            re.ASCII,
        ),
        "replacement": "[REDACTED:key]",
    },
    "jwt": {
        "label": "JWT Token",
        "regex": re.compile(
            r"\beyJ[A-Za-z0-9_-]{20,}\.eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"
        ),
        "replacement": "[REDACTED:jwt]",
    },
    "password": {
        "label": "Password",
        "regex": _PASSWORD_STRICT,
        "replacement": "[REDACTED:password]",
        # Also catches "pwd - value" and values on the next line.
        "redactor": _redact_passwords,
    },
    "email": {
        "label": "Email Address",
        "regex": _EMAIL,
        "replacement": "[REDACTED:email]",
        # Keeps the domain: "[REDACTED:email@example.com]".
        "redactor": _redact_emails,
    },
    "phone": {
        "label": "Phone Number",
        "regex": _PHONE,
        "replacement": "[REDACTED:phone]",
        "validator": _phone_digits_ok,
    },
}

DEFAULT_TYPES = ["credit_card", "ssn", "api_key", "jwt", "password", "email", "phone", "iban"]


def filter_sensitive_text(
    text: str,
    enabled_types: Optional[list] = None,
) -> dict:
    """
    Scan text and redact sensitive data.

    Args:
        text: Raw OCR / organized text
        enabled_types: List of pattern keys to apply, in any order. They run
            in PATTERNS order. Defaults to DEFAULT_TYPES.

    Returns:
        {
            "clean_text": "...",
            "redacted_count": 3,
            "types_found": ["credit_card", "api_key"],
            "details": [
                {"type": "credit_card", "count": 2},
                {"type": "api_key", "count": 1},
            ]
        }
    """
    if not text:
        return {
            "clean_text": text or "",
            "redacted_count": 0,
            "types_found": [],
            "details": [],
        }

    if enabled_types is None:
        enabled_types = DEFAULT_TYPES
    enabled = set(enabled_types)

    clean = text
    total_redacted = 0
    types_found = []
    details = []

    for ptype, pattern_info in PATTERNS.items():
        if ptype not in enabled:
            continue

        regex = pattern_info["regex"]
        replacement = pattern_info["replacement"]

        redactor = pattern_info.get("redactor")
        if redactor:
            clean, count = redactor(clean, replacement)
            if count:
                total_redacted += count
                types_found.append(ptype)
                details.append({"type": ptype, "count": count})
            continue

        # Count matches before replacing
        matches = regex.findall(clean)
        count = len(matches)

        if count > 0:
            # If pattern has a validator (e.g. Luhn for credit cards),
            # only redact matches that pass validation
            validator = pattern_info.get("validator")
            if validator:
                def _validated_sub(m):
                    return replacement if validator(m.group()) else m.group()
                before_count = clean.count(replacement)
                clean = regex.sub(_validated_sub, clean)
                count = clean.count(replacement) - before_count
            else:
                clean = regex.sub(replacement, clean)

            total_redacted += count
            types_found.append(ptype)
            details.append({"type": ptype, "count": count})

    if total_redacted > 0:
        logger.info(f"Redacted {total_redacted} sensitive item(s): {', '.join(types_found)}")

    return {
        "clean_text": clean,
        "redacted_count": total_redacted,
        "types_found": types_found,
        "details": details,
    }


def filter_after_label(label: Optional[str], text: str, enabled_types: Optional[list] = None) -> str:
    """Filter text that may be the value for a label in the previous piece
    ("pwd -" in one OCR box or typed chunk, the value in the next one)."""
    if label:
        prefix = label + "\n"
        joined = filter_sensitive_text(prefix + text, enabled_types)["clean_text"]
        if joined.startswith(prefix):
            text = joined[len(prefix):]
    return filter_sensitive_text(text, enabled_types)["clean_text"]


def filter_ocr_boxes(boxes: Optional[list], enabled_types: Optional[list] = None) -> Optional[list]:
    """Redact the "text" of OCR boxes (dicts), in place and in list order.

    OCR often puts a label and its value in neighbouring boxes ("pwd" and
    "- value"), so a label left open by one box is checked with the next.
    Returns the same list.
    """
    label = None
    for box in boxes or []:
        text = box.get("text")
        if not text:
            continue
        box["text"] = filter_after_label(label, text, enabled_types)
        label = dangling_secret_label(box["text"])
    return boxes


def parse_enabled_types(types_str: str) -> list:
    """Parse comma-separated filter types string into a list."""
    if not types_str:
        return list(DEFAULT_TYPES)
    return [t.strip() for t in types_str.split(",") if t.strip() in PATTERNS]
