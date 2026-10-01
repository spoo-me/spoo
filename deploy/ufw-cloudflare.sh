#!/bin/bash
# Allowlist inbound 443 + 80 to Cloudflare IP ranges. Idempotent.
# Ports 80 + 443 both need it: 443 for customer HTTPS, 80 for the
# CF Worker → origin hop (worker can't do mTLS, see cloudflare-worker/).
#
# CF IP ranges: https://www.cloudflare.com/ips-{v4,v6}
#
# Docker-mapped ports bypass UFW's filter chain (DNAT happens in
# PREROUTING), so we ALSO mirror the rules into DOCKER-USER via
# /etc/ufw/after.rules. Step 5 below.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
	echo "must run as root" >&2
	exit 1
fi

# ── Step 1: fetch CF IP ranges (read-only, safe to fail) ───────────────
echo "[ufw-cloudflare] fetching CF IP ranges…"
v4=$(curl -fsS --connect-timeout 5 --max-time 20 https://www.cloudflare.com/ips-v4)
v6=$(curl -fsS --connect-timeout 5 --max-time 20 https://www.cloudflare.com/ips-v6)

if [[ -z "$v4" || -z "$v6" ]]; then
	echo "[ufw-cloudflare] empty CF IP list — refusing to proceed (UFW unchanged)" >&2
	exit 2
fi

# ── Step 2: drop prior 443/80 rules so the re-run rebuilds cleanly ─────
echo "[ufw-cloudflare] dropping existing 443 + 80 allow rules…"
mapfile -t rule_nums < <(
	ufw status numbered \
		| awk -F'[][]' '/^\[/ {
			# Match exact 443/tcp, 443/udp, 80/tcp tokens — naive 443|80
			# would also match 4430, 8080, etc.
			if ($0 ~ /(^|[^0-9])(443\/(tcp|udp)|80\/tcp)([^0-9]|$).*ALLOW IN/) print $2
		}' \
		| sort -rn
)
if (( ${#rule_nums[@]} > 0 )); then
	for n in "${rule_nums[@]}"; do
		[[ -z "$n" ]] && continue
		ufw --force delete "$n" >/dev/null
	done
fi

# ── Step 3: baseline policy ────────────────────────────────────────────
echo "[ufw-cloudflare] applying baseline rules…"
ufw default deny incoming >/dev/null
ufw default allow outgoing >/dev/null
ufw allow 22/tcp comment 'SSH' >/dev/null

# ── Step 4: CF allowlist (443 tcp+udp for HTTP/{2,3}, 80 tcp) ──────────
echo "[ufw-cloudflare] adding 443 + 80 allow rules for CF…"
while IFS= read -r ip; do
	[[ -z "$ip" ]] && continue
	ufw allow proto tcp from "$ip" to any port 443 comment 'Cloudflare-v4' >/dev/null
	ufw allow proto udp from "$ip" to any port 443 comment 'Cloudflare-v4' >/dev/null
	ufw allow proto tcp from "$ip" to any port 80 comment 'Cloudflare-v4' >/dev/null
done <<< "$v4"

while IFS= read -r ip; do
	[[ -z "$ip" ]] && continue
	ufw allow proto tcp from "$ip" to any port 443 comment 'Cloudflare-v6' >/dev/null
	ufw allow proto udp from "$ip" to any port 443 comment 'Cloudflare-v6' >/dev/null
	ufw allow proto tcp from "$ip" to any port 80 comment 'Cloudflare-v6' >/dev/null
done <<< "$v6"

# ── Step 5: mirror the allowlist into DOCKER-USER ──────────────────────
# Docker bypasses UFW filter; DOCKER-USER is the documented hook.
# Rules are scoped to dst = Caddy container IPs so they only affect
# inbound traffic, not outbound HTTPS from any container (which would
# otherwise also match a naked `--dport 443 DROP`).
# Caddy container IPs are pinned in docker-compose.prod.yml.

CADDY_IPV4=172.30.0.20
CADDY_IPV6=""   # spoonet is v4-only today; left empty so v6 block no-ops

BEGIN_MARKER='# BEGIN cf-docker-user (managed by ufw-cloudflare.sh)'
END_MARKER='# END cf-docker-user'

write_docker_user_block() {
	local rules_file="$1"
	local ip_list="$2"
	local dst_ip="$3"   # Caddy container IP for this family; "" skips

	if grep -qF "$BEGIN_MARKER" "$rules_file"; then
		echo "[ufw-cloudflare] removing prior managed block from $rules_file…"
		sed -i "/^$BEGIN_MARKER\$/,/^$END_MARKER\$/d" "$rules_file"
	fi

	# Skip the file if no container IP for this family.
	if [[ -z "$dst_ip" ]]; then
		echo "[ufw-cloudflare] no container IP for $rules_file family; skipping"
		return
	fi

	echo "[ufw-cloudflare] writing managed block to $rules_file…"
	{
		echo ""
		echo "$BEGIN_MARKER"
		echo "*filter"
		echo ":DOCKER-USER - [0:0]"
		while IFS= read -r ip; do
			[[ -z "$ip" ]] && continue
			echo "-A DOCKER-USER -p tcp -s $ip -d $dst_ip --dport 443 -j RETURN"
			echo "-A DOCKER-USER -p udp -s $ip -d $dst_ip --dport 443 -j RETURN"
			echo "-A DOCKER-USER -p tcp -s $ip -d $dst_ip --dport 80 -j RETURN"
		done <<< "$ip_list"
		echo "-A DOCKER-USER -p tcp -d $dst_ip --dport 443 -j DROP"
		echo "-A DOCKER-USER -p udp -d $dst_ip --dport 443 -j DROP"
		echo "-A DOCKER-USER -p tcp -d $dst_ip --dport 80 -j DROP"
		# Default Docker fall-through.
		echo "-A DOCKER-USER -j RETURN"
		echo "COMMIT"
		echo "$END_MARKER"
	} >> "$rules_file"
}

write_docker_user_block /etc/ufw/after.rules "$v4" "$CADDY_IPV4"
write_docker_user_block /etc/ufw/after6.rules "$v6" "$CADDY_IPV6"

# ── Step 6: enable + reload ────────────────────────────────────────────
echo "[ufw-cloudflare] enabling UFW…"
ufw --force enable >/dev/null
ufw reload >/dev/null

echo "[ufw-cloudflare] done. UFW rules:"
ufw status | head -60
echo
echo "[ufw-cloudflare] DOCKER-USER chain (live, IPv4):"
iptables -L DOCKER-USER -n --line-numbers | head -20
