"""Tests for privacy/data_filter.py — sensitive data redaction."""

from screenmind.privacy.data_filter import filter_sensitive_text, parse_enabled_types


def test_redact_credit_card():
    text = "My card is 4111-1111-1111-1111 thanks"
    result = filter_sensitive_text(text, ["credit_card"])
    assert "[REDACTED:card]" in result["clean_text"]
    assert "4111" not in result["clean_text"]
    assert result["redacted_count"] == 1
    assert "credit_card" in result["types_found"]


def test_credit_card_luhn_rejects_random_16_digits():
    """16-digit numbers that fail Luhn checksum are NOT redacted."""
    text = "tracking number: 1234-5678-9012-3456"
    result = filter_sensitive_text(text, ["credit_card"])
    assert result["redacted_count"] == 0
    assert "1234-5678-9012-3456" in result["clean_text"]


def test_credit_card_luhn_accepts_valid_mastercard():
    """Valid Mastercard test number passes Luhn."""
    text = "pay with 5500-0000-0000-0004"
    result = filter_sensitive_text(text, ["credit_card"])
    assert "[REDACTED:card]" in result["clean_text"]


def test_redact_ssn():
    text = "SSN: 123-45-6789"
    result = filter_sensitive_text(text, ["ssn"])
    assert "[REDACTED:ssn]" in result["clean_text"]
    assert "123-45-6789" not in result["clean_text"]


def test_redact_api_key_openai():
    text = "key: sk-abcdefghijklmnopqrstuvwxyz1234567890"
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_api_key_github():
    text = "token ghp_ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghij"
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_password():
    text = "password: mysecretpass123"
    result = filter_sensitive_text(text, ["password"])
    assert "[REDACTED:password]" in result["clean_text"]
    assert "mysecretpass123" not in result["clean_text"]


def test_no_redaction_clean_text():
    text = "Just a normal sentence about coding in Python."
    result = filter_sensitive_text(text, ["credit_card", "ssn", "api_key", "password"])
    assert result["clean_text"] == text
    assert result["redacted_count"] == 0
    assert result["types_found"] == []


def test_empty_text():
    result = filter_sensitive_text("", ["credit_card"])
    assert result["clean_text"] == ""
    assert result["redacted_count"] == 0


def test_none_text():
    result = filter_sensitive_text(None, ["credit_card"])
    assert result["clean_text"] == ""


def test_multiple_redactions():
    text = "Card: 4111 1111 1111 1111, SSN: 999-88-7777, key: sk-AAAABBBBCCCCDDDDEEEEFFFFGGGG"
    result = filter_sensitive_text(text, ["credit_card", "ssn", "api_key"])
    assert result["redacted_count"] == 3
    assert len(result["types_found"]) == 3


def test_parse_enabled_types():
    assert parse_enabled_types("credit_card,ssn") == ["credit_card", "ssn"]
    assert parse_enabled_types("") == ["credit_card", "ssn", "api_key", "jwt", "password", "email", "phone", "iban"]
    assert parse_enabled_types("invalid,credit_card") == ["credit_card"]


# ── New pattern coverage ──────────────────────────────────────────────


def test_redact_anthropic_key():
    text = "key: sk-ant-api03-AAAABBBBCCCCDDDDEEEEFFFFGGGG-1234567890"
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]
    assert "sk-ant-" not in result["clean_text"]


def test_redact_stripe_live_key():
    text = "STRIPE_KEY=" + "sk_live_" + "A" * 24  # build key dynamically to avoid GitHub push protection
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]
    assert "sk_live_" not in result["clean_text"]


def test_redact_stripe_test_key():
    text = "key: " + "sk_test_" + "A" * 24  # dynamic to avoid GitHub push protection
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_stripe_publishable_key():
    text = "pk_live_" + "A" * 24 + " in the frontend"
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_github_fine_grained_pat():
    text = "token: github_pat_AAAA_BBBBCCCCDDDDEEEEFFFFGGGG"
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]
    assert "github_pat_" not in result["clean_text"]


def test_redact_github_oauth_token():
    text = "gho_" + "ABCDEFGHIJKLMNOPQRSTUVWXYZ" + "abcdefghij1234"
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_notion_key():
    text = "NOTION_KEY=" + "ntn_" + "A" * 24
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_webhook_secret():
    text = "webhook: " + "whsec_" + "A" * 24  # build dynamically to avoid GitHub push protection
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_jwt_token():
    text = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N_XgL0n3I9PlFUP0THsR8U"
    result = filter_sensitive_text(text, ["jwt"])
    assert "[REDACTED:jwt]" in result["clean_text"]
    assert "eyJ" not in result["clean_text"]


def test_jwt_not_triggered_by_short_strings():
    """Short 'eyJ' strings that aren't JWTs should not match."""
    text = "the eyJ fragment alone is not a JWT"
    result = filter_sensitive_text(text, ["jwt"])
    assert result["redacted_count"] == 0


