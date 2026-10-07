"""Tests for a11y text quality: content detection, walk limits, browser URL, Electron titles."""

import sys
from unittest.mock import patch

import pytest

from screenmind.platform_support.macos import MacOSAdapter
from screenmind.workers import analysis_worker as aw
from screenmind.workers.analysis_worker import _a11y_is_content, _screen_text_len


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

    def test_repeated_title_lines_in_any_language(self):
        title = "Яндекс — быстрый поиск в интернете — Mozilla Firefox"
        for filtered in (True, False):
            assert not _a11y_is_content("\n".join([title, "Mozilla Firefox"] * 3), title,
                                        "Mozilla Firefox", type_filtered=filtered)

    def test_word_checks_only_where_the_adapter_did_not_filter_by_type(self):
        assert aw._A11Y_TYPE_FILTERED == (sys.platform == "win32")


# Mentions chrome words, but it is page text.
_HELP_EN = ("Close the extensions menu, then minimize or maximize the window.\n"
            "Memory usage of each tab is shown in the address and search bar.\n"
            "Restore a sleeping tab by clicking it; no access needed for this site.\n"
            "Bookmarks and history stay where they were.")
_HELP_RU = ("Закройте меню расширений, затем сверните или разверните окно.\n"
            "Использование памяти каждой вкладкой видно в адресной строке.\n"
            "Чтобы восстановить спящую вкладку, нажмите на неё; доступ сайту не нужен.\n"
            "Закладки и история останутся на месте.")
_MENU_LINES = "\n".join(["File | Edit | View | History | Bookmarks | Window | Help",
                         "Chrome | Apple | File | Edit | View | Profiles | Tab | Tools",
                         "Go | Format | Insert | Shell | Window | Help | View | Edit",
                         "Apple | Chrome | File | Bookmarks | History | Go | Tab"])


class TestA11yIsContentTypeFiltered:
    """Windows: the adapter left chrome out by element type, so no word checks."""

    def test_same_verdict_in_any_language(self):
        assert _a11y_is_content(_HELP_EN, "Help", "chrome", type_filtered=True)
        assert _a11y_is_content(_HELP_RU, "Справка", "chrome", type_filtered=True)
        assert not _a11y_is_content(_HELP_RU[:150], "Справка", "chrome", type_filtered=True)

    def test_menu_words_are_not_dropped(self):
        assert _a11y_is_content(_MENU_LINES + "\n" + "x" * 10, type_filtered=True)


class TestA11yIsContentWordChecks:
    """macOS and Linux: the walkers keep buttons and tabs, so English chrome
    markers and menu-word lines still count as chrome."""

    def test_window_chrome_markers(self):
        text = "Minimize\nMaximize\nClose\n" + "x" * 300
        assert not _a11y_is_content(text, type_filtered=False)
        assert _a11y_is_content(text, type_filtered=True)

    def test_menu_word_lines_are_not_content(self):
        assert not _a11y_is_content(_MENU_LINES + "\n" + "x" * 10, type_filtered=False)

    def test_real_content_still_passes(self):
        text = "\n".join(f"Line {i}: some real text in a document about the roadmap" for i in range(10))
        assert _a11y_is_content(text, "Doc", "Pages", type_filtered=False)

    def test_empty(self):
        assert not _a11y_is_content(None)
        assert not _a11y_is_content("")


