# Contributing to ScreenMind

Contributions are welcome — bug fixes, features, docs, tests, all of it.

## Setup

1. Fork and clone:
   ```bash
   git clone https://github.com/YOUR_USERNAME/ScreenMind.git
   cd ScreenMind
   ```

2. Install dependencies:
   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   pip install -r requirements.txt
   pip install -r requirements-test.txt
   ```

3. Set up a Gemma model (needed for the analysis engine):
   ```bash
   python -m screenmind.setup_llama
   ```
   Or use the Model Hub in the web dashboard to download a model.

4. Run tests:
   ```bash
   pytest
   ```

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
2. Make your changes, add tests if applicable
3. Run `pytest` to make sure nothing breaks
4. Open a PR against `main`

Keep commits focused — one logical change per commit.

## Coding Conventions

### Logging (no bare `print()`)

All output goes through Python's `logging` module — **never use bare `print()`** in source files. CI will reject PRs that add `print()` calls (enforced via `flake8-print`).

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
- **No emoji in logger messages** — they crash on Windows cp1252 terminals. Emoji in UI strings (overlay notifications, DB text) are fine
- If you genuinely need a `print(file=sys.stderr)` (e.g. startup banners), add `# noqa: T201`

### Why?

ScreenMind uses MCP stdio transport — any stray `print()` to stdout corrupts the protocol and crashes IDE integrations. All logging goes to `stderr` by default.

## Where Help is Needed

- **macOS support** — screen capture on macOS needs work
- **Wayland testing** — Wayland capture works but needs more real-world testing across distros
- **Model testing** — trying ScreenMind with different Gemma quantizations and variants
- **Agent recipes** — writing and sharing useful agent configurations in `default_agents/`
- **MCP integrations** — expanding the MCP server with new tools
- **Docs** — tutorials, setup guides for specific hardware, video walkthroughs

## Project Structure

```
ScreenMind/
├── screenmind/               # Main package
│   ├── main.py               # Entry point — starts all services
│   ├── config.py             # Pydantic settings (env + runtime overrides)
│   ├── launcher.py           # Splash screen launcher (tkinter)
│   ├── startup.py            # Cross-platform auto-start registration
│   ├── setup_llama.py        # Auto-detect + install llama-server
│   ├── screenmind_sdk.py     # SDK for Python plugin agents
│   ├── mcp_server.py         # MCP server for Claude/Cursor/VS Code
│   ├── api/                  # Web dashboard + REST API (FastAPI)
│   ├── assets/               # Bundled logo, favicon
│   ├── capture/              # Screen capture (mss, Wayland, hotkeys)
│   ├── engine/               # Analysis, LLM client, embeddings, agents
│   ├── storage/              # SQLite database layer
│   ├── workers/              # Background workers (capture, analysis, audio)
│   ├── integrations/         # Notion, webhooks, Obsidian
│   ├── platform_support/     # OS-specific window detection
│   └── privacy/              # Encryption, sensitive data redaction
├── tests/                    # Test suite — 29 modules (pytest)
├── default_agents/           # 4 built-in agents (.md)
└── docs/                     # BUILD_YOUR_OWN_AGENT.md
```

## Code of Conduct

This project follows the [Contributor Covenant](CODE_OF_CONDUCT.md).
