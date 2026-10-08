"""Replay benchmark: run stored frames through the real analysis pipeline.

Each scenario is a frozen frame (screenshot + what the capture side saw:
app, window title, a11y text, browser URL, user actions) plus expectations
on what analysis should store. `run` feeds every frame through
AnalysisWorker._process against a throwaway data dir, reads the row back
and checks it. Nothing is written to ~/.screenmind.

Fixtures hold real screen content, so they live outside the repo, in
~/.screenmind-bench (override with SCREENMIND_BENCH_DIR):

    fixtures/<name>/screenshot.jpg
    fixtures/<name>/capture.json
    scenarios.yaml          expectations, see bench/scenarios.example.yaml
    runs/<timestamp>.json   results of each run

Usage (from the repo or worktree root; see bench/README.md):
    uv run python bench/replay.py freeze 324 --name chrome_ticket
    uv run python bench/replay.py run [--only NAME ...] [--repeat N]
"""

import argparse
import asyncio
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

BENCH = Path(os.environ.get("SCREENMIND_BENCH_DIR", "~/.screenmind-bench")).expanduser()
SOURCE_DATA = Path("~/.screenmind").expanduser()

GENERIC = re.compile(r"\b(is interacting with|likely|is working (on|within))\b", re.I)
BROWSER_WORDS = {"chrome", "chromium", "safari", "firefox", "edge", "brave", "arc", "opera", "vivaldi"}


def _say(msg: str):
    print(msg, flush=True)  # noqa: T201 (CLI output)


# ── freeze ──────────────────────────────────────────────────────────────

def freeze(activity_id: int, name: str, source_db: Path, force: bool):
    """Copy one stored frame into fixtures/<name>."""
    con = sqlite3.connect(f"file:{source_db}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    row = con.execute("SELECT * FROM activities WHERE id = ?", (activity_id,)).fetchone()
    if not row:
        sys.exit(f"activity {activity_id} not found in {source_db}")

    out = BENCH / "fixtures" / name
    if out.exists() and not force:
        sys.exit(f"{out} exists (use --force to replace)")
    out.mkdir(parents=True, exist_ok=True)

    src = Path(row["screenshot_path"])
    if not src.is_absolute():
        src = SOURCE_DATA / src
    from screenmind.privacy.encryption import open_image
    open_image(src).convert("RGB").save(out / "screenshot.jpg", quality=92)

    # The capture-side app (detected_app). app_name is what analysis wrote, so it
    # is only a fallback for rows that have a window title but no detected_app.
    # No app and no title is an empty display: keep it unlabeled.
    app = row["detected_app"] or (row["app_name"] if row["window_title"] else None)
    keys = row.keys()
    # Text with no OCR boxes came from a11y at capture time: replay it as a11y input.
    a11y = row["ocr_text"] if row["ocr_text"] and not row["ocr_boxes"] else None
    is_browser = any(w in (app or "").lower().split() for w in BROWSER_WORDS)
    capture = {
        "source_activity_id": activity_id,
        "timestamp": row["timestamp"],
        "app_name": app,
        "window_title": row["window_title"],
        "a11y_text": a11y,
        # Older rows took active_url from OCR text, so only trust it for browsers.
        "browser_url": row["active_url"] if is_browser and row["active_url"] else None,
        "user_actions": row["user_actions"] if "user_actions" in keys else None,
        # What production stored, for side-by-side comparison
        "stored": {k: row[k] for k in ("app_name", "category", "summary", "analysis_method")},
    }
    (out / "capture.json").write_text(json.dumps(capture, indent=1, ensure_ascii=False))
    _say(f"froze #{activity_id} -> {out}  ({app} | {row['window_title']})")


# ── run ─────────────────────────────────────────────────────────────────

def _main_env_file():
    """The .env next to the main checkout. A worktree has none, but the main
    instance runs with it (OCR languages and so on), so replay should too."""
    if (REPO / ".env").exists():
        return REPO / ".env"
    try:
        common = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--git-common-dir"],
                                capture_output=True, text=True).stdout.strip()
        env = (REPO / common).resolve().parent / ".env"
        return env if env.exists() else None
    except Exception:
        return None


def _isolate_settings(tmp: Path):
    """Run like the main instance (its .env and settings.json), but with a
    throwaway data dir."""
    from screenmind.config import settings
    env = _main_env_file()
    if env:
        for line in env.read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and not key.strip().startswith("#"):
                try:
                    setattr(settings, key.strip().lower(), value.strip().strip('"\''))
                except Exception:
                    pass
    live = SOURCE_DATA / "settings.json"
    if live.exists():
        for key, value in json.loads(live.read_text()).items():
            try:
                setattr(settings, key, value)
            except Exception:
                pass
    settings.data_dir = str(tmp)
    return settings


