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
# Sharing with testers (local target, when the tunnel is on):
#   bash deploy.sh                     # public link + a tester password,
#                                       #   generated on first use and kept in
#                                       #   webapp/.env (same password next time)
#   bash deploy.sh --password=lotus25  # choose the password yourself (saved too)
#   bash deploy.sh --no-password       # share with NO password - anyone with
#                                       #   the link can read and delete everything
# The link and password are printed in a box once the tunnel is up. Testers
# open the link and type the password into the browser's sign-in box (any
# username). The link changes on every run; the password doesn't. A password
# set with --password also applies with --no-tunnel (handy to try it locally).
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
NO_PASSWORD=0
PASSWORD_ARG=""
for arg in "$@"; do
  case "$arg" in
    local|vercel) TARGET="$arg" ;;
    --no-pull) NO_PULL=1 ;;
    --no-tunnel) NO_TUNNEL=1 ;;
    --no-password) NO_PASSWORD=1 ;;
    --password=*) PASSWORD_ARG="${arg#--password=}" ;;
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
  command -v curl >/dev/null 2>&1 || NEED_APT+=(curl)
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

  # --- Tester password (see "Sharing with testers" at the top). Exported
  # before uvicorn starts, which is where the app reads it (backend/main.py's
  # require_tester_password). ---
  if [ "$NO_PASSWORD" = "1" ]; then
    unset AJANDB_PASSWORD
  elif [ -n "$PASSWORD_ARG" ]; then
    AJANDB_PASSWORD="$PASSWORD_ARG"
    save_env_var AJANDB_PASSWORD "$AJANDB_PASSWORD"
  elif [ -z "${AJANDB_PASSWORD:-}" ] && [ "$NO_TUNNEL" != "1" ]; then
    AJANDB_PASSWORD="$(python3 -c "import secrets; a='abcdefghjkmnpqrstuvwxyz23456789'; print(''.join(secrets.choice(a) for _ in range(10)))")"
    save_env_var AJANDB_PASSWORD "$AJANDB_PASSWORD"
    echo "==> Created a tester password (saved in webapp/.env)."
  fi
  [ -n "${AJANDB_PASSWORD:-}" ] && export AJANDB_PASSWORD

  echo "==> Stopping any previous server on port $PORT..."
  pkill -9 -f "uvicorn backend.main:app" 2>/dev/null || true
  sleep 1

  echo "==> Starting server..."
  rm -f /tmp/ajandb_uvicorn.log
  nohup uvicorn backend.main:app --host 0.0.0.0 --port "$PORT" > /tmp/ajandb_uvicorn.log 2>&1 &
  UVICORN_PID=$!
  # Wait until it actually answers, not just until the process exists: the
  # first start after an install/update compiles every module (OpenCV,
  # PyMuPDF, PyThaiNLP, both model SDKs...) and took ~20s even on a fast
  # machine - a fixed short sleep said "Running, open localhost" while
  # nothing was listening yet, so the browser got "connection refused".
  echo "    Waiting for it to finish starting (the first start after an update can take a minute)..."
  for _ in $(seq 1 180); do
    if ! kill -0 "$UVICORN_PID" 2>/dev/null; then
      echo "!! Server failed to start. Last log lines:"
      tail -n 30 /tmp/ajandb_uvicorn.log
      exit 1
    fi
    if curl -s -o /dev/null "http://localhost:$PORT/"; then
      break
    fi
    sleep 1
  done
  if ! curl -s -o /dev/null "http://localhost:$PORT/"; then
    echo "!! Server is running but still not answering after 3 minutes. Last log lines:"
    tail -n 30 /tmp/ajandb_uvicorn.log
    exit 1
  fi
  echo "    Running and answering (pid $UVICORN_PID). Logs: /tmp/ajandb_uvicorn.log"

  if [ "$NO_TUNNEL" = "1" ]; then
    echo "==> Skipping tunnel (--no-tunnel)."
    echo "    Open http://localhost:$PORT/ on this machine - no public URL,"
    echo "    no tunnel to drop or debug."
    echo "    On a Chromebook (Linux), if localhost doesn't load, try"
    echo "    http://penguin.linux.test:$PORT/ instead."
    [ -n "${AJANDB_PASSWORD:-}" ] && echo "    It asks for the tester password (any username) - see webapp/.env."
    echo "    Stop the server with: kill $UVICORN_PID   (or: pkill -f 'uvicorn backend.main:app')"
    return 0
  fi

  echo "==> Setting up cloudflared..."
  # Pick the build for this machine's processor - many Chromebooks are ARM,
  # where the amd64 build this used to always download can't run at all.
  case "$(uname -m)" in
    x86_64|amd64) CF_ARCH=amd64 ;;
    aarch64|arm64) CF_ARCH=arm64 ;;
    armv7l|armv6l) CF_ARCH=arm ;;
    *) echo "!! No cloudflared build for this processor ($(uname -m)) - use --no-tunnel."; exit 1 ;;
  esac
  CLOUDFLARED_BIN="$WEBAPP_DIR/cloudflared"
  if ! "$CLOUDFLARED_BIN" --version >/dev/null 2>&1; then  # missing, or a wrong-architecture download
    curl -fLo "$CLOUDFLARED_BIN" "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-$CF_ARCH"
    chmod +x "$CLOUDFLARED_BIN"
  fi

  pkill -f "$CLOUDFLARED_BIN tunnel" 2>/dev/null || true  # an earlier run's tunnel, if still up
  TUNNEL_LOG=/tmp/ajandb_tunnel.log
  TUNNEL_PID=""
  trap 'echo; echo "==> Stopping sharing and the server..."; kill $TUNNEL_PID "$UVICORN_PID" 2>/dev/null || true' INT TERM EXIT
  if [ -f "$HOME/.cloudflared/config.yml" ] || [ -f "$HOME/.cloudflared/config.yaml" ]; then
    echo "    Note: found a cloudflared config in ~/.cloudflared/ (from an earlier Cloudflare setup)."
    echo "    If the tunnel fails, move it aside and run again:  mv ~/.cloudflared ~/.cloudflared.off"
  fi
  # Cloudflare hands out the public link *before* the tunnel is actually
  # connected, and that registration can still fail ("Unauthorized: Tunnel
  # not found" in a loop) - so wait for the "Registered tunnel connection"
  # line before showing the link, rather than sharing one that doesn't work.
  # Quick tunnels are best-effort; when one won't register, a fresh tunnel
  # over HTTP/2 (instead of the default QUIC) is the usual fix, so that's
  # tried once automatically.
  TUNNEL_URL=""
  for PROTOCOL in auto http2; do
    echo "==> Opening tunnel (protocol: $PROTOCOL)..."
    "$CLOUDFLARED_BIN" tunnel --protocol "$PROTOCOL" --url "http://localhost:$PORT" > "$TUNNEL_LOG" 2>&1 &
    TUNNEL_PID=$!
    for _ in $(seq 1 60); do
      if grep -q "Registered tunnel connection" "$TUNNEL_LOG"; then
        TUNNEL_URL="$(grep -o 'https://[a-z0-9-]*\.trycloudflare\.com' "$TUNNEL_LOG" | head -1 || true)"
        break
      fi
      if [ "$(grep -c 'Unauthorized\|failed to request quick Tunnel' "$TUNNEL_LOG" || true)" -ge 3 ]; then
        break  # retrying the same tunnel won't fix this; try a fresh one
      fi
      kill -0 "$TUNNEL_PID" 2>/dev/null || break
      sleep 1
    done
    [ -n "$TUNNEL_URL" ] && break
    kill "$TUNNEL_PID" 2>/dev/null || true
    echo "    The tunnel didn't connect. Last lines of its log:"
    tail -n 5 "$TUNNEL_LOG" | sed 's/^/      /'
  done
  if [ -z "$TUNNEL_URL" ]; then
    echo
    echo "!! Couldn't open a public link. The app itself is running fine at"
    echo "   http://localhost:$PORT/ - only the sharing part failed. Things to try:"
    echo "   1. Wait a few minutes and run deploy.sh again (Cloudflare's free quick"
    echo "      tunnels are best-effort and sometimes refuse new tunnels for a while)."
    echo "   2. If ~/.cloudflared/ exists, move it aside: mv ~/.cloudflared ~/.cloudflared.off"
    echo "   3. Check this machine's internet connection / any VPN or firewall."
    echo "   Full tunnel log: $TUNNEL_LOG"
    trap - INT TERM EXIT  # keep the local server running for the person at this machine
    exit 1
  fi

  echo
  echo "  ================================================================"
  echo "   Share with testers"
  echo "     Link:      $TUNNEL_URL"
  if [ -n "${AJANDB_PASSWORD:-}" ]; then
    echo "     Password:  $AJANDB_PASSWORD    (any username)"
  else
    echo "     Password:  NONE - anyone with the link can see and delete"
    echo "                everything. Re-run without --no-password to add one."
  fi
  if [ -n "${ANTHROPIC_API_KEY:-}" ] || [ -n "${OPENAI_API_KEY:-}" ]; then
    echo "   Note: the server has an API key set (webapp/.env), so testers'"
    echo "   uploads are billed to it."
  fi
  echo "   The link changes each time you run deploy.sh. Keep this window"
  echo "   open - closing it or pressing Ctrl+C stops sharing."
  echo "  ================================================================"
  echo
  wait "$TUNNEL_PID"
}

# Sets NAME=value in webapp/.env (replacing any earlier line for NAME), quoted
# so the `source .env` above reads it back exactly. Readable only by you.
save_env_var() {
  touch .env
  grep -v "^$1=" .env > .env.tmp || true
  printf '%s=%q\n' "$1" "$2" >> .env.tmp
  mv .env.tmp .env
  chmod 600 .env
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