def test_redact_aws_access_key():
    text = "AWS_KEY=" + "AKIA" + "IOSFODNN7EXAMPLE"
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_slack_token():
    text = "SLACK=xoxb-123456789012-abcdefghij"
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_redact_gitlab_token():
    text = "token: " + "glpat-" + "A" * 24
    result = filter_sensitive_text(text, ["api_key"])
    assert "[REDACTED:key]" in result["clean_text"]


def test_email_redacted_by_default_keeps_domain():
    text = "contact me at Jane.Doe+work@Mail.Example.com"
    result = filter_sensitive_text(text)  # uses defaults
    assert result["clean_text"] == "contact me at [REDACTED:email@mail.example.com]"
    assert result["types_found"] == ["email"]


def test_email_kept_when_type_off():
    text = "contact me at user@example.com"
    assert filter_sensitive_text(text, ["password"])["clean_text"] == text



# ── Password forms beyond "key: value" ───────────────────────────────
# All values here are made up.

import pytest  # noqa: E402

from screenmind.privacy.data_filter import dangling_secret_label  # noqa: E402

PW = "[REDACTED:password]"


@pytest.mark.parametrize("text, expected", [
    ("pwd - abc123def456", f"pwd - {PW}"),
    ("root pwd  - abc123def456\r\nnext line", f"root pwd  - {PW}\r\nnext line"),
    ("password \u2013 Tr0ub4dor&3", f"password \u2013 {PW}"),
    ("pwd-abc123def456", f"pwd-{PW}"),
    ("password  Tr0ub4dor&3", f"password  {PW}"),
    ("token xyz789abc000", f"token {PW}"),
    # OCR splits the label and the value onto two lines
    ("pwd\n- abc123def456\nnotes", f"pwd\n- {PW}\nnotes"),
    ("Password\r\nhunter_2x", f"Password\r\n{PW}"),
    ("password:\n  s3cr3tValue", f"password:\n  {PW}"),
    # the label stays, only the value goes
    ("password: mysecretpass123", f"password: {PW}"),
    ("DB_PASSWORD=hunter22", f"DB_PASSWORD={PW}"),
])
def test_password_value_forms(text, expected):
    result = filter_sensitive_text(text, ["password"])
    assert result["clean_text"] == expected
    assert result["redacted_count"] == 1
    assert result["types_found"] == ["password"]


@pytest.mark.parametrize("text", [
    "password reset",
    "Forgot your password?",
    "Change password - Google Account",
    "Reset password - LinkedIn",
    "Password Manager",
    "Enter your password\nRemember me",
    "Password\nForgot password?",
    "Password\nPowerShell sessions",
    "password-protected file",
    "Your password must be at least 8 characters",
    "password (optional)",
    "password 2026-10-07",
    "pwd - https://example.com/a1",
    "passwords 123abc456",
    "Token limit 4096",
    "secret Santa",
    "Notepad++ with a fake pwd\nI - think about it.",
])
def test_password_prose_not_redacted(text):
    result = filter_sensitive_text(text, ["password"])
    assert result["clean_text"] == text
    assert result["redacted_count"] == 0


def test_password_filter_is_idempotent():
    once = filter_sensitive_text("pwd - abc123def456, password: s3cr3tValue", ["password"])
    assert once["redacted_count"] == 2
    twice = filter_sensitive_text(once["clean_text"], ["password"])
    assert twice["clean_text"] == once["clean_text"]
    assert twice["redacted_count"] == 0


def test_api_key_after_token_label_counted_once():
    text = "token ghp_" + "A" * 36
    result = filter_sensitive_text(text, ["api_key", "password"])
    assert result["clean_text"] == "token [REDACTED:key]"
    assert result["types_found"] == ["api_key"]


@pytest.mark.parametrize("text, label", [
    ("pwd -", "pwd -"),
    ("my Password:", "Password:"),
    ("DB_PASSWORD=", "PASSWORD="),
    ("server pwd", "pwd"),
    ("pwd - abc123def456", None),
    ("passwords", None),
    ("", None),
])
def test_dangling_secret_label(text, label):
    assert dangling_secret_label(text) == label


def test_filter_ocr_boxes_joins_label_and_value_boxes():
    from screenmind.privacy.data_filter import filter_ocr_boxes
    boxes = [
        {"text": "pwd", "conf": 0.9},
        {"text": "- abc123def456", "conf": 0.9},
        {"text": "T 5", "conf": 0.9},
        {"text": "card 4111 1111 1111 1111", "conf": 0.9},
        {"text": "Password", "conf": 0.9},
        {"text": "Forgot password?", "conf": 0.9},
    ]
    out = filter_ocr_boxes(boxes, ["credit_card", "password"])
    assert out is boxes
    assert [b["text"] for b in boxes] == [
        "pwd", f"- {PW}", "T 5", "card [REDACTED:card]", "Password", "Forgot password?",
    ]