def _git_head() -> str:
    try:
        return subprocess.run(["git", "-C", str(REPO), "log", "-1", "--format=%h %s"],
                              capture_output=True, text=True).stdout.strip()
    except Exception:
        return "?"


async def _replay_one(worker, db, name: str):
    from PIL import Image
    from screenmind.workers.capture_worker import CaptureResult

    fx = BENCH / "fixtures" / name
    cap = json.loads((fx / "capture.json").read_text())
    image = Image.open(fx / "screenshot.jpg").convert("RGB")
    path = Path(db._db_path).parent / f"{name}.jpg"
    image.save(path, quality=92)
    fields = dict(
        filepath=path,
        timestamp=datetime.now(),  # fresh, so the staleness skip never fires
        window_title=cap.get("window_title"),
        app_name=cap.get("app_name"),
        image=image,
        a11y_text=cap.get("a11y_text"),
        user_actions=cap.get("user_actions"),
        browser_url=cap.get("browser_url"),
    )
    # Older code has fewer CaptureResult fields; drop the ones it does not know.
    known = set(CaptureResult.__dataclass_fields__)
    capture = CaptureResult(**{k: v for k, v in fields.items() if k in known})
    start = time.time()
    await worker._process(capture)
    elapsed = time.time() - start
    row = db._get_conn().execute(
        "SELECT * FROM activities WHERE screenshot_path = ? ORDER BY id DESC LIMIT 1", (str(path),)
    ).fetchone()
    return dict(row) if row else None, elapsed


_WORDS = None


def _word_ratio(text):
    """Share of 3+ letter tokens that are dictionary words: a rough OCR quality score."""
    global _WORDS
    if _WORDS is None:
        try:
            _WORDS = {w.strip().lower() for w in open("/usr/share/dict/words") if len(w.strip()) >= 3}
        except OSError:
            _WORDS = set()
    toks = re.findall(r"[A-Za-z]{3,}", text or "")
    if not toks or not _WORDS:
        return None
    return round(sum(t.lower() in _WORDS for t in toks) / len(toks), 3)


def _text_source(row) -> str:
    text, boxes = row.get("ocr_text"), row.get("ocr_boxes")
    if not text:
        return "none"
    if not boxes:
        return "a11y"
    return "a11y+ocr" if "--- (from image OCR) ---" in text else "ocr"


def check(row, expect: dict, elapsed: float) -> list:
    """Return (check, ok, detail) for every expectation."""
    if row is None:
        return [("row_written", False, "no activity row")]
    out = []
    summary = f"{row.get('summary') or ''} {row.get('details') or ''}"
    text = row.get("ocr_text") or ""
    low_sum, low_text = summary.lower(), text.lower()

    def add(name, ok, detail):
        out.append((name, bool(ok), detail))

    if "status" in expect:
        add("status", row.get("status") == expect["status"], row.get("status"))
    if "app_name_any" in expect:
        add("app_name_any", any(a.lower() in (row.get("app_name") or "").lower() for a in expect["app_name_any"]),
            row.get("app_name"))
    if "category_in" in expect:
        add("category_in", row.get("category") in expect["category_in"], row.get("category"))
    if "category_not_in" in expect:
        add("category_not_in", row.get("category") not in expect["category_not_in"], row.get("category"))
    if "summary_any" in expect:
        hits = [k for k in expect["summary_any"] if k.lower() in low_sum]
        add("summary_any", hits, hits or (row.get("summary") or "")[:80])
    if "summary_none" in expect:
        bad = [k for k in expect["summary_none"] if k.lower() in low_sum]
        add("summary_none", not bad, bad or "clean")
    if expect.get("summary_not_generic"):
        add("summary_not_generic", not GENERIC.search(row.get("summary") or ""), (row.get("summary") or "")[:80])
    if "text_max_chars" in expect:
        add("text_max_chars", len(text) <= expect["text_max_chars"], len(text))
    if "text_min_chars" in expect:
        add("text_min_chars", len(text) >= expect["text_min_chars"], len(text))
    if "text_any" in expect:
        hits = [k for k in expect["text_any"] if k.lower() in low_text]
        add("text_any", hits, hits or "none found")
    if "text_none" in expect:
        bad = [k for k in expect["text_none"] if k.lower() in low_text]
        add("text_none", not bad, bad or "clean")
    if "text_source_in" in expect:
        src = _text_source(row)
        add("text_source_in", src in expect["text_source_in"], src)
    if "active_url" in expect:
        add("active_url", row.get("active_url") == expect["active_url"], row.get("active_url"))
    if "active_url_prefix" in expect:
        url = row.get("active_url") or ""
        add("active_url_prefix", url.startswith(expect["active_url_prefix"]), url or None)
    if "max_seconds" in expect:
        add("max_seconds", elapsed <= expect["max_seconds"], round(elapsed, 1))
    return out


