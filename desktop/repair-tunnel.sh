#!/bin/zsh
set -euo pipefail

LABEL="com.sangshuai.ai-radar-tunnel"
DOMAIN="gui/$(id -u)"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

if [[ ! -f "$PLIST" ]]; then
  print -u2 "AI Radar tunnel configuration is missing: $PLIST"
  exit 1
fi

plutil -lint "$PLIST" >/dev/null
launchctl enable "$DOMAIN/$LABEL"
launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true

# Allow an ad-hoc tunnel used during repair to release the local port first.
for _ in {1..20}; do
  if ! /usr/sbin/lsof -nP -iTCP:8765 -sTCP:LISTEN >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done

launchctl bootstrap "$DOMAIN" "$PLIST"
launchctl kickstart -k "$DOMAIN/$LABEL"

for _ in {1..20}; do
  if curl -fsS --max-time 2 http://127.0.0.1:8765/api/summary >/dev/null 2>&1; then
    print "AI Radar tunnel is connected and managed by launchd."
    exit 0
  fi
  sleep 0.5
done

print -u2 "AI Radar tunnel was registered but did not become healthy."
exit 1
