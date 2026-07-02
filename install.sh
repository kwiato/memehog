#!/usr/bin/env bash
# Memehog setup wizard — generates .env and starts the Docker stack.
set -euo pipefail

BOLD=$(tput bold 2>/dev/null || true)
RESET=$(tput sgr0 2>/dev/null || true)

echo "${BOLD}"
echo "  🐗 Memehog setup wizard"
echo "${RESET}"

cd "$(dirname "$0")"

# --- prerequisites -----------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "❌ Docker is not installed."
    echo "   Install it first: curl -fsSL https://get.docker.com | sh"
    exit 1
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "❌ The 'docker compose' plugin is missing (Docker is too old?)."
    exit 1
fi

if [ -f .env ]; then
    echo "⚠️  .env already exists."
    read -r -p "   Overwrite it and reconfigure? [y/N] " overwrite
    case "$overwrite" in
        [yY]*) ;;
        *) echo "Keeping existing .env — starting the stack."; docker compose up -d --build; exit 0 ;;
    esac
fi

# --- questions ---------------------------------------------------------------
echo
echo "1) Telegram bot token"
echo "   Create a bot with @BotFather on Telegram (/newbot) and paste its token."
echo "   Leave empty to run without the bot (web UI only)."
read -r -p "   BOT_TOKEN: " BOT_TOKEN

ALLOWED_TELEGRAM_IDS=""
if [ -n "$BOT_TOKEN" ]; then
    echo
    echo "2) Who may use the bot?"
    echo "   Message @userinfobot on Telegram to learn your numeric ID."
    read -r -p "   Allowed Telegram IDs (comma-separated): " ALLOWED_TELEGRAM_IDS
fi

echo
echo "3) Where should memes be stored? (created if missing)"
read -r -p "   Data directory [./data]: " HOST_DATA_DIR
HOST_DATA_DIR=${HOST_DATA_DIR:-./data}

echo
read -r -p "4) Web UI port [2137]: " PORT
PORT=${PORT:-2137}

# --- generate ----------------------------------------------------------------
if command -v openssl >/dev/null 2>&1; then
    API_TOKEN=$(openssl rand -hex 24)
else
    API_TOKEN=$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')
fi

mkdir -p "$HOST_DATA_DIR"

cat > .env <<EOF
BOT_TOKEN=$BOT_TOKEN
ALLOWED_TELEGRAM_IDS=$ALLOWED_TELEGRAM_IDS
API_TOKEN=$API_TOKEN
HOST=0.0.0.0
PORT=$PORT
HOST_DATA_DIR=$HOST_DATA_DIR
COOKIES_FILE=
SCAN_CRON=0 3 * * *
LOG_LEVEL=INFO
EOF
chmod 600 .env

echo
echo "✅ Wrote .env (API token: $API_TOKEN — you'll need it for the API/extension)"
echo
echo "🐳 Building and starting Memehog (first build takes a few minutes on a Pi)…"
docker compose up -d --build

echo
echo "${BOLD}🎉 Done!${RESET}"
echo "   Web UI:  http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):$PORT"
echo "   Logs:    docker compose logs -f"
echo "   Update:  git pull && docker compose up -d --build"
if [ -n "$BOT_TOKEN" ]; then
    echo "   Bot:     open Telegram and send your bot a meme link!"
fi
