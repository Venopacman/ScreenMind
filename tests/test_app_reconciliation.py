"""Tests for frame labels from rules (Issue #6).

Validates that label_frame() reconciles the OS-detected app name and the
window title (and, in a browser, the URL) into app_name and
activity_category. No model is involved.

See: https://github.com/ayushh0110/ScreenMind/issues/6
"""

import pytest

from evolve_capture.engine.labels import (
    _extract_app_from_title,
    _extract_simple_title,
    _is_browser_window,
    _lookup_known_category,
    _site_category,
    label_frame,
)


# ── Helper: _extract_app_from_title ──────────────────────────────────────────


class TestExtractAppFromTitle:
    """Test the L1 title ' - ' extraction logic."""

    def test_standard_convention(self):
        assert _extract_app_from_title("main.py - Visual Studio Code") == "Visual Studio Code"

    def test_browser_title(self):
        assert _extract_app_from_title("YouTube - Google Chrome") == "Google Chrome"

    def test_multiple_dashes(self):
        assert _extract_app_from_title("John Doe - Engineer - LinkedIn") == "LinkedIn"

    def test_en_dash(self):
        assert _extract_app_from_title("main.py \u2013 IntelliJ IDEA") == "IntelliJ IDEA"

    def test_em_dash(self):
        assert _extract_app_from_title("Document \u2014 LibreOffice") == "LibreOffice"

    def test_no_dash(self):
        assert _extract_app_from_title("AnyType") is None

    def test_none_input(self):
        assert _extract_app_from_title(None) is None

    def test_empty_string(self):
        assert _extract_app_from_title("") is None

    def test_filename_rejection_mkv(self):
        """Reversed convention: 'mpv - movie.mkv' should NOT extract 'movie.mkv'."""
        assert _extract_app_from_title("mpv - movie.mkv") is None

    def test_filename_rejection_py(self):
        assert _extract_app_from_title("nano - config.py") is None

    def test_filename_rejection_txt(self):
        assert _extract_app_from_title("vim - notes.txt") is None

    def test_app_name_with_dot_not_extension(self):
        """App names like 'Vue.js' should not be rejected if not a known extension... 
        but 'js' IS a known extension so it would be rejected."""
        # This is a known limitation — very rare in practice
        assert _extract_app_from_title("Project - Vue.js") is None

    def test_candidate_too_long(self):
        long_name = "A" * 41
        assert _extract_app_from_title(f"content - {long_name}") is None

    def test_candidate_too_short(self):
        assert _extract_app_from_title("content - A") is None

    def test_discord_title(self):
        assert _extract_app_from_title("#general - Discord") == "Discord"

    def test_spotify_title(self):
        assert _extract_app_from_title("Song Name - Artist - Spotify") == "Spotify"


# ── Helper: _extract_simple_title ────────────────────────────────────────────


class TestExtractSimpleTitle:
    """Test the L3 simple title extraction logic."""

    def test_clean_app_name(self):
        assert _extract_simple_title("AnyType") == "AnyType"

    def test_calculator(self):
        assert _extract_simple_title("Calculator") == "Calculator"

    def test_rejects_path(self):
        assert _extract_simple_title("~/Documents") is None

    def test_rejects_terminal_prompt(self):
        assert _extract_simple_title("user@host: ~") is None

    def test_rejects_untitled(self):
        assert _extract_simple_title("Untitled") is None

    def test_rejects_new_document(self):
        assert _extract_simple_title("New Document") is None

    def test_rejects_document_prefix(self):
        assert _extract_simple_title("Document1") is None

    def test_rejects_long_title(self):
        assert _extract_simple_title("This is a very long window title text") is None

    def test_rejects_file_extension(self):
        assert _extract_simple_title("config.py") is None

    def test_microsoft_teams(self):
        assert _extract_simple_title("Microsoft Teams") == "Microsoft Teams"

    def test_empty_string(self):
        assert _extract_simple_title("") is None

    def test_whitespace_only(self):
        assert _extract_simple_title("   ") is None


# ── Helper: _is_browser_window ───────────────────────────────────────────────


