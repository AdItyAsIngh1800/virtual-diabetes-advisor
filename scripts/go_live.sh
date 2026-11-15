#!/usr/bin/env bash
# Start the ngrok tunnel and the FastAPI server together, so a phone can open the
# patient app over https (Web Push and service workers require a secure origin).
#
# - Reuses the static ngrok domain in PUBLIC_BASE_URL (.env) if one is set; otherwise
#   starts a random tunnel and writes its URL into .env.
# - Writes logs to logs/ngrok.log and logs/uvicorn.log so they can be inspected later.
# - Ctrl+C stops both.
set -euo pipefail
cd "$(dirname "$0")/.."

command -v ngrok >/dev/null || { echo "ngrok is not installed. Run: brew install ngrok"; exit 1; }
ngrok config check >/dev/null 2>&1 || {
  echo "ngrok has no auth token. Copy yours from dashboard.ngrok.com and run:"
  echo "  ngrok config add-authtoken <token>"; exit 1; }

mkdir -p logs
# Stop leftovers from an earlier run so port 8000 and the tunnel are free.
pkill -f "ngrok http" 2>/dev/null || true
pkill -f "uvicorn app.main:app" 2>/dev/null || true
sleep 1

current_url=$(grep -E '^PUBLIC_BASE_URL=' .env | cut -d= -f2- || true)
domain=${current_url#https://}
domain=${domain%/}

if [[ "$domain" == *.ngrok-free.dev || "$domain" == *.ngrok-free.app || "$domain" == *.ngrok.app ]]; then
  echo "Using static ngrok domain: $domain"
  ngrok http --url="$domain" 8000 --log=stdout > logs/ngrok.log 2>&1 &
else
  ngrok http 8000 --log=stdout > logs/ngrok.log 2>&1 &
fi
ngrok_pid=$!
trap 'kill $ngrok_pid 2>/dev/null || true' EXIT

# Ask ngrok's local API for the public https URL (up to 20 s).
url=""
for _ in $(seq 1 20); do
  url=$(curl -s http://127.0.0.1:4040/api/tunnels | python3 -c \
    'import sys, json; t = json.load(sys.stdin)["tunnels"]; print(next((x["public_url"] for x in t if x["public_url"].startswith("https")), ""))' \
    2>/dev/null || true)
  [[ -n "$url" ]] && break
  sleep 1
done
[[ -n "$url" ]] || { echo "ngrok did not start. Last log lines:"; tail -5 logs/ngrok.log; exit 1; }

# Keep .env in sync (needed when the URL is random).
if grep -qE '^PUBLIC_BASE_URL=' .env; then
  sed -i '' "s|^PUBLIC_BASE_URL=.*|PUBLIC_BASE_URL=$url|" .env
else
  echo "PUBLIC_BASE_URL=$url" >> .env
fi
echo "$url/app/" > logs/app_url.txt

echo
echo "Tunnel:      $url"
echo "Patient app: $url/app/   (open this on your phone)"
echo "Starting API on port 8000 (Ctrl+C to stop both)..."
echo
uv run uvicorn app.main:app --port 8000 2>&1 | tee logs/uvicorn.log
