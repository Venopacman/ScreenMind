"""Tests for a11y text quality: content detection, walk limits, browser URL, Electron titles."""

from unittest.mock import patch

import pytest

from screenmind.platform_support.macos import MacOSAdapter
from screenmind.workers.analysis_worker import _a11y_is_content, _is_browser, _pick_active_url


# ── Content detection ───────────────────────────────────────────────


class TestA11yIsContent:
    def test_real_content(self):
        text = "\n".join(f"Line {i}: some real text in a document about the roadmap" for i in range(10))
        assert _a11y_is_content(text, "Doc", "Pages")

    def test_window_title_only(self):
        title = "Google Workspace API Integration - Truto - Google Chrome - Pavel (Work)" * 1
        assert not _a11y_is_content(title + "\n" + "Google Chrome", title, "Google Chrome")

    def test_repeated_menu_bar(self):
        menu = "Chrome\nApple\nFile\nEdit\nView\nHistory\nBookmarks\nProfiles\nTab\nWindow\nHelp"
        assert not _a11y_is_content("\n".join([menu] * 80), "Some page", "Google Chrome")

    def test_menu_bar_on_one_line(self):
        line = "Chrome | Apple | File | Edit | View | History | Bookmarks | Profiles | Tab | Window | Help"
        assert not _a11y_is_content("\n".join([line] * 3), "x", "Google Chrome")

    def test_mostly_repeats(self):
        lines = ["same line of text that is long enough to count"] * 30 + ["other"] * 2
        assert not _a11y_is_content("\n".join(lines))

    def test_window_chrome_markers(self):
        text = "Minimize\nMaximize\nClose\n" + "x" * 300
        assert not _a11y_is_content(text)

    def test_empty(self):
        assert not _a11y_is_content(None)
        assert not _a11y_is_content("")


# ── Active URL ──────────────────────────────────────────────────────


class TestActiveUrl:
    def test_browser_url_wins(self):
        assert _pick_active_url("Google Chrome", "https://a.com/x", ["https://b.com"]) == "https://a.com/x"

    def test_non_browser_gets_none(self):
        assert _pick_active_url("Terminal", None, ["https://github.com/pytorch/pytorch/issues/1"]) is None
        assert _pick_active_url("Telegram", None, ["https://gith"]) is None

    def test_browser_without_ax_url_falls_back_to_text(self):
        assert _pick_active_url("Firefox", None, ["https://b.com"]) == "https://b.com"

    @pytest.mark.parametrize("name", ["Google Chrome", "Safari", "Arc", "Microsoft Edge", "chrome.exe", "Brave Browser"])
    def test_is_browser(self, name):
        assert _is_browser(name)

    @pytest.mark.parametrize("name", ["Terminal", "Telegram", "Slack", "Claude", "Arcade Game", None])
    def test_is_not_browser(self, name):
        assert not _is_browser(name)


# ── macOS adapter (AX faked) ────────────────────────────────────────


class FakeEl:
    def __init__(self, role, title=None, value=None, children=(), **attrs):
        self.attrs = {"AXRole": role, "AXTitle": title, "AXValue": value,
                      "AXChildren": list(children), **attrs}


def fake_attr(_self, el, attr):
    return el.attrs.get(attr)


@pytest.fixture
def mac():
    a = MacOSAdapter.__new__(MacOSAdapter)
    a._ax_available = True
    a._appkit_available = True
    a._manual_a11y_pids = set()
    with patch.object(MacOSAdapter, "_ax_attr", fake_attr):
        yield a


class TestWalk:
    def test_skips_menu_bar_and_dedups(self, mac):
        menu = FakeEl("AXMenuBar", children=[FakeEl("AXMenuBarItem", title="File")])
        win = FakeEl("AXWindow", title="Win", children=[
            menu,
            FakeEl("AXStaticText", value="hello world"),
            FakeEl("AXStaticText", value="hello world"),
            FakeEl("AXGroup", children=[menu]),
        ])
        texts = []
        mac._walk_ax_tree(win, texts, 0)
        assert texts == ["Win", "hello world"]

    def test_large_text_area_reads_visible_part(self, mac):
        area = FakeEl("AXTextArea", value="old scrollback\n" * 20000)
        with patch.object(MacOSAdapter, "_ax_visible_text", return_value="what is on screen"):
            texts = []
            mac._walk_ax_tree(FakeEl("AXWindow", children=[area]), texts, 0)
        assert texts == ["what is on screen"]

    def test_large_text_area_without_visible_range_keeps_tail(self, mac):
        area = FakeEl("AXTextArea", value="a" * 50000 + "END")
        with patch.object(MacOSAdapter, "_ax_visible_text", return_value=None):
            texts = []
            mac._walk_ax_tree(FakeEl("AXWindow", children=[area]), texts, 0)
        assert texts[0].endswith("END") and len(texts[0]) <= 4000

    def test_total_size_capped(self, mac):
        kids = [FakeEl("AXStaticText", value=f"{i} " + "x" * 3000) for i in range(30)]
        texts = []
        mac._walk_ax_tree(FakeEl("AXWindow", children=kids), texts, 0)
        assert sum(len(t) for t in texts) <= 20000


class TestTitlesAndUrls:
    def test_electron_title_from_web_area(self, mac):
        front = {"owner": "Claude", "pid": 7, "title": "Claude", "bounds": (0, 0, 1, 1)}
        with patch.object(MacOSAdapter, "_front_window", return_value=front), \
             patch.object(MacOSAdapter, "_ax_document_title", return_value="My chat - Claude Code"):
            assert mac.get_active_window_title() == "My chat - Claude Code"

    def test_real_title_kept(self, mac):
        front = {"owner": "Google Chrome", "pid": 7, "title": "Docs - Chrome", "bounds": (0, 0, 1, 1)}
        with patch.object(MacOSAdapter, "_front_window", return_value=front), \
             patch.object(MacOSAdapter, "_ax_document_title") as doc:
            assert mac.get_active_window_title() == "Docs - Chrome"
            doc.assert_not_called()

    def test_document_title_bfs(self, mac):
        inner = FakeEl("AXWebArea", title="Chat name - Claude Code")
        outer = FakeEl("AXWebArea", title="", children=[inner])
        win = FakeEl("AXWindow", children=[FakeEl("AXGroup", children=[outer])])
        with patch.object(MacOSAdapter, "_ax_focused_window", return_value=win), \
             patch.object(MacOSAdapter, "enable_full_a11y_tree"):
            assert mac._ax_document_title(7) == "Chat name - Claude Code"

    def test_browser_url(self, mac):
        win = FakeEl("AXWindow", children=[FakeEl("AXGroup", children=[
            FakeEl("AXWebArea", AXURL="https://truto.one/page")])])
        front = {"owner": "Google Chrome", "pid": 7, "title": "x", "bounds": (0, 0, 1, 1)}
        with patch.object(MacOSAdapter, "_front_window", return_value=front), \
             patch.object(MacOSAdapter, "_ax_focused_window", return_value=win):
            assert mac.get_browser_url() == "https://truto.one/page"

    def test_browser_url_none_for_other_apps(self, mac):
        front = {"owner": "Terminal", "pid": 7, "title": "x", "bounds": (0, 0, 1, 1)}
        with patch.object(MacOSAdapter, "_front_window", return_value=front):
            assert mac.get_browser_url() is None
