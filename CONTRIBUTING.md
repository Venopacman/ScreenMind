# Contributing to ScreenMind

Contributions are welcome: bug fixes, features, docs and tests.

ScreenMind collects actions and labels them for a workflows app. New features should collect, dedup, unify, label or export actions. See [docs/plans/strip-to-actions.md](docs/plans/strip-to-actions.md).

## Setup

1. Fork and clone:
   ```bash
   git clone https://github.com/YOUR_USERNAME/ScreenMind.git
   cd ScreenMind
   ```

2. Install dependencies with [uv](https://docs.astral.sh/uv/getting-started/installation/):
   ```bash
   uv sync
   ```
   This creates `.venv` from `uv.lock`, including the test tools.
   To add a dependency, run `uv add <package>` and commit `pyproject.toml` and `uv.lock`.

3. Install llama-server (needed for the analysis engine):
   ```bash
   uv run python -m screenmind.setup_llama
   ```
   The app also does this on first start. Download a Gemma model in the Model Hub in the dashboard.

4. Run tests:
   ```bash
   uv run pytest tests -q
   ```

5. Run your branch from a git worktree with `scripts/dev-instance.sh` (from the worktree root). It uses its own data dir (`~/.screenmind-dev/<worktree>`) and a port in 7800-7899, so it does not touch the main instance's data. `scripts/dev-instance.sh info` prints the port and data dir. Stop it with Ctrl+C when you are done, because it captures the screen.

## Reporting Bugs

Open an [issue](https://github.com/ayushh0110/ScreenMind/issues) with:
- Your OS, Python version, and GPU (model + VRAM)
- Steps to reproduce
- Relevant logs or screenshots

## Pull Requests

1. Branch off `main`:
   ```bash
   git checkout -b feature/your-feature-name
   ```
2. Make your changes and add tests where it makes sense.
3. Run `uv run pytest tests -q` to make sure nothing breaks.
4. Open a PR against `main`.

Keep commits focused: one logical change per commit.

## Coding Conventions

### Logging (no bare `print()`)

All output goes through Python's `logging` module. **Never use bare `print()`** in source files. CI rejects PRs that add `print()` calls (`flake8-print`).

```python
import logging

logger = logging.getLogger("screenmind.<module_path>")
# e.g. "screenmind.workers.analysis_worker"

logger.info("Normal operation")
logger.warning("Something unexpected but recoverable")
logger.error("Something failed")
logger.debug("Verbose detail for troubleshooting")
```

**Rules:**
- Declare `logger` at **module level**, after all imports
- Use the `screenmind.<dotted.module.path>` naming convention
- Pick the right level: `info` for milestones, `warning` for degraded state, `error` for failures, `debug` for verbose detail
- **No emoji in logger messages.** They crash on Windows cp1252 terminals. Emoji in UI strings (dashboard, DB text) are fine
- If you genuinely need a `print(file=sys.stderr)` (e.g. startup banners), add `# noqa: T201`

### Why?

Logging goes to `stderr` and, once the app starts (`main.run()`), to a rotating `screenmind.log` in the data dir. `SCREENMIND_LOG_LEVEL` sets the level and `SCREENMIND_LOG_FILE` moves the file. A bare `print()` skips both.

## Where Help is Needed

- **Capture gaps.** The open gaps per OS are listed in [docs/architecture/capture.md, section 8](docs/architecture/capture.md#8-gaps).
- **Wayland testing.** Wayland capture works but needs more real-world testing across distros.
- **Model testing.** Try different Gemma quantizations and variants.
- **Docs.** Setup guides for specific hardware.

## Project Structure

```
ScreenMind/
├── screenmind/               # Main package
│   ├── main.py               # Entry point, starts all services
│   ├── config.py             # Pydantic settings (env + runtime overrides)
│   ├── launcher.py           # Splash screen launcher (tkinter)
│   ├── startup.py            # Cross-platform auto-start registration
│   ├── setup_llama.py        # Auto-detect + install llama-server
│   ├── api/                  # Web dashboard + REST API (FastAPI)
│   ├── assets/               # Bundled logo, favicon
│   ├── capture/              # Screen capture, dedup, UI events
│   ├── engine/               # Gemma analysis, OCR, layout, LLM client, models
│   ├── export/               # Per-day zip archives for the workflows app
│   ├── storage/              # SQLite database layer (schema v11)
│   ├── workers/              # Background workers (capture, analysis, audio)
│   ├── platform_support/     # OS-specific window, a11y and mic detection
│   └── privacy/              # Encryption, sensitive-data and URL filters
├── scripts/                  # dev-instance.sh, e2e_collect.py
├── tests/                    # Test suite (pytest)
└── docs/                     # Capture map, export format, plans, backlog
```

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