class TestIsBrowserWindow:
    """Test browser detection via title suffix."""

    def test_chrome(self):
        assert _is_browser_window("YouTube - Google Chrome") is True

    def test_firefox(self):
        assert _is_browser_window("Gmail - Mozilla Firefox") is True

    def test_edge(self):
        assert _is_browser_window("GitHub - Microsoft Edge") is True

    def test_brave(self):
        assert _is_browser_window("Reddit - Brave") is True

    def test_not_browser(self):
        assert _is_browser_window("main.py - Visual Studio Code") is False

    def test_none(self):
        assert _is_browser_window(None) is False

    def test_no_suffix(self):
        assert _is_browser_window("AnyType") is False

    def test_pwa_no_suffix(self):
        """Edge PWAs don't have browser suffix."""
        assert _is_browser_window("Microsoft Teams") is False

    def test_process_name_firefox(self):
        """Fallback: OS reports 'firefox' even when title has no browser suffix."""
        assert _is_browser_window("Build Log - Jenkins", app_name_hint="firefox") is True

    def test_process_name_chrome(self):
        assert _is_browser_window("Some Web App", app_name_hint="chrome") is True

    def test_process_name_non_browser(self):
        """Non-browser process should not trigger browser detection."""
        assert _is_browser_window("Some Window", app_name_hint="alacritty") is False

    def test_title_suffix_takes_priority(self):
        """Title suffix detection works even without app_name_hint."""
        assert _is_browser_window("YouTube - Google Chrome", app_name_hint=None) is True


# ── Helper: _lookup_known_category ───────────────────────────────────────────


class TestLookupKnownCategory:
    """Test known app category lookup with fuzzy matching."""

    def test_exact_match(self):
        assert _lookup_known_category("alacritty") == "terminal"

    def test_case_insensitive(self):
        assert _lookup_known_category("Alacritty") == "terminal"

    def test_anytype(self):
        assert _lookup_known_category("AnyType") == "writing"

    def test_discord(self):
        assert _lookup_known_category("Discord") == "communication"

    def test_spotify(self):
        assert _lookup_known_category("Spotify") == "media"

    def test_linux_truncation(self):
        """Linux /proc/pid/comm truncates to 15 chars."""
        assert _lookup_known_category("gnome-terminal-") == "terminal"

    def test_version_suffix(self):
        assert _lookup_known_category("anytype 0.38") == "writing"

    def test_word_in_phrase(self):
        """'Microsoft Teams' should match 'teams' in dict."""
        assert _lookup_known_category("Microsoft Teams") == "communication"

    def test_unknown_app(self):
        assert _lookup_known_category("myobscureapp") is None

    def test_trailing_whitespace(self):
        assert _lookup_known_category("  alacritty  ") == "terminal"

    def test_pwsh(self):
        assert _lookup_known_category("pwsh") == "terminal"

    def test_zoom(self):
        assert _lookup_known_category("zoom") == "meeting"

    def test_claude_desktop_is_coding(self):
        assert _lookup_known_category("Claude") == "coding"

    def test_jira_title_in_browser(self):
        assert _site_category(None, "Jira") == "writing"

    def test_greedy_prefix_not_matches_notion(self):
        """'not' should NOT match 'notion' — too short for reverse prefix."""
        assert _lookup_known_category("not") is None

    def test_greedy_prefix_sl_matches_slack(self):
        """'sl' should NOT match 'slack' — too short."""
        assert _lookup_known_category("sl") is None

    def test_greedy_prefix_di_matches_discord(self):
        """'di' should NOT match 'discord' — too short."""
        assert _lookup_known_category("di") is None

    def test_valid_truncation_still_works(self):
        """'gnome-termin' (3 chars short) should still match 'gnome-terminal'."""
        assert _lookup_known_category("gnome-termin") == "terminal"


