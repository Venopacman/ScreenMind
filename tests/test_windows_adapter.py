"""Windows adapter parity features that need no OS: page URL choice, top
window filtering and the a11y walk rules. Live checks are in
docs/plans/ui-events.md (Windows implementation notes)."""

import pytest

pw = pytest.importorskip("screenmind.platform_support.windows")


def doc(url, nested=False, onscreen=True, name="Page"):
    return {"name": name, "url": url, "nested": nested, "onscreen": onscreen}


# ── Browser URL ─────────────────────────────────────────────────────


class TestPickPageDocument:
    def test_chrome_single_page(self):
        assert pw.pick_page_document([doc("https://github.com/x")]) == ("Page", "https://github.com/x")

    def test_file_url(self):
        assert pw.pick_page_document([doc("file:///C:/a.html")])[1] == "file:///C:/a.html"

    def test_firefox_background_tabs_are_ignored(self):
        docs = [
            doc("https://active.example/", onscreen=True),
            doc("moz-extension://abc/options.html", onscreen=False),
            doc("https://sponsor.example/", nested=True, onscreen=False),
        ]
        assert pw.pick_page_document(docs)[1] == "https://active.example/"

    def test_new_tab_gives_none_not_a_background_url(self):
        docs = [
            doc("file:///C:/old.html", onscreen=False),
            doc("https://sponsor.example/", nested=True, onscreen=False),
            doc("about:newtab", onscreen=True),
        ]
        assert pw.pick_page_document(docs) is None

    def test_iframes_do_not_count(self):
        docs = [doc("chrome://newtab/"), doc("https://iframe.example/", nested=True)]
        assert pw.pick_page_document(docs) is None

    def test_docked_devtools_is_ignored(self):
        docs = [doc("https://app.example/login"), doc("devtools://devtools/bundled/devtools_app.html")]
        assert pw.pick_page_document(docs)[1] == "https://app.example/login"

    def test_extension_side_panel_is_ignored(self):
        docs = [doc("https://mail.example/"), doc("chrome-extension://abc/panel.html")]
        assert pw.pick_page_document(docs)[1] == "https://mail.example/"

    def test_side_panel_never_stands_in_for_a_browser_page(self):
        docs = [doc("chrome://settings/"), doc("https://panel.example/")]
        assert pw.pick_page_document(docs) is None

    def test_two_visible_pages_is_ambiguous(self):
        assert pw.pick_page_document([doc("https://a.example/"), doc("https://b.example/")]) is None

    def test_nothing(self):
        assert pw.pick_page_document([]) is None
        assert pw.pick_page_document([doc("")]) is None


# ── Top window per display ──────────────────────────────────────────


def app_window(**kw):
    args = dict(visible=True, minimized=False, cloaked=False, ex_style=0,
                class_name="Chrome_WidgetWin_1", title="Inbox - Mail", width=1200, height=800)
    args.update(kw)
    return pw.is_app_window(**args)


class TestTopWindow:
    def test_normal_window(self):
        assert app_window()

    @pytest.mark.parametrize("kw", [
        {"visible": False},
        {"minimized": True},
        {"cloaked": True},                       # other virtual desktop
        {"ex_style": 0x80},                      # tool window
        {"ex_style": 0x20},                      # click-through overlay
        {"ex_style": 0x08000000},                # no-activate overlay
        {"ex_style": 0x08},                      # always on top (picture-in-picture)
        {"class_name": "Shell_TrayWnd"},         # taskbar
        {"class_name": "Progman"},               # desktop
        {"title": ""},
        {"width": 30},
    ])
    def test_skipped(self, kw):
        assert not app_window(**kw)

    def test_tool_window_marked_as_app_counts(self):
        assert app_window(ex_style=0x80 | 0x40000)

    def test_center_rule(self):
        # Maximized windows overhang their display by a few pixels.
        assert pw.center_in((-8, -8, 2568, 1608), 0, 0, 2560, 1600)
        assert not pw.center_in((2560, 0, 4480, 1080), 0, 0, 2560, 1600)
        assert pw.center_in((2560, 0, 4480, 1080), 2560, 0, 1920, 1080)

    def test_adapter_says_it_can_find_top_windows(self):
        assert pw.WindowsAdapter().can_find_top_window is True


# ── A11y walk ───────────────────────────────────────────────────────


class FakePattern:
    def __init__(self, value="", read_only=False, visible=None):
        self.Value = value
        self.IsReadOnly = read_only
        self._visible = visible

    def GetVisibleRanges(self):
        return [FakeRange(t) for t in self._visible or []]


class FakeRange:
    def __init__(self, text):
        self.text = text

    def GetText(self, max_len):
        return self.text[:max_len]


