"""Tests for the stored-URL sanitizer (active_url, ui_events.url)."""

import pytest

from screenmind.privacy.url_filter import sanitize_url

FAKE_JWT = "eyJhbGciOiJIUzI1NiJ9.eyJlbWFpbCI6ImFAYi5jIn0.c2lnbmF0dXJlLXRlc3Q"


@pytest.mark.parametrize("url, expected", [
    # Normal pages keep scheme + host + path
    ("https://gitlab.com/group/proj/-/merge_requests/1175/diffs", "https://gitlab.com/group/proj/-/merge_requests/1175/diffs"),
    ("https://github.com/screenpipe/screenpipe", "https://github.com/screenpipe/screenpipe"),
    # Query and fragment dropped, safe params kept
    ("https://app.example.com/board?tab=done&session=abc123#row-4", "https://app.example.com/board?tab=done"),
    ("https://www.google.com/search?q=private+thing", "https://www.google.com/search"),
    # user:password removed
    ("https://user:secret@example.com/x", "https://example.com/x"),
    # Sign-in hosts: host only
    ("https://accounts.google.com/o/oauth2/v2/auth?client_id=1&state=s&nonce=n", "https://accounts.google.com/"),
    (f"https://auth.eu-services.academy.nebius.com/realms/x/login-actions/action-token?key={FAKE_JWT}",
     "https://auth.eu-services.academy.nebius.com/"),
    ("https://acme.okta.com/app/123/sso/saml", "https://acme.okta.com/"),
    # Auth paths on normal hosts: host only
    ("https://app.example.com/oauth/callback?code=xyz", "https://app.example.com/"),
    ("https://example.com/reset-password/abc", "https://example.com/"),
    # Token-like path segments
    (f"https://example.com/share/{FAKE_JWT}", "https://example.com/share/<token>"),
    ("https://example.com/dl/" + "a" * 70, "https://example.com/dl/<token>"),
    # A Google Docs id (44 chars) is a page, not a token
    ("https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefgh/edit",
     "https://docs.google.com/document/d/1AbCdEfGhIjKlMnOpQrStUvWxYz0123456789abcdefgh/edit"),
    # Local dev server keeps its port
    ("http://127.0.0.1:7777/#timeline", "http://127.0.0.1:7777/"),
    # Not web URLs
    ("chrome://settings", None),
    ("javascript:alert(1)", None),
    ("", None),
    (None, None),
])
def test_sanitize_url(url, expected):
    assert sanitize_url(url) == expected


def test_file_url_dropped():
    assert sanitize_url("file:///Users/me/Downloads/report.pdf?x=1#p2") is None
    assert sanitize_url("FILE:///Users/me/report.pdf") is None