class TestSiteCategory:
    """Browser windows get their category from the site."""

    def test_url_host(self):
        assert _site_category("https://www.youtube.com/watch?v=x", None) == "media"

    def test_url_subdomain_of_known_site(self):
        assert _site_category("https://team.atlassian.net/wiki/x", None) == "writing"

    def test_specific_host_beats_parent(self):
        assert _site_category("https://music.youtube.com/", None) == "media"
        assert _site_category("https://meet.google.com/abc-defg-hij", None) == "meeting"

    def test_url_without_scheme(self):
        assert _site_category("github.com/org/repo", None) == "coding"

    def test_title_words_without_url(self):
        assert _site_category(None, "Inbox (3) - Gmail - Google Chrome") == "communication"

    def test_unknown_site(self):
        assert _site_category("https://example.com/", "Example - Google Chrome") is None


# ── Full labels: label_frame() scenarios ──────────────────────────────────


class TestLabelFrame:
    """The 4-level hierarchy for the app name, and the category rules."""

    @pytest.fixture(autouse=True)
    def _os_name_not_trusted(self, monkeypatch):
        # These scenarios use Windows/Linux-style process names and titles.
        # macOS resolves the OS owner first; see TestLabelFrameMacOS.
        monkeypatch.setattr("evolve_capture.engine.labels._os_app_name_is_trusted", lambda: False)

    def test_scenario_1_alacritty(self):
        result = label_frame("Alacritty", "user@host: ~")
        assert result.app_name == "Alacritty"
        assert result.activity_category == "terminal"

    def test_scenario_2_anytype(self):
        result = label_frame("AnyType", "AnyType")
        assert result.app_name == "AnyType"
        assert result.activity_category == "writing"

    def test_scenario_3_vscode_from_title(self):
        result = label_frame("code", "main.py - Visual Studio Code")
        assert result.app_name == "Visual Studio Code"
        assert result.activity_category == "coding"

    def test_scenario_4_chrome_youtube(self):
        """Chrome showing YouTube — category from the site: 'media'."""
        result = label_frame("chrome", "YouTube - Google Chrome", "https://www.youtube.com/watch?v=1")
        assert result.app_name == "Google Chrome"
        assert result.activity_category == "media"

    def test_scenario_5_firefox_gmail_no_url(self):
        """Firefox showing Gmail, no URL — category from the title words."""
        result = label_frame("firefox", "Gmail - Mozilla Firefox")
        assert result.app_name == "Mozilla Firefox"
        assert result.activity_category == "communication"

    def test_scenario_6_electron_discord_title(self):
        result = label_frame("electron", "#general - Discord")
        assert result.app_name == "Discord"
        assert result.activity_category == "communication"

    def test_scenario_7_electron_no_title(self):
        """Generic wrapper and an unhelpful title: nothing better than the wrapper."""
        result = label_frame("electron", "Untitled")
        assert result.app_name == "electron"
        assert result.activity_category == "other"

    def test_scenario_8_msedge_pwa_teams(self):
        result = label_frame("msedge", "Microsoft Teams")
        assert result.app_name == "Microsoft Teams"
        assert result.activity_category == "communication"

    def test_scenario_9_msedge_browser_unknown_site(self):
        result = label_frame("msedge", "Some page - Microsoft Edge")
        assert result.app_name == "Microsoft Edge"
        assert result.activity_category == "browsing"

    def test_scenario_10_uwp_calculator(self):
        result = label_frame("ApplicationFrameHost", "Calculator")
        assert result.app_name == "Calculator"
        assert result.activity_category == "other"

    def test_scenario_11_os_none_anytype_title(self):
        result = label_frame(None, "AnyType")
        assert result.app_name == "AnyType"
        assert result.activity_category == "writing"

    def test_scenario_12_os_none_discord_title(self):
        result = label_frame(None, "#general - Discord")
        assert result.app_name == "Discord"
        assert result.activity_category == "communication"

    def test_scenario_13_os_none_untitled(self):
        result = label_frame(None, "Untitled")
        assert result.app_name == "unknown"
        assert result.activity_category == "other"

    def test_scenario_14_st_terminal_not_steam(self):
        result = label_frame("st", "user@host: ~")
        assert result.app_name == "st"
        assert result.activity_category == "terminal"
        assert _lookup_known_category("steam") is None

    def test_scenario_15_vim(self):
        result = label_frame("vim", "config.py")
        assert result.app_name == "vim"

    def test_scenario_16_gnome_terminal_truncated(self):
        result = label_frame("gnome-terminal-", "user@host: ~")
        assert result.app_name == "gnome-terminal-"
        assert result.activity_category == "terminal"

    def test_scenario_18_unknown_app(self):
        result = label_frame("myobscureapp", "Some Window")
        assert result.app_name == "myobscureapp"
        assert result.activity_category == "other"

    def test_scenario_19_browser_process_shadows_title(self):
        """Firefox tab without browser suffix — OS 'firefox' still marks a browser."""
        result = label_frame("firefox", "Build Log - Jenkins")
        assert result.app_name == "Jenkins"
        assert result.activity_category == "browsing"

    def test_url_marks_browser(self):
        result = label_frame("Arc", "PR #12", "https://github.com/org/repo/pull/12")
        assert result.activity_category == "coding"

    def test_no_model_fields(self):
        """Nothing looks at the image: summary and the rest stay empty."""
        result = label_frame("Discord", "#general - Discord")
        assert result.activity_summary == ""
        assert result.detailed_context == ""
        assert result.scene_description == ""
        assert result.visible_text_snippets == []