class TestScreenTextLen:
    def test_menu_bar_only_is_empty(self):
        assert _screen_text_len("Finder\nFile\nView\nGo\nWindow\nHelp") < 20

    def test_menu_bar_with_clock_is_empty(self):
        assert _screen_text_len("Finder | File | Edit | View | Go | Window | Help\nOct 7., Wed 13:30") < 20

    def test_real_text_counts(self):
        assert _screen_text_len("Finder\nFile\nQuarterly report draft v2.docx") >= 20


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
        mac._walk_ax_tree(FakeEl("AXWindow", children=kids), texts, 0, budget=[20000, 4000])
        assert sum(len(t) for t in texts) <= 20000

    def test_long_page_not_cut_at_20k(self, mac):
        """Screen text is kept in full; only the Gemma prompt is trimmed."""
        kids = [FakeEl("AXStaticText", value=f"{i} " + "x" * 3000) for i in range(30)]
        texts = []
        mac._walk_ax_tree(FakeEl("AXWindow", children=kids), texts, 0)
        assert len(texts) == 30

    def test_skips_password_fields(self, mac):
        win = FakeEl("AXWindow", children=[
            FakeEl("AXTextField", value="••••••", AXSubrole="AXSecureTextField"),
            FakeEl("AXTextField", value="user name"),
        ])
        texts = []
        mac._walk_ax_tree(win, texts, 0)
        assert texts == ["user name"]

    def test_long_title_capped(self, mac):
        texts = []
        mac._walk_ax_tree(FakeEl("AXWindow", children=[FakeEl("AXGroup", title="t" * 9000)]), texts, 0)
        assert len(texts[0]) == 4000

    def test_web_area_text_deep_inside_is_read(self, mac):
        """Electron apps: the web area is ~7 levels down and its text ~30 more."""
        el = FakeEl("AXStaticText", value="deep message")
        for _ in range(30):
            el = FakeEl("AXGroup", children=[el])
        el = FakeEl("AXWebArea", children=[el])
        for _ in range(7):
            el = FakeEl("AXGroup", children=[el])
        texts = []
        mac._walk_ax_tree(FakeEl("AXWindow", children=[el]), texts, 0)
        assert "deep message" in texts

    def test_node_budget_stops_walk(self, mac):
        kids = [FakeEl("AXStaticText", value=f"line {i}") for i in range(50)]
        texts = []
        mac._walk_ax_tree(FakeEl("AXWindow", children=kids), texts, 0, budget=[20000, 11])
        assert len(texts) == 10

    def test_page_areas_skip_devtools_panels_and_iframes(self, mac):
        page = FakeEl("AXWebArea", AXURL="https://example.com/",
                      children=[FakeEl("AXWebArea", AXURL="https://ads.example/")])
        devtools = FakeEl("AXWebArea", AXURL="devtools://devtools/bundled/devtools_app.html")
        panel = FakeEl("AXWebArea", AXURL="chrome-extension://abc/panel.html")
        win = FakeEl("AXWindow", children=[FakeEl("AXGroup", children=[devtools, page, panel])])
        assert mac._ax_page_areas(win) == [page]

    def test_browser_reads_only_the_page(self, mac):
        """Not the tab strip or the address bar (raw URL, not sanitized)."""
        page = FakeEl("AXWebArea", title="Example", AXURL="https://example.com/",
                      children=[FakeEl("AXStaticText", value="the page text people read")])
        win = FakeEl("AXWindow", title="Example - Google Chrome", children=[
            FakeEl("AXTextField", value="example.com/secret?token=1"),
            FakeEl("AXRadioButton", title="Other tab title"),
            FakeEl("AXGroup", children=[page]),
        ])
        with patch("ApplicationServices.AXUIElementCreateApplication"), \
             patch("ApplicationServices.AXUIElementCopyAttributeValue", return_value=(0, win)), \
             patch.object(MacOSAdapter, "_is_browser", return_value=True), \
             patch.object(MacOSAdapter, "enable_full_a11y_tree"):
            text, source = mac.extract_a11y_text(7)
        assert text == "Example\nthe page text people read" and source == "a11y"

    def test_skips_sidebar_and_nav_landmarks(self, mac):
        win = FakeEl("AXWindow", children=[
            FakeEl("AXGroup", AXSubrole="AXLandmarkComplementary",
                   children=[FakeEl("AXLink", title="Other chat")]),
            FakeEl("AXGroup", AXSubrole="AXLandmarkNavigation",
                   children=[FakeEl("AXLink", title="Site menu")]),
            FakeEl("AXGroup", AXSubrole="AXLandmarkMain",
                   children=[FakeEl("AXStaticText", value="the conversation")]),
        ])
        texts = []
        mac._walk_ax_tree(win, texts, 0)
        assert texts == ["the conversation"]

    def test_drops_line_repeated_inside_the_next(self, mac):
        """A link title with a status prefix, then its own text child."""
        win = FakeEl("AXWindow", children=[
            FakeEl("AXLink", title="Idle Open items review",
                   children=[FakeEl("AXStaticText", value="Open items review")]),
            FakeEl("AXStaticText", value="Go"),
            FakeEl("AXStaticText", value="Google search"),
            FakeEl("AXStaticText", value="Draft"),
            FakeEl("AXStaticText", value="Draft saved at noon"),
        ])
        texts = []
        mac._walk_ax_tree(win, texts, 0)
        assert texts == ["Idle Open items review", "Go", "Google search", "Draft saved at noon"]


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

    def test_document_title_uses_window_with_matching_bounds(self, mac):
        """Two windows of one app on two displays: use the one at these bounds."""
        win_b = FakeEl("AXWindow", children=[FakeEl("AXWebArea", title="Chat B")])
        with patch.object(MacOSAdapter, "_ax_window_at", return_value=win_b) as at, \
             patch.object(MacOSAdapter, "_ax_focused_window") as focused, \
             patch.object(MacOSAdapter, "enable_full_a11y_tree"):
            assert mac._ax_document_title(7, (1512, -275, 2198, 1257)) == "Chat B"
            at.assert_called_once_with(7, (1512, -275, 2198, 1257))
            focused.assert_not_called()

    def test_browser_url(self, mac):
        win = FakeEl("AXWindow", children=[FakeEl("AXGroup", children=[
            FakeEl("AXWebArea", AXURL="https://truto.one/page")])])
        front = {"owner": "Google Chrome", "pid": 7, "title": "x", "bounds": (0, 0, 1, 1)}
        with patch.object(MacOSAdapter, "_front_window", return_value=front), \
             patch.object(MacOSAdapter, "_ax_focused_window", return_value=win):
            assert mac.get_browser_url() == "https://truto.one/page"

    def test_browser_url_ignores_docked_devtools(self, mac):
        """DevTools is a web area too, and comes first in the tree."""
        win = FakeEl("AXWindow", children=[FakeEl("AXGroup", children=[
            FakeEl("AXWebArea", AXURL="devtools://devtools/bundled/devtools_app.html"),
            FakeEl("AXWebArea", AXURL="https://example.com/")])])
        front = {"owner": "Google Chrome", "pid": 7, "title": "x", "bounds": (0, 0, 1, 1)}
        with patch.object(MacOSAdapter, "_front_window", return_value=front), \
             patch.object(MacOSAdapter, "_ax_focused_window", return_value=win):
            assert mac.get_browser_url() == "https://example.com/"

    def test_browser_url_none_when_two_pages(self, mac):
        """A side panel with its own https page: a wrong URL is worse than none."""
        win = FakeEl("AXWindow", children=[
            FakeEl("AXWebArea", AXURL="https://example.com/"),
            FakeEl("AXWebArea", AXURL="https://www.google.com/search?q=x")])
        front = {"owner": "Google Chrome", "pid": 7, "title": "x", "bounds": (0, 0, 1, 1)}
        with patch.object(MacOSAdapter, "_front_window", return_value=front), \
             patch.object(MacOSAdapter, "_ax_focused_window", return_value=win):
            assert mac.get_browser_url() is None

    def test_browser_url_none_for_other_apps(self, mac):
        front = {"owner": "Terminal", "pid": 7, "title": "x", "bounds": (0, 0, 1, 1)}
        with patch.object(MacOSAdapter, "_front_window", return_value=front):
            assert mac.get_browser_url() is None


def test_top_window_in_uses_electron_title(mac):
    """Per-display labels must get the same Electron title fallback."""
    win = {"owner": "Claude", "pid": 7, "title": "Claude", "bounds": (0, 0, 1, 1)}
    with patch.object(MacOSAdapter, "_front_window", return_value=win), \
         patch.object(MacOSAdapter, "_ax_document_title", return_value="My chat - Claude Code"):
        assert mac.get_top_window_in(0, 0, 100, 100) == ("Claude", "My chat - Claude Code")
