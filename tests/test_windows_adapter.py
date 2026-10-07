"""Windows adapter parity features that need no OS: page URL choice, top
window filtering and the a11y walk rules. Live checks are in
docs/plans/ui-events.md (Windows implementation notes)."""

import pytest

pw = pytest.importorskip("screenmind.platform_support.windows")


def doc(url, nested=False, onscreen=True, name="Page", element=None, web=True):
    return {"name": name, "url": url, "nested": nested, "onscreen": onscreen,
            "element": element, "web": web}


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

    def test_web_documents_are_left_for_the_cached_walk(self, adapter):
        page = FakeControl("DocumentControl", "Docs", value=FakePattern("https://docs.example/", read_only=True),
                           children=[FakeControl("TextControl", "page text")])
        assert walk(adapter, FakeControl("PaneControl", "", [page])) == []

    def test_read_only_text_viewer_keeps_its_text(self, adapter):
        log = FakeControl("DocumentControl", "Log", value=FakePattern("12:00 started\n12:01 done", read_only=True))
        assert walk(adapter, log) == ["Log", "12:00 started\n12:01 done"]

    @pytest.mark.parametrize("read_only, value, web", [
        (True, "https://app.slack.com/client", True),
        (True, "file:///C:/app/index.html", True),
        (True, "about:newtab", True),
        (False, "https://x.example/", False),     # editable
        (True, "see https://x.example/ for more", False),
        (True, "", False),
        (None, "https://x.example/", False),     # unsupported property
    ])
    def test_is_web_document(self, read_only, value, web):
        assert pw.is_web_document(read_only, value) is web

    def test_window_chrome_skipped_by_type_in_any_language(self, adapter):
        """Chrome's frame in Russian (activity 184): buttons, toolbars, tabs."""
        root = FakeControl("WindowControl", "Заметки", [
            FakeControl("ButtonControl", "Свернуть"),
            FakeControl("ButtonControl", "Развернуть"),
            FakeControl("ToolBarControl", "", [FakeControl("ButtonControl", "Расширения"),
                                               FakeControl("TextControl", "Закладки")]),
            FakeControl("TabItemControl", "GitHub — Википедия"),
            FakeControl("SplitButtonControl", "Скачанные файлы"),
            FakeControl("StatusBarControl", "", [FakeControl("TextControl", "Готово")]),
            FakeControl("PaneControl", "Chrome Legacy Window"),
            FakeControl("TextControl", "Текст заметки о квартальном плане"),
        ])
        assert walk(adapter, root) == ["Заметки", "Текст заметки о квартальном плане"]

    def test_prefix_repeat_keeps_longer_line(self, adapter):
        root = FakeControl("PaneControl", "", [FakeControl("ListItemControl", "Idle Open items review"),
                                               FakeControl("TextControl", "Open items review")])
        assert walk(adapter, root) == ["Idle Open items review"]

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

    def test_total_budget(self, adapter, monkeypatch):
        monkeypatch.setattr(pw, "_A11Y_MAX_TOTAL_CHARS", 20000)
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


# ── Web content (browsers, Electron apps) ───────────────────────────


def node(type_name, name="", children=(), value="", role="", offscreen=False, password=False):
    return {"type": type_name, "name": name, "value": value, "role": role,
            "offscreen": offscreen, "password": password, "children": list(children)}


def web(tree, budget=None):
    texts = []
    pw.web_text(tree, texts, set(), budget or [pw._A11Y_MAX_TOTAL_CHARS])
    return texts


class TestPageDocuments:
    def test_split_view_gives_both_pages(self):
        docs = [doc("https://a.example/"), doc("https://b.example/")]
        assert pw.page_documents(docs) == docs

    def test_browser_pages_count_but_panels_do_not(self):
        page = doc("chrome://settings/")
        docs = [page, doc("devtools://devtools/x.html"), doc("chrome-extension://abc/panel.html"),
                doc("chrome://read-later.top-chrome/"), doc("https://bg.example/", onscreen=False),
                doc("https://ad.example/", nested=True)]
        assert pw.page_documents(docs) == [page]


class TestWebText:
    def test_page_text_in_tree_order(self):
        tree = node("DocumentControl", "Wiki", value="https://ru.wikipedia.org/wiki/GitHub", children=[
            node("TextControl", "GitHub — веб-сервис для хостинга IT-проектов"),
            node("GroupControl", "", [node("ButtonControl", "Ответить"),
                                      node("HyperlinkControl", "Git", value="https://ru.wikipedia.org/wiki/Git")]),
        ])
        assert web(tree) == ["Wiki", "GitHub — веб-сервис для хостинга IT-проектов", "Ответить", "Git"]

    def test_only_on_screen_text(self):
        tree = node("DocumentControl", "Page", children=[
            node("TextControl", "visible paragraph"),
            node("TextControl", "scrolled out paragraph", offscreen=True),
        ])
        assert web(tree) == ["Page", "visible paragraph"]

    def test_site_menus_and_sidebars_skipped(self):
        tree = node("DocumentControl", "Page", children=[
            node("GroupControl", "", [node("HyperlinkControl", "Home")], role="navigation"),
            node("GroupControl", "", [node("TextControl", "Related")], role="complementary"),
            node("GroupControl", "", [node("TextControl", "Main article text")], role="main"),
        ])
        assert web(tree) == ["Page", "Main article text"]

    def test_inputs_give_their_value_but_passwords_nothing(self):
        tree = node("DocumentControl", "Login", children=[
            node("EditControl", "Email", value="me@example.com"),
            node("EditControl", "Password", value="hunter2", password=True),
            node("MenuControl", "", [node("MenuItemControl", "Copy")]),
        ])
        assert web(tree) == ["Login", "Email", "me@example.com"]

    def test_budget(self):
        tree = node("DocumentControl", "", [node("TextControl", f"{i} " + "z" * 98) for i in range(10)])
        assert sum(len(t) for t in web(tree, budget=[250])) == 250

    def test_deep_text_is_read(self):
        """Electron apps keep their text ~30 levels below the Document."""
        n = node("TextControl", "deep message")
        for _ in range(40):
            n = node("GroupControl", "", [n])
        assert web(node("DocumentControl", "App", [n])) == ["App", "deep message"]


