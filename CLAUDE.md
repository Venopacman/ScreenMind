# Working in this repo

## Main instance vs worktrees

The user's everyday ScreenMind runs from the main checkout (`/Users/pavel/projects/ScreenMind`, branch `custom`):

- dashboard on port 7777
- data in `~/.screenmind` (DB, screenshots, `settings.json`)
- shared `llama-server` on port 5809

Do not stop, restart or reconfigure it unless the user asks. That includes `POST /api/settings` on port 7777.

Feature work happens in git worktrees under `.claude/worktrees/`.

- Base new work on `custom`. A new worktree may start from `main`. If its branch has no commits yet, run `git reset --hard custom` first.
- Don't edit files in the main checkout from a worktree session.

## Running your branch

Never run `python -m screenmind` or `.venv/bin/screenmind` directly in a worktree. Without its own config it uses `~/.screenmind` and writes into the user's real DB.

Use the launcher from the worktree root instead. Start it in the background:

```bash
scripts/dev-instance.sh
```

It gives the worktree its own data dir (`~/.screenmind-dev/<worktree>`) and a fixed port in 7800-7899. It loads the main `.env` (OCR languages, capture on start), then overrides only the isolation settings.

- `scripts/dev-instance.sh info` prints the port and data dir.
- `scripts/dev-instance.sh reset` deletes the worktree's data for a fresh start.
- Stop it with Ctrl+C or `kill -INT <pid>`. Shutdown takes up to a minute.
- Stop it when you are done. It captures the screen and uses the shared `llama-server`.

## What is still shared

- The venv in the main checkout. If your branch needs a new dependency, tell the user before installing it, because it changes the main instance's environment too.
- Models in `~/.screenmind/models`.
- `llama-server` on port 5809. The dev instance reuses it and never stops it. Don't kill it.

## Tests

`pytest` already isolates the data dir (`tests/conftest.py`). Run it with the main venv:

```bash
/Users/pavel/projects/ScreenMind/.venv/bin/python -m pytest tests -q
```
