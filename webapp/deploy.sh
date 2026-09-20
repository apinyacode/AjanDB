#!/usr/bin/env bash
# One-command deploy/redeploy for the AjanDB webapp. Two targets:
#
#   bash deploy.sh                 # = "local": installs missing system deps,
#                                   #   sets up the venv, restarts uvicorn,
#                                   #   opens a public cloudflared tunnel.
#                                   #   This is the fully-supported path -
#                                   #   everything (uploads, OCR, search,
#                                   #   compile) works exactly as developed.
#   bash deploy.sh --no-tunnel     # same as above but skips cloudflared -
#                                   #   the server stays reachable only at
#                                   #   http://localhost:8000/ on this
#                                   #   machine. No tunnel flakiness to
#                                   #   debug at all, at the cost of only
#                                   #   this machine being able to reach it
#                                   #   (no other device, no public URL to
#                                   #   share). Good for local-only testing.
#   bash deploy.sh vercel          # deploys to Vercel with `vercel deploy`.
#                                   #   Read the warning it prints before
#                                   #   using this - see "Known limitations
#                                   #   of the vercel target" below.
#
# Flags (either target): --no-pull skips `git pull`. --no-tunnel (local
# target only) skips the cloudflared tunnel - see above. --yes skips the
# interactive confirmation prompt (vercel target only, for scripted use).
# vercel target only: --prod passes --prod through to `vercel deploy`.
#
# Optional: create webapp/.env (gitignored) with lines like
#   ANTHROPIC_API_KEY=sk-ant-...
#   OPENAI_API_KEY=sk-...
# to avoid re-exporting keys every run (local target). The browser
# "API Keys" panel in the app itself works too and needs no server-side
# setup at all.
#
# --- Known limitations of the vercel target ---
# Vercel runs this app's backend as stateless serverless functions, which
# breaks three things this app currently depends on:
#   1. SQLite storage (data/ajandb.sqlite3) - the function's filesystem is
#      ephemeral and not shared across invocations, so anything written by
#      one request (an upload) is simply gone by the next request. There is
#      no persistent library on Vercel with this codebase as-is.
#   2. The classical OCR engine - it shells out to the `tesseract` binary,
#      which isn't present in Vercel's Python runtime and can't be apt-
#      installed there. Only the vision-LLM engine (Claude/GPT-4o via API
#      key, no system binary) has any chance of working.
#   3. Background upload jobs - /api/upload hands OCR to a background
#      thread and returns immediately; a serverless function has no
#      "background" after it returns a response, and OCR for a real
#      multi-page scan will usually exceed Vercel's function timeout anyway.
# In short: this target is useful for a quick UI preview or kicking the
# tires on the frontend, not for real uploads/search/library use. The
# `local` target (with the cloudflared tunnel) is the one that actually
# works end-to-end today. A real serverless port would need an external
# database (e.g. hosted Postgres) and dropping/reworking all three items
# above - ask for that explicitly if you want it; it's a bigger change than
# this script.
set -euo pipefail

# --- Parse args (before the re-exec, so we know whether to pull; the same
# args are forwarded unchanged to the re-exec'd copy of this script below,
# where they're parsed again for the real work). ---
TARGET="local"
NO_PULL=0
NO_TUNNEL=0
for arg in "$@"; do
  case "$arg" in
    local|vercel) TARGET="$arg" ;;
    --no-pull) NO_PULL=1 ;;
    --no-tunnel) NO_TUNNEL=1 ;;
  esac
done

# --- Re-exec after a git pull so we always run the freshly-pulled version of
# this very script, never a half-updated one (bash doesn't guarantee reading
# a script file atomically while it changes underneath itself). ---
if [ "${AJANDB_REEXEC:-}" != "1" ]; then
  REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  cd "$REPO_DIR"

  if [ "$NO_PULL" = "1" ]; then
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

deploy_local() {
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

  if [ "$NO_TUNNEL" = "1" ]; then
    echo "==> Skipping tunnel (--no-tunnel)."
    echo "    Open http://localhost:$PORT/ on this machine - no public URL,"
    echo "    no tunnel to drop or debug."
    echo "    Stop the server with: kill $UVICORN_PID   (or: pkill -f 'uvicorn backend.main:app')"
    return 0
  fi

  echo "==> Setting up cloudflared..."
  CLOUDFLARED_BIN="$WEBAPP_DIR/cloudflared"
  if [ ! -x "$CLOUDFLARED_BIN" ]; then
    curl -Lo "$CLOUDFLARED_BIN" https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64
    chmod +x "$CLOUDFLARED_BIN"
  fi

  echo "==> Opening tunnel - Ctrl+C stops both the tunnel and the server."
  trap 'echo; echo "==> Stopping server (pid $UVICORN_PID)..."; kill "$UVICORN_PID" 2>/dev/null || true' INT TERM EXIT
  "$CLOUDFLARED_BIN" tunnel --url "http://localhost:$PORT"
}

deploy_vercel() {
  local skip_confirm=0
  local vercel_args=()
  for arg in "$@"; do
    case "$arg" in
      --yes) skip_confirm=1 ;;
      --prod) vercel_args+=(--prod) ;;
    esac
  done

  cat <<'EOF'
==> WARNING: the vercel target does NOT work like the local target.

Vercel runs this backend as stateless serverless functions, which breaks:
  1. SQLite storage - uploads will not persist; the library is effectively
     wiped between requests.
  2. The classical OCR engine - it needs the `tesseract` binary, which
     Vercel's Python runtime doesn't have. Only the vision-LLM engine
     (needs an Anthropic/OpenAI API key) has any chance of working.
  3. Background upload jobs - OCR for a real scanned document will likely
     exceed Vercel's function timeout; there's no background thread that
     survives past the response the way there is locally.

This is only useful for a quick UI preview, not for real uploads/search/
library use. Use the `local` target for that.
EOF

  if [ "$skip_confirm" != "1" ]; then
    read -r -p "Continue with a Vercel deploy anyway? [y/N] " reply
    case "$reply" in
      [yY]|[yY][eE][sS]) ;;
      *) echo "Aborted."; exit 1 ;;
    esac
  fi

  if ! command -v vercel >/dev/null 2>&1; then
    echo "!! The 'vercel' CLI isn't installed. Install it with:"
    echo "     npm install -g vercel"
    echo "   then re-run this script."
    exit 1
  fi

  if [ ! -f "vercel.json" ]; then
    echo "==> No webapp/vercel.json found - writing a minimal one."
    cat > vercel.json <<'EOF'
{
  "builds": [{ "src": "backend/main.py", "use": "@vercel/python" }],
  "routes": [{ "src": "/(.*)", "dest": "backend/main.py" }]
}
EOF
  fi

  echo "==> Running vercel deploy..."
  echo "    Set ANTHROPIC_API_KEY/OPENAI_API_KEY as Vercel project env vars"
  echo "    (dashboard or 'vercel env add') if you want vision-LLM uploads"
  echo "    or Data Generation to work without the browser API-key panel."
  vercel deploy "${vercel_args[@]}"
}

case "$TARGET" in
  local) deploy_local ;;
  vercel) deploy_vercel "$@" ;;
esac
