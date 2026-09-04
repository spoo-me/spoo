#!/usr/bin/env bash
# Deny private-range egress from the probe browser's docker network.
#
# The probe checks a page's hosts against public addresses before the
# request, but Chromium resolves again to connect, so a DNS-rebinding page
# (public answer for the check, private answer for the connect) could reach
# whatever the container's network can. Its own network already hides the
# other services; this rule closes the host and any other bridge too.
#
# Usage (as root, on the box running the compose):
#   infrastructure/probenet-egress.sh <probenet subnet, e.g. 172.31.0.0/24>
set -euo pipefail

subnet="${1:?probenet subnet required, e.g. 172.31.0.0/24}"

for dst in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 169.254.0.0/16 100.64.0.0/10 127.0.0.0/8; do
  # Idempotent: insert only when the identical rule is not already present.
  if ! iptables -C DOCKER-USER -s "$subnet" -d "$dst" -j DROP 2>/dev/null; then
    iptables -I DOCKER-USER -s "$subnet" -d "$dst" -j DROP
  fi
done
# The worker talks to the browser inside this subnet, which the private
# ranges above cover; let that traffic through before the drops.
if ! iptables -C DOCKER-USER -s "$subnet" -d "$subnet" -j RETURN 2>/dev/null; then
  iptables -I DOCKER-USER 1 -s "$subnet" -d "$subnet" -j RETURN
fi
iptables -S DOCKER-USER | grep -- "-s ${subnet%/*}"