async def _run(scenarios: dict, only: list, repeat: int):
    tmp = Path(tempfile.mkdtemp(prefix="screenmind-bench-"))
    _isolate_settings(tmp)
    from screenmind.storage.database import Database
    from screenmind.workers.analysis_worker import AnalysisWorker

    db = Database(tmp / "screenmind.db")
    worker = AnalysisWorker(asyncio.Queue(), db)

    results = []
    for name, spec in scenarios.items():
        if only and name not in only:
            continue
        if not (BENCH / "fixtures" / name / "capture.json").exists():
            results.append({"name": name, "error": "fixture missing"})
            continue
        for i in range(repeat):
            worker._app_cache.clear()  # every replay is a full analysis
            row, elapsed = await _replay_one(worker, db, name)
            checks = check(row, spec.get("expect", {}), elapsed)
            results.append({
                "name": name, "run": i + 1, "seconds": round(elapsed, 1),
                "gap": spec.get("known_gap"),
                "checks": [{"check": c, "ok": ok, "got": d} for c, ok, d in checks],
                "row": {k: (row or {}).get(k) for k in
                        ("app_name", "category", "summary", "active_url", "analysis_method")}
                       | {"text_chars": len((row or {}).get("ocr_text") or ""),
                          "text_word_ratio": _word_ratio((row or {}).get("ocr_text")),
                          "text_source": _text_source(row or {})},
            })
            _print_result(results[-1])
    shutil.rmtree(tmp, ignore_errors=True)
    return results


def _print_result(r):
    if "error" in r:
        _say(f"ERROR {r['name']}: {r['error']}")
        return
    failed = [c for c in r["checks"] if not c["ok"]]
    status = "PASS" if not failed else ("GAP " if r["gap"] else "FAIL")
    _say(f"{status} {r['name']} (run {r['run']}, {r['seconds']}s)  "
          f"[{r['row']['category']}] {(r['row']['summary'] or '')[:90]}")
    for c in failed:
        _say(f"       x {c['check']}: got {c['got']}")
    if failed and r["gap"]:
        _say(f"       known gap: {r['gap']}")


def _summary(results):
    done = [r for r in results if "checks" in r]
    if not done:
        return {}
    passed = sum(all(c["ok"] for c in r["checks"]) for r in done)
    gaps = sum(1 for r in done if r["gap"] and not all(c["ok"] for c in r["checks"]))
    generic = sum(bool(GENERIC.search(r["row"]["summary"] or "")) for r in done)
    secs = sorted(r["seconds"] for r in done)
    return {
        "runs": len(done), "passed": passed, "known_gaps": gaps,
        "failed": len(done) - passed - gaps,
        "generic_summary_share": round(generic / len(done), 2),
        "median_seconds": secs[len(secs) // 2],
        "max_text_chars": max(r["row"]["text_chars"] for r in done),
    }


def run(only: list, repeat: int):
    spec_file = BENCH / "scenarios.yaml"
    if not spec_file.exists():
        sys.exit(f"{spec_file} missing (start from bench/scenarios.example.yaml)")
    scenarios = yaml.safe_load(spec_file.read_text())["scenarios"]
    results = asyncio.run(_run(scenarios, only, repeat))
    summary = _summary(results)
    _say("\n" + json.dumps(summary, indent=1))
    (BENCH / "runs").mkdir(parents=True, exist_ok=True)
    out = BENCH / "runs" / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.write_text(json.dumps({"code": _git_head(), "taken_at": datetime.now().isoformat(timespec="seconds"),
                               "summary": summary, "results": results}, indent=1, default=str,
                              ensure_ascii=False))
    _say(f"saved {out}")
    return 1 if summary.get("failed") else 0


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    f = sub.add_parser("freeze", help="copy a stored frame into a fixture")
    f.add_argument("activity_id", type=int)
    f.add_argument("--name", required=True)
    f.add_argument("--db", type=Path, default=SOURCE_DATA / "screenmind.db")
    f.add_argument("--force", action="store_true")
    r = sub.add_parser("run", help="replay all scenarios and check expectations")
    r.add_argument("--only", nargs="*", default=[])
    r.add_argument("--repeat", type=int, default=1, help="runs per scenario, to see flaky output")
    a = p.parse_args()
    if a.cmd == "freeze":
        freeze(a.activity_id, a.name, a.db, a.force)
        return 0
    return run(a.only, a.repeat)


if __name__ == "__main__":
    sys.exit(main())