def test_filter_ocr_boxes_handles_empty():
    from screenmind.privacy.data_filter import filter_ocr_boxes
    assert filter_ocr_boxes(None) is None
    assert filter_ocr_boxes([{"text": ""}, {"box": []}]) == [{"text": ""}, {"box": []}]


# ── Emails, phones, IBANs ────────────────────────────────────────────
# All values here are made up (IBANs are the standard documentation examples).

def test_email_filter_is_idempotent():
    once = filter_sensitive_text("mail a.b@example.org now")["clean_text"]
    assert once == "mail [REDACTED:email@example.org] now"
    assert filter_sensitive_text(once)["clean_text"] == once


def test_mention_read_as_email_hides_the_name():
    """OCR of a Slack mention ("Hi @jane.doe") can look like an email whose
    domain is a person's name. That domain is not kept."""
    assert filter_sensitive_text("Hi@jane.doekova- thanks")["clean_text"] == (
        "[REDACTED:email]- thanks")


def test_country_code_domain_is_kept():
    assert filter_sensitive_text("ivan@example.ru")["clean_text"] == "[REDACTED:email@example.ru]"


def test_retina_file_name_is_not_an_email():
    text = "logo@2x.png icon@3x.webp"
    assert filter_sensitive_text(text)["clean_text"] == text


@pytest.mark.parametrize("text", [
    "call +1 (415) 555-0132 today",
    "call +7 999 123-45-67 today",
    "call +79991234567 today",
    "call +49 30 1234567 today",
    "call +44 20 7946 0958 today",
    "call (415) 555-0132 today",
    "call 415-555-0132 today",
    "call 415.555.0132 today",
    "call 8 (999) 123-45-67 today",
    "call 8-999-123-45-67 today",
])
def test_phone_redacted(text):
    result = filter_sensitive_text(text)
    assert result["clean_text"] == "call [REDACTED:phone] today"
    assert result["types_found"] == ["phone"]


def test_phone_at_end_of_sentence():
    assert filter_sensitive_text("My number is +7 999 123-45-67.")["clean_text"] == (
        "My number is [REDACTED:phone].")


@pytest.mark.parametrize("text", [
    "2026-10-08 12:44:36",
    "build 1.2.3.4567 on 10.0.0.12",
    "order 89991234567 shipped",          # bare digit run: an ID, not a phone
    "total 1 234 567,89 EUR",
    "UTC+03:00",
    "+30% since 2025-10-08",
    "v415-555-0132x",
    "https://example.com/415-555-0132",
    "id 12345678-1234-1234-1234-123456789abc",
    "price +12.50",
])
def test_phone_not_redacted(text):
    assert filter_sensitive_text(text)["clean_text"] == text


@pytest.mark.parametrize("iban", [
    "DE89 3704 0044 0532 0130 00",
    "DE89370400440532013000",
    "GB82 WEST 1234 5698 7654 32",
    "NL91 ABNA 0417 1643 00",
    "FR14 2004 1010 0505 0001 3M02 606",
])
def test_iban_redacted(iban):
    result = filter_sensitive_text(f"pay to {iban} please")
    assert result["clean_text"] == "pay to [REDACTED:iban] please"
    assert result["types_found"] == ["iban"]


def test_iban_followed_by_a_word():
    assert filter_sensitive_text("DE89 3704 0044 0532 0130 00 TEST")["clean_text"] == (
        "[REDACTED:iban] TEST")


@pytest.mark.parametrize("text", [
    "DE89 3704 0044 0532 0130 01",   # wrong checksum
    "AB12 CDEF GHIJ",                # too short
    "RFC2616 SECTION 1234 ABCD",
])
def test_iban_not_redacted(text):
    assert filter_sensitive_text(text)["clean_text"] == text


def test_iban_digits_not_taken_as_card():
    """IBAN runs before the card check, so its digits are not half redacted."""
    text = "DE89 3704 0044 0532 0130 00"
    assert filter_sensitive_text(text, ["credit_card", "iban"])["clean_text"] == "[REDACTED:iban]"


def test_types_run_in_fixed_order():
    text = "user@example.com DE89 3704 0044 0532 0130 00"
    a = filter_sensitive_text(text, ["email", "iban", "credit_card"])
    b = filter_sensitive_text(text, ["credit_card", "iban", "email"])
    assert a == b


def test_config_default_matches_filter_default():
    from screenmind.config import Settings
    from screenmind.privacy.data_filter import DEFAULT_TYPES
    assert Settings.model_fields["sensitive_filter_types"].default == ",".join(DEFAULT_TYPES)