class TestLabelFrameMacOS:
    """Where the adapter trusts the OS app name (macOS), it wins over title parsing."""

    @pytest.fixture(autouse=True)
    def _os_name_trusted(self, monkeypatch):
        monkeypatch.setattr("evolve_capture.engine.labels._os_app_name_is_trusted", lambda: True)

    def test_terminal_title_suffix_ignored(self):
        """Terminal titles end in the window size, not the app name."""
        result = label_frame("Terminal", "Evolve Capture — python — 120×30")
        assert result.app_name == "Terminal"
        assert result.activity_category == "terminal"

    def test_owner_beats_title_segment(self):
        result = label_frame("Slack", "general (Channel) - TripleTen - Slack")
        assert result.app_name == "Slack"
        assert result.activity_category == "communication"

    def test_no_owner_falls_back_to_title(self):
        result = label_frame(None, "main.py - Visual Studio Code")
        assert result.app_name == "Visual Studio Code"

    def test_macos_browser_owner(self):
        result = label_frame("Google Chrome", "Inbox - Gmail", "https://mail.google.com/mail/u/0/")
        assert result.app_name == "Google Chrome"
        assert result.activity_category == "communication"


class TestTrustsOsAppName:
    """The platform layer decides whether the OS app name is trusted."""

    def test_base_default_false(self):
        from evolve_capture.platform_support.base import PlatformAdapter
        assert PlatformAdapter.trusts_os_app_name.fget(object()) is False

    def test_macos_true(self):
        from evolve_capture.platform_support.macos import MacOSAdapter
        assert MacOSAdapter.trusts_os_app_name.fget(object()) is True

    def test_windows_and_linux_inherit_false(self):
        from evolve_capture.platform_support.windows import WindowsAdapter
        from evolve_capture.platform_support.linux import LinuxAdapter
        from evolve_capture.platform_support.base import PlatformAdapter
        assert WindowsAdapter.trusts_os_app_name is PlatformAdapter.trusts_os_app_name
        assert LinuxAdapter.trusts_os_app_name is PlatformAdapter.trusts_os_app_name

    def test_labels_read_adapter(self, monkeypatch):
        import evolve_capture.engine.labels as analyzer
        fake = type("A", (), {"trusts_os_app_name": True})()
        monkeypatch.setattr(analyzer, "platform_adapter", lambda: fake)
        assert analyzer._os_app_name_is_trusted() is True
        fake.trusts_os_app_name = False
        assert analyzer._os_app_name_is_trusted() is False

    def test_labels_adapter_error_means_untrusted(self, monkeypatch):
        import evolve_capture.engine.labels as analyzer
        def boom():
            raise RuntimeError("no adapter")
        monkeypatch.setattr(analyzer, "platform_adapter", boom)
        assert analyzer._os_app_name_is_trusted() is False
