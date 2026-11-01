#!/usr/bin/env bash
# Run by the deployment agent on an approved free Ubuntu VM, never on the user's computer.
set -euo pipefail
umask 077
if [ "$(id -u)" -ne 0 ]; then echo 'Run as root on the approved pilot VM' >&2; exit 1; fi
if [ "$#" -ne 2 ]; then echo 'Usage: install_vm.sh DNS_HOSTNAME /srv/ksu/releases/COMMIT' >&2; exit 1; fi
pilot_hostname=$1
release_dir=$(realpath -- "$2")
case "$release_dir" in /srv/ksu/releases/*) ;; *) echo 'Release must be under /srv/ksu/releases/' >&2; exit 1;; esac
[ -f "$release_dir/server/app.py" ] && [ -f "$release_dir/deploy/Caddyfile" ]
for private_path in .git .env var demo-accounts.txt; do
    [ ! -e "$release_dir/$private_path" ] || { echo 'Release contains local/private files; use git archive' >&2; exit 1; }
done
. /etc/os-release
[ "$ID" = ubuntu ] && [ "$VERSION_ID" = 24.04 ] || { echo 'Ubuntu 24.04 is required' >&2; exit 1; }
mountpoint -q /srv/ksu-data || { echo 'STOP: dedicated persistent data volume is not mounted' >&2; exit 1; }
pilot_filesystem=$(findmnt -n -o FSTYPE --target /srv/ksu-data)
case "$pilot_filesystem" in ext4|xfs) ;; *) echo 'Use a local ext4/xfs block volume for SQLite' >&2; exit 1;; esac
python3 "$release_dir/deploy/render_config.py" "$pilot_hostname" /etc/ksu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y python3-venv fonts-dejavu-core ca-certificates curl iptables-persistent
id ksu >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin ksu
id caddy >/dev/null 2>&1 || useradd --system --home-dir /var/lib/caddy --no-create-home --shell /usr/sbin/nologin caddy
install -d -m 0700 -o ksu -g ksu /srv/ksu-data/backups
chown ksu:ksu /srv/ksu-data
chmod 0700 /srv/ksu-data
chown root:ksu /etc/ksu/ksu.env
chown root:caddy /etc/ksu/caddy.env
chmod 0755 /etc/ksu
install -d -m 0700 -o caddy -g caddy /var/lib/caddy
install -d -m 0755 /etc/caddy
install -d -m 0755 -o root -g root /srv/ksu /srv/ksu/releases
python3 -m venv "$release_dir/.venv"
"$release_dir/.venv/bin/python" -m pip install -r "$release_dir/requirements.txt"
"$release_dir/.venv/bin/python" -m pip check
# Release files are public source/dependencies; runtime secrets are on the separate data volume.
chown -R root:root "$release_dir"
chmod -R a+rX "$release_dir"
if [ ! -e /usr/local/bin/caddy ]; then
    python3 "$release_dir/deploy/get_caddy.py" --output /usr/local/bin/caddy
fi
/usr/local/bin/caddy version | grep -q '^v2\.11\.7 '
install -m 0644 "$release_dir/deploy/Caddyfile" /etc/caddy/Caddyfile
KSU_HOSTNAME="$pilot_hostname" /usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
if [ ! -e /srv/ksu-data/ksu.sqlite3 ]; then
    ( cd "$release_dir"; runuser -u ksu -- .venv/bin/python -m server init-demo --db /srv/ksu-data/ksu.sqlite3 --generate )
else
    ( cd "$release_dir"; runuser -u ksu -- .venv/bin/python -m server backup "/srv/ksu-data/backups/pre-release-$(date -u +%Y%m%dT%H%M%S).sqlite3" --db /srv/ksu-data/ksu.sqlite3 )
fi
# Never delete data, and never copy credentials into the release/check-out.
python3 - "$release_dir" <<'PY'
import os,sys
from pathlib import Path
link=Path('/srv/ksu/current')
if link.exists() and not link.is_symlink():raise SystemExit('Current release path is not a symlink; refusing replacement')
next_link=link.with_name('current.next')
next_link.symlink_to(sys.argv[1]);os.replace(next_link,link)
PY
for unit in ksu.service ksu-backup.service ksu-backup.timer; do
    sed 's|/srv/ksu/\.venv|/srv/ksu/current/.venv|g; s|WorkingDirectory=/srv/ksu$|WorkingDirectory=/srv/ksu/current|' "$release_dir/deploy/$unit" > "/etc/systemd/system/$unit"
    chmod 0644 "/etc/systemd/system/$unit"
done
install -m 0644 "$release_dir/deploy/caddy.service" /etc/systemd/system/caddy.service
systemd-analyze verify /etc/systemd/system/ksu.service /etc/systemd/system/ksu-backup.service /etc/systemd/system/ksu-backup.timer /etc/systemd/system/caddy.service
# Preserve OCI's existing firewall/metadata rules. Only add the two required web ports.
for pilot_port in 80 443; do
    iptables -C INPUT -p tcp --dport "$pilot_port" -j ACCEPT 2>/dev/null || iptables -I INPUT 1 -p tcp --dport "$pilot_port" -j ACCEPT
done
netfilter-persistent save
systemctl daemon-reload
systemctl enable ksu.service caddy.service ksu-backup.timer
systemctl restart ksu.service caddy.service
systemctl start ksu-backup.service ksu-backup.timer
curl --fail --silent --show-error --retry 10 --retry-connrefused --retry-delay 2 --max-time 5 "https://$pilot_hostname/api/health"
echo "HTTPS pilot health verified: https://$pilot_hostname"
echo 'Account credentials are in /srv/ksu-data/demo-accounts.txt, readable only by the server account/root.'
