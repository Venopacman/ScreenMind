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
    assert parse_enabled_types("") == ["credit_card", "ssn", "api_key", "jwt", "password"]
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


def test_email_not_redacted_by_default():
    """Email is defined but not in the default enabled list."""
    text = "contact me at user@example.com"
    result = filter_sensitive_text(text)  # uses defaults
    assert "user@example.com" in result["clean_text"]


def test_email_redacted_when_enabled():
    text = "contact me at user@example.com"
    result = filter_sensitive_text(text, ["email"])
    assert "[REDACTED:email]" in result["clean_text"]



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
