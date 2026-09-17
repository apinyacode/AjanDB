#!/usr/bin/env bash
# One-command deploy/redeploy for the AjanDB webapp: installs missing system
# dependencies, sets up the Python venv, pulls the latest code, restarts the
# server, and opens a public cloudflared tunnel. Safe to re-run any time -
# every step is idempotent (skips what's already done) and this script never
# discards local changes; it just refuses to auto-pull over them.
#
# Usage:
#   bash deploy.sh            # pull latest, redeploy, open tunnel
#   bash deploy.sh --no-pull  # redeploy what's on disk without touching git
#
# Optional: create webapp/.env (gitignored) with lines like
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...
# to avoid re-exporting keys every run. The browser "API Keys" panel in the
# app itself works too and needs no server-side setup at all.
set -euo pipefail

# --- Re-exec after a git pull so we always run the freshly-pulled version of
# this very script, never a half-updated one (bash doesn't guarantee reading
# a script file atomically while it changes underneath itself). ---
if [ "${AJANDB_REEXEC:-}" != "1" ]; then
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$REPO_DIR"

  if [ "${1:-}" = "--no-pull" ]; then
    echo "==> Skipping git pull (--no-pull)"
  elif [ -n "$(git status --porcelain)" ]; then
    echo "==> Local changes detected in $REPO_DIR - not auto-pulling."
    echo "    Review with 'git status'; commit or stash, then re-run."
  else
    echo "==> Pulling latest code..."
    git pull
  fi

  export AJANDB_REEXEC=1
  exec bash "$REPO_DIR/webapp/deploy.sh" "$@"
fi

WEBAPP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$WEBAPP_DIR"
PORT=8000

echo "==> Checking system dependencies..."
NEED_APT=()
command -v git >/dev/null 2>&1 || NEED_APT+=(git)
python3 -c "import venv" >/dev/null 2>&1 || NEED_APT+=(python3-venv)
command -v pip3 >/dev/null 2>&1 || NEED_APT+=(python3-pip)
command -v tesseract >/dev/null 2>&1 || NEED_APT+=(tesseract-ocr tesseract-ocr-eng tesseract-ocr-tha tesseract-ocr-chi-sim)
command -v ffmpeg >/dev/null 2>&1 || NEED_APT+=(ffmpeg)

if [ ${#NEED_APT[@]} -gt 0 ]; then
  echo "    Installing: ${NEED_APT[*]}"
  sudo apt-get update -qq
  sudo apt-get install -y "${NEED_APT[@]}"
else
  echo "    All present."
fi

echo "==> Python environment..."
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate
pip install -r requirements-dev.txt

if [ -f ".env" ]; then
  echo "==> Loading webapp/.env"
  set -a
  # shellcheck disable=SC1091
  source .env
  set +a
fi

echo "==> Stopping any previous server on port $PORT..."
pkill -9 -f "uvicorn backend.main:app" 2>/dev/null || true
sleep 1

echo "==> Starting server..."
rm -f /tmp/ajandb_uvicorn.log
nohup uvicorn backend.main:app --host 0.0.0.0 --port "$PORT" > /tmp/ajandb_uvicorn.log 2>&1 &
UVICORN_PID=$!
sleep 2
if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
  echo "!! Server failed to start. Last log lines:"
  tail -n 30 /tmp/ajandb_uvicorn.log
  exit 1
fi
echo "    Running (pid $UVICORN_PID). Logs: /tmp/ajandb_uvicorn.log"

echo "==> Setting up cloudflared..."
CLOUDFLARED_BIN="$WEBAPP_DIR/cloudflared"
if [ ! -x "$CLOUDFLARED_BIN" ]; then
  curl -Lo "$CLOUDFLARED_BIN" https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
  chmod +x "$CLOUDFLARED_BIN"
fi

echo "==> Opening tunnel - Ctrl+C stops both the tunnel and the server."
trap 'echo; echo "==> Stopping server (pid $UVICORN_PID)..."; kill "$UVICORN_PID" 2>/dev/null || true' INT TERM EXIT
"$CLOUDFLARED_BIN" tunnel --url "http://localhost:$PORT"
