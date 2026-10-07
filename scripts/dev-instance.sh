#!/usr/bin/env bash
# Run an isolated ScreenMind instance from a git worktree, next to the main one.
#
# The main instance runs from the main checkout: port 7777, data in ~/.screenmind.
# A worktree instance gets its own data dir and port, so it never touches the
# main DB, screenshots or settings. It shares the downloaded models
# (~/.screenmind/models) and the running llama-server. It uses the worktree's
# own venv if it has one (`uv sync` in the worktree), else the main checkout's.
#
# Usage (from the worktree root):
#   scripts/dev-instance.sh [run|info|reset]
#
#   run    start the instance in the foreground (default)
#   info   print the port, dashboard URL and data dir
#   reset  delete this worktree's data dir (fresh DB next run)

set -euo pipefail

CODE_DIR="$(cd "$(git rev-parse --show-toplevel)" && pwd -P)"
# The first entry of `git worktree list` is always the main checkout
MAIN_ROOT="$(git worktree list --porcelain | sed -n '1s/^worktree //p')"
MAIN_ROOT="$(cd "$MAIN_ROOT" && pwd -P)"

if [ "$CODE_DIR" = "$MAIN_ROOT" ]; then
    echo "This is the main checkout. Run this script from a worktree." >&2
    exit 1
fi

NAME="$(basename "$CODE_DIR")"
DATA="$HOME/.screenmind-dev/$NAME"
# Stable port per worktree in 7800-7899, so the dashboard URL survives restarts
PORT=$((7800 + $(printf %s "$NAME" | cksum | cut -d' ' -f1) % 100))

case "${1:-run}" in
    info)
        echo "worktree:  $CODE_DIR"
        echo "data dir:  $DATA"
        echo "dashboard: http://127.0.0.1:$PORT"
        ;;
    reset)
        rm -rf "$DATA"
        echo "Deleted $DATA"
        ;;
    run)
        # Same personal settings as the main instance (OCR languages, capture on start...)
        if [ -f "$MAIN_ROOT/.env" ]; then
            set -a
            # shellcheck disable=SC1091
            . "$MAIN_ROOT/.env"
            set +a
        fi

        # Isolation. Env vars beat .env, and settings.json starts empty in the new data dir.
        export DATA_DIR="$DATA"
        export SCREENMIND_DATA_DIR="$DATA"
        export API_PORT="$PORT"
        export SETUP_COMPLETE=true

        mkdir -p "$DATA"
        # A missing DB means "first run", which rewrites ~/Desktop/ScreenMind.command
        # to point at this worktree. An empty file is a valid new SQLite DB.
        [ -e "$DATA/screenmind.db" ] || : > "$DATA/screenmind.db"

        echo "ScreenMind dev instance: $NAME"
        echo "  dashboard: http://127.0.0.1:$PORT"
        echo "  data dir:  $DATA"
        # Python from the worktree's .venv if it exists (a branch with changed
        # deps runs `uv sync` there), else the main checkout's .venv.
        # bin/ on macOS/Linux, Scripts/ on Windows Git Bash.
        # SCREENMIND_PYTHON overrides both.
        PY=""
        for venv in "$CODE_DIR/.venv" "$MAIN_ROOT/.venv"; do
            for p in "$venv/bin/python" "$venv/Scripts/python.exe"; do
                if [ -z "$PY" ] && [ -x "$p" ]; then PY="$p"; fi
            done
        done
        PY="${SCREENMIND_PYTHON:-$PY}"
        echo "  python:    $PY"
        cd "$CODE_DIR"
        # -m with cwd = worktree imports the worktree's code, not the main checkout's
        exec "$PY" -m screenmind
        ;;
    *)
        echo "Unknown command: $1 (use run, info or reset)" >&2
        exit 2
        ;;
esac
