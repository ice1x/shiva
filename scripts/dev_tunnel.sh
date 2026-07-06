#!/usr/bin/env bash
#
# dev_tunnel.sh — expose the local n8n to GitHub for end-to-end testing.
#
# GitHub can only deliver a `pull_request` webhook to a PUBLIC URL, but n8n runs
# on localhost. This script opens a Cloudflare quick tunnel to localhost:5678,
# solves the chicken-and-egg between the (random) tunnel URL and n8n's
# WEBHOOK_URL — which n8n only reads at startup — by:
#
#   1. starting the tunnel and waiting for its https://<random>.trycloudflare.com URL
#   2. writing that URL into .env as WEBHOOK_URL
#   3. (re)starting n8n via docker compose so it picks the URL up
#   4. printing the exact Payload URL to paste into each repo's GitHub webhook
#
# The tunnel runs in the FOREGROUND: closing it (Ctrl-C) tears the public URL
# down, so nothing external can reach n8n once you stop testing. This is a
# manual, operator-run dev helper — deliberately NOT part of CI: a quick tunnel
# URL is random and single-use, so there is nothing deterministic to assert, and
# exposing a CI runner publicly would be a hole.
#
# Usage:  ./scripts/dev_tunnel.sh
# Deps:   cloudflared (brew install cloudflared), docker compose
set -euo pipefail

cd "$(dirname "$0")/.."

WEBHOOK_PATH="pr-review"   # matches the Webhook node path in workflows/pr_review.json
LOCAL_PORT="${N8N_PORT:-5678}"

command -v cloudflared >/dev/null 2>&1 || {
  echo "error: cloudflared not found. Install it with: brew install cloudflared" >&2
  exit 1
}
command -v docker >/dev/null 2>&1 || {
  echo "error: docker not found." >&2
  exit 1
}

# Ensure a .env exists so docker compose has somewhere to persist WEBHOOK_URL.
[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example"; }

# Safety preflight: never expose an unclaimed n8n. Until the owner account is
# created, ANYONE who reaches the tunnel URL can complete the setup wizard and
# take the instance over. n8n reports this via /rest/settings
# (userManagement.showSetupOnFirstLoad == true → no owner yet).
if curl -sf --max-time 5 "http://localhost:${LOCAL_PORT}/rest/settings" 2>/dev/null \
     | grep -q '"showSetupOnFirstLoad":true'; then
  echo "error: n8n has no owner account yet — refusing to open a public tunnel." >&2
  echo "       Anyone reaching the URL could claim your instance. Create the owner" >&2
  echo "       account first at http://localhost:${LOCAL_PORT} (strong password)," >&2
  echo "       then re-run this script." >&2
  exit 1
fi

log="$(mktemp -t shiva-cloudflared.XXXXXX)"
cleanup() {
  [ -n "${cf_pid:-}" ] && kill "$cf_pid" 2>/dev/null || true
  rm -f "$log"
  echo ""
  echo "tunnel stopped — the public URL is now dead."
}
trap cleanup EXIT INT TERM

echo "starting Cloudflare quick tunnel to http://localhost:${LOCAL_PORT} ..."
cloudflared tunnel --url "http://localhost:${LOCAL_PORT}" >"$log" 2>&1 &
cf_pid=$!

# Wait (up to ~30s) for cloudflared to print its public URL.
url=""
for _ in $(seq 1 30); do
  url="$(grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' "$log" | head -1 || true)"
  [ -n "$url" ] && break
  # If cloudflared died early, surface its log and bail.
  kill -0 "$cf_pid" 2>/dev/null || { echo "cloudflared exited early:" >&2; cat "$log" >&2; exit 1; }
  sleep 1
done
[ -n "$url" ] || { echo "error: timed out waiting for a tunnel URL. Log:" >&2; cat "$log" >&2; exit 1; }

# Persist WEBHOOK_URL into .env (replace an existing line or append one).
if grep -qE '^WEBHOOK_URL=' .env; then
  # portable in-place edit (macOS/BSD sed needs the empty '' backup arg)
  sed -i.bak -E "s#^WEBHOOK_URL=.*#WEBHOOK_URL=${url}/#" .env && rm -f .env.bak
else
  printf '\nWEBHOOK_URL=%s/\n' "$url" >>.env
fi

echo "tunnel up: ${url}"
echo "restarting n8n so it reads the new WEBHOOK_URL ..."
docker compose up -d >/dev/null

cat <<EOF

────────────────────────────────────────────────────────────────────────
  Public tunnel:   ${url}
  n8n editor:      ${url}   (and http://localhost:${LOCAL_PORT})

  GitHub webhook Payload URL (same for every repo):

      ${url}/webhook/${WEBHOOK_PATH}

  Add it in each repo:  Settings → Webhooks → Add webhook
      Content type:     application/json
      Events:           Pull requests only

  Leave this running while you test. Press Ctrl-C to tear the tunnel down.
────────────────────────────────────────────────────────────────────────
EOF

# Keep the tunnel in the foreground; Ctrl-C triggers the cleanup trap.
wait "$cf_pid"
