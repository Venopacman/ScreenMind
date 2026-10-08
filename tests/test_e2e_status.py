"""Status files written by scripts/e2e_collect.py and the summary built from them."""

import importlib.util
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "e2e_collect.py"


@pytest.fixture
def e2e(tmp_path, monkeypatch):
    spec = importlib.util.spec_from_file_location("e2e_collect", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    monkeypatch.setattr(mod, "STATUS_DIR", tmp_path)
    return mod


def _report(e2e, rows, secrets=()):
    rep = e2e.Report(list(secrets))
    for cid, result, evidence in rows:
        rep.add(cid, result, evidence)
    return rep


def test_status_file_round_trip(e2e, tmp_path):
    rep = _report(e2e, [("screenshots", e2e.PASS, "2 displays"),
                        ("calls", e2e.SKIP, "no call")])
    path = tmp_path / "macos.md"
    e2e.write_status(path, {"os_label": "macOS", "state": "ran", "git": "custom abc123"}, rep.rows, [])

    data = e2e._read_status(path)
    assert data["meta"]["state"] == "ran"
    assert [(r["id"], r["result"]) for r in data["results"]] == [
        ("screenshots", "PASS"), ("calls", "SKIP")]
    assert "| Screenshots, every display | **PASS** | 2 displays |" in path.read_text()


def test_evidence_masks_secrets_and_table_breakers(e2e):
    rep = _report(e2e, [("redact_typed", e2e.FAIL, "found Zq123 in a|b\nnext")], secrets=["Zq123"])
    assert rep.rows[0]["evidence"] == "found <secret> in a/b next"


def test_gap_links_follow_the_os(e2e, monkeypatch):
    monkeypatch.setattr(e2e, "OS_KEY", "windows")
    rep = _report(e2e, [("calls", e2e.SKIP, "")])
    assert rep.rows[0]["gaps"] == ["G16", "G17"]
    monkeypatch.setattr(e2e, "OS_KEY", "macos")
    rep = _report(e2e, [("calls", e2e.SKIP, "")])
    assert rep.rows[0]["gaps"] == []


def test_summary_puts_machines_side_by_side(e2e, tmp_path):
    mac = _report(e2e, [("screenshots", e2e.PASS, ""), ("ocr", e2e.FAIL, "")])
    mac.add("browser_url:safari", e2e.PASS, "", label="Browser URL (safari)", gap_key="browser_url")
    e2e.write_status(tmp_path / "macos.md", {"os_label": "macOS", "state": "ran"}, mac.rows, [])
    e2e.write_placeholder("windows")
    (tmp_path / "README.md").write_text("not a status file")

    text = e2e.write_summary().read_text()
    assert "| Data point | [macos](macos.md) | [windows](windows.md) |" in text
    assert "| *State* | ran | not run yet |" in text
    assert "| OCR text | FAIL | - |" in text
    # Browser rows sort with the other browser checks, after OCR
    assert text.index("Browser URL (safari)") > text.index("OCR text")


def test_settings_row_lists_only_what_differs_between_machines(e2e):
    log = ("2026-10-08 10:00:00 [screenmind.main] INFO: Settings that differ from the defaults: "
           "data_dir=C:\\tmp\\x (env); api_port=7901 (env); capture_interval=10 (env); "
           "ocr_languages=en,ru (.env); capture_on_start=True (.env); blocked_apps=bank (.env)\n")
    assert e2e._settings_text(log) == "ocr_languages=en,ru (.env); blocked_apps=bank (.env)"
    only_forced = "Settings that differ from the defaults: data_dir=/t (env); capture_interval=10 (env)\n"
    assert e2e._settings_text(only_forced) == "all at code defaults"
    assert e2e._settings_text("no such line") == ""