class FakeCachedEl:
    def __init__(self, ct, name="", value=None, role=None, children=()):
        self.CachedControlType, self.CachedName = ct, name
        self.CachedIsOffscreen = self.CachedIsPassword = False
        self._props = {pw._UIA_ValueValuePropertyId: value, pw._UIA_AriaRolePropertyId: role}
        self._children = list(children)

    def GetCachedPropertyValue(self, pid):
        return self._props[pid]

    def GetCachedChildren(self):
        return FakeElementArray(self._children) if self._children else None

    def BuildUpdatedCache(self, request):
        assert request.TreeScope == pw._TreeScope_Subtree
        return self


class FakeElementArray:
    def __init__(self, items):
        self.items = items
        self.Length = len(items)

    def GetElement(self, i):
        return self.items[i]


class FakeCacheRequest:
    TreeScope = None

    def AddProperty(self, pid):
        pass


def test_cached_tree_converts_and_caps_nodes(monkeypatch):
    class Client:
        IUIAutomation = type("IUIA", (), {"CreateCacheRequest": lambda self: FakeCacheRequest()})()

    class Auto:
        ControlTypeNames = {50030: "DocumentControl", 50020: "TextControl"}

        class uiautomation:
            class _AutomationClient:
                instance = staticmethod(Client)

    monkeypatch.setattr(pw, "uia", lambda: Auto)
    monkeypatch.setattr(pw, "_A11Y_WEB_MAX_NODES", 3)
    unsupported = object()  # what UIA gives for a property the element lacks
    root = FakeCachedEl(50030, "Page", value="https://x.example/", role=unsupported,
                        children=[FakeCachedEl(50020, f"t{i}", value=unsupported, role="Main") for i in range(5)])
    tree = pw._cached_tree(root)
    assert tree["type"] == "DocumentControl" and tree["value"] == "https://x.example/" and tree["role"] == ""
    assert [c["name"] for c in tree["children"]] == ["t0", "t1"]
    assert tree["children"][0] == node("TextControl", "t0", role="main")


PAGE = node("DocumentControl", "GitHub — Википедия", value="https://ru.wikipedia.org/wiki/GitHub", children=[
    node("TextControl", "GitHub — крупнейший веб-сервис для хостинга IT-проектов и их совместной разработки."),
])
PAGE_LINES = ["GitHub — Википедия",
              "GitHub — крупнейший веб-сервис для хостинга IT-проектов и их совместной разработки."]


class TestExtractWindow:
    """extract_a11y_text on one window, with the UIA tree faked."""

    @pytest.fixture
    def extract(self, adapter, monkeypatch):
        def _extract(app, docs, trees, window=None):
            monkeypatch.setattr(pw, "uia", lambda: None)
            monkeypatch.setattr(pw, "_window_pid", lambda hwnd: 1)
            monkeypatch.setattr(pw, "process_name", lambda pid: app)
            monkeypatch.setattr(pw, "_window_documents", lambda hwnd: docs)
            monkeypatch.setattr(pw, "_cached_tree", lambda el: trees[el])
            monkeypatch.setattr(FakeAuto, "ControlFromHandle", staticmethod(lambda hwnd: window), raising=False)
            adapter._a11y_initialized = True
            return adapter.extract_a11y_text(42)
        return _extract

    def test_browser_gives_only_the_page(self, extract):
        """Not the toolbar, tabs or extension panels (Russian UI, activity 184)."""
        frame = FakeControl("WindowControl", "GitHub — Википедия - Google Chrome", [
            FakeControl("TextControl", "Адресная строка и строка поиска"),
            FakeControl("TextControl", "У расширения есть доступ к этому сайту"),
        ])
        text, source = extract("chrome", [doc("https://ru.wikipedia.org/wiki/GitHub", element="page"),
                                          doc("chrome-extension://abc/panel.html", element="panel")],
                               {"page": PAGE, "panel": node("DocumentControl", "Open Claude")}, window=frame)
        assert source == "a11y" and text.split("\n") == PAGE_LINES

    def test_browser_without_a_page_gives_nothing_so_ocr_runs(self, extract):
        assert extract("msedge", [doc("https://bg.example/", onscreen=False, element="bg")],
                       {"bg": PAGE}) == (None, "none")

    def test_electron_app_reads_its_web_document(self, extract):
        """The Document sits 9 levels down, past the native walk's depth limit."""
        window = FakeControl("WindowControl", "Slack", [
            FakeControl("ButtonControl", "Свернуть"),
            FakeControl("DocumentControl", "", value=FakePattern("https://app.slack.com/", read_only=True)),
        ])
        text, _ = extract("slack", [doc("https://app.slack.com/", element="app"),
                                    doc("", web=False, element="editor")],
                          {"app": PAGE}, window=window)
        assert text.split("\n") == ["Slack"] + PAGE_LINES