class FakeControl:
    def __init__(self, type_name, name="", children=(), password=False, value=None, text=None):
        self.ControlTypeName = type_name
        self.Name = name
        self.IsPassword = password
        self._children = list(children)
        self._value = value
        self._text = text

    def GetChildren(self):
        return self._children

    def GetPattern(self, pid):
        if pid == "value":
            return self._value
        if pid == "text":
            return self._text
        return None


class FakeAuto:
    class PatternId:
        ValuePattern = "value"
        TextPattern = "text"


@pytest.fixture
def adapter():
    a = pw.WindowsAdapter()
    a._uia = FakeAuto
    return a


def walk(adapter, root):
    texts = []
    adapter._walk_tree(root, texts, depth=0)
    return texts


class TestA11yWalk:
    def test_menus_title_bar_and_scroll_bars_are_skipped(self, adapter):
        root = FakeControl("WindowControl", "Editor", [
            FakeControl("MenuBarControl", "Menu", [FakeControl("MenuItemControl", "File")]),
            FakeControl("TitleBarControl", "", [FakeControl("ButtonControl", "Minimize")]),
            FakeControl("ScrollBarControl", "Vertical"),
            FakeControl("TextControl", "Real content here"),
        ])
        assert walk(adapter, root) == ["Editor", "Real content here"]

    def test_repeated_lines_dropped(self, adapter):
        root = FakeControl("PaneControl", "", [FakeControl("TextControl", "Same")] * 5)
        assert walk(adapter, root) == ["Same"]

    def test_password_field_value_never_read(self, adapter):
        pwd = FakeControl("EditControl", "Password", password=True,
                          value=FakePattern("hunter2"), text=FakePattern(visible=["hunter2"]))
        assert walk(adapter, FakeControl("PaneControl", "", [pwd])) == ["Password"]

    def test_editable_text_area_gives_visible_part_only(self, adapter):
        big = FakeControl("DocumentControl", "Text editor",
                          value=FakePattern("x" * 1_000_000),
                          text=FakePattern(visible=["line 41", "line 42"]))
        assert walk(adapter, big) == ["Text editor", "line 41\nline 42"]

    def test_read_only_page_gives_its_url_value(self, adapter):
        page = FakeControl("DocumentControl", "Docs", value=FakePattern("https://docs.example/", read_only=True),
                           text=FakePattern(visible=["whole page text"]))
        assert walk(adapter, page) == ["Docs", "https://docs.example/"]

    def test_long_value_keeps_its_end(self, adapter):
        combo = FakeControl("ComboBoxControl", "Log", value=FakePattern("a" * 3000 + "b" * 3000))
        out = walk(adapter, combo)
        assert len(out[1]) == pw._A11Y_VISIBLE_ONLY_CHARS and out[1].endswith("b")

    def test_link_urls_are_not_text(self, adapter):
        link = FakeControl("HyperlinkControl", "Open docs", value=FakePattern("https://x.example/"))
        assert walk(adapter, link) == ["Open docs"]

    def test_document_text_in_a_name_is_capped(self, adapter):
        editor = FakeControl("PaneControl", "x" * 50_000)  # Scintilla: name = document
        assert len(walk(adapter, editor)[0]) == pw._A11Y_VISIBLE_ONLY_CHARS

    def test_total_budget(self, adapter):
        kids = [FakeControl("TextControl", f"{i}" + "z" * 999) for i in range(40)]
        out = walk(adapter, FakeControl("PaneControl", "", kids))
        assert sum(len(t) for t in out) == pw._A11Y_MAX_TOTAL_CHARS


class TestDocumentTitle:
    def test_electron_title_from_document(self, adapter, monkeypatch):
        monkeypatch.setattr(pw, "_window_documents", lambda hwnd: [
            doc("file:///app/index.html", name=""),
            doc("https://claude.ai/chat/1", name="Fix the login bug"),
        ])
        assert adapter._document_title(1, "Claude") == "Fix the login bug"

    def test_same_as_title_is_no_better(self, adapter, monkeypatch):
        monkeypatch.setattr(pw, "_window_documents", lambda hwnd: [doc("https://claude.ai/", name="Claude")])
        assert adapter._document_title(1, "Claude") is None

    def test_best_title_only_replaces_app_name_titles(self, adapter, monkeypatch):
        monkeypatch.setattr(pw, "_window_documents", lambda hwnd: [doc("https://claude.ai/c/1", name="Session")])
        assert adapter._best_title(1, "Claude", "claude") == "Session"
        assert adapter._best_title(1, "", "claude") == "Session"
        assert adapter._best_title(1, "Inbox - Mail", "outlook") == "Inbox - Mail"

    def test_offscreen_and_iframes_ignored(self, adapter, monkeypatch):
        monkeypatch.setattr(pw, "_window_documents", lambda hwnd: [
            doc("https://a/", name="Background tab", onscreen=False),
            doc("https://b/", name="Ad frame", nested=True),
        ])
        assert adapter._document_title(1, "Slack") is None
