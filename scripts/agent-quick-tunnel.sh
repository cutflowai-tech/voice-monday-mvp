#!/usr/bin/env bash
# Start (or restart) the no-account Cloudflare quick tunnel, then point the ElevenLabs
# agent's tools at its new https URL. Run from the project directory on the server:
#
#   scripts/agent-quick-tunnel.sh
#
# Quick tunnels get a new random URL on every restart, so run this again after any
# restart of the cloudflared-quick container. A named tunnel (profile "agent") avoids that.
set -euo pipefail
cd "$(dirname "$0")/.."
compose() { sudo docker compose "$@"; }

compose --profile agent-quick up -d --force-recreate cloudflared-quick >/dev/null
url=""
for _ in $(seq 1 30); do
  url="$(compose logs --no-log-prefix cloudflared-quick 2>&1 \
    | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | tail -1 || true)"
  [[ -n "$url" ]] && break
  sleep 2
done
[[ -n "$url" ]] || { echo "no trycloudflare URL in cloudflared logs" >&2; exit 1; }

if grep -q '^AGENT_PUBLIC_URL=' .env; then
  sed -i "s|^AGENT_PUBLIC_URL=.*|AGENT_PUBLIC_URL=$url|" .env
else
  echo "AGENT_PUBLIC_URL=$url" >>.env
fi
echo "tunnel: $url"

# The API refuses everything but /agent/tools/* through Cloudflare, and needs the token.
for _ in $(seq 1 20); do
  code="$(curl -s -o /dev/null -w '%{http_code}' -m 10 "$url/health" || true)"
  [[ "$code" == "404" ]] && break
  sleep 3
done
echo "public /health -> $code (expected 404)"
echo "public /agent/tools/cancel_last without token -> $(curl -s -o /dev/null -w '%{http_code}' \
  -m 10 -X POST -H 'Content-Type: application/json' -d '{"conversation_id":"probe"}' \
  "$url/agent/tools/cancel_last") (expected 401)"

compose run --rm --no-deps api el-agent sync
