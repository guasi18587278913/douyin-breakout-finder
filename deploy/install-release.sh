#!/usr/bin/env bash
set -euo pipefail
umask 077
release=${1:?Provide the absolute reviewed release directory}
[[ "$release" =~ ^/opt/douyin-breakout/releases/[a-f0-9]{40}$ ]] || exit 2
[[ -f "$release/requirements-web.txt" && -f "$release/webapp/app.py" ]] || exit 2
[[ "$(id -u)" == 0 ]] || exit 2
if id breakout >/dev/null 2>&1; then
    entry=$(getent passwd breakout)
    [[ "$(cut -d: -f6 <<< "$entry")" == /var/lib/douyin-breakout && "$(cut -d: -f7 <<< "$entry")" == /usr/sbin/nologin ]] || { echo 'Existing breakout account is not this dedicated service account.' >&2;exit 2; }
else
    useradd --system --home-dir /var/lib/douyin-breakout --create-home --shell /usr/sbin/nologin breakout
fi
install -d -o breakout -g breakout -m 700 /var/lib/douyin-breakout
# Each immutable release owns its dependencies. Switching back restores both code and runtime.
if [[ ! -d "$release/.venv" ]]; then python3 -m venv "$release/.venv";fi
"$release/.venv/bin/pip" install --disable-pip-version-check -q -r "$release/requirements-web.txt"
chmod -R a+rX "$release"
if [[ ! -f /etc/douyin-breakout.env ]]; then
    "$release/.venv/bin/python" - <<'PY'
import os
from cryptography.fernet import Fernet
fd=os.open('/etc/douyin-breakout.env',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
with os.fdopen(fd,'w') as f:
    f.write('BREAKOUT_MASTER_KEY='+Fernet.generate_key().decode()+'\n')
    f.write('BREAKOUT_PUBLIC_URL=https://breakout.zmtgzt.com\nBREAKOUT_DATA_DIR=/var/lib/douyin-breakout\nBREAKOUT_DAILY_LIVE_JOBS=2\nBREAKOUT_DAILY_USER_JOBS=2\n')
    f.flush();os.fsync(f.fileno())
PY
fi
chmod 600 /etc/douyin-breakout.env
previous=''
if [[ -L /opt/douyin-breakout/current && -d /opt/douyin-breakout/current ]]; then
    previous=$(readlink -f /opt/douyin-breakout/current)
    [[ "$previous" =~ ^/opt/douyin-breakout/releases/[a-f0-9]{40}$ ]] || exit 2
elif [[ -e /opt/douyin-breakout/current && ! -L /opt/douyin-breakout/current ]]; then
    echo 'Unexpected existing current directory; left untouched.' >&2;exit 2
fi
unit=/etc/systemd/system/douyin-breakout.service
site=/etc/caddy/sites/breakout.caddy
backup=$(mktemp -d)
unit_existed=0;site_existed=0;site_changed=0
if [[ -f "$unit" ]]; then cp "$unit" "$backup/unit";unit_existed=1;fi
if [[ -f "$site" ]]; then cp "$site" "$backup/site";site_existed=1;fi
rollback() {
    rc=$?
    trap - ERR
    set +e
    if [[ "$unit_existed" == 1 ]]; then cp "$backup/unit" "$unit";else systemctl disable douyin-breakout >/dev/null 2>&1;rm -f "$unit";fi
    if [[ "$site_changed" == 1 ]]; then
        if [[ "$site_existed" == 1 ]]; then cp "$backup/site" "$site";else rm -f "$site";fi
        caddy validate --config /etc/caddy/Caddyfile >/dev/null 2>&1 && systemctl reload caddy
    fi
    systemctl daemon-reload
    if [[ -n "$previous" ]]; then
        ln -sfn "$previous" /opt/douyin-breakout/current
        systemctl restart douyin-breakout
    else
        systemctl stop douyin-breakout
        [[ -L /opt/douyin-breakout/current ]] && rm /opt/douyin-breakout/current
    fi
    rm -rf "$backup"
    echo 'Release failed; this project configuration was restored. Data and credentials were preserved.' >&2
    exit "$rc"
}
trap rollback ERR
install -m 644 "$release/deploy/douyin-breakout.service" "$unit"
ln -sfn "$release" /opt/douyin-breakout/current
systemctl daemon-reload
systemctl enable douyin-breakout >/dev/null
systemctl restart douyin-breakout
healthy=0
for i in $(seq 1 20); do
    if curl -fsS -H 'Host: breakout.zmtgzt.com' http://127.0.0.1:8795/health >/dev/null;then healthy=1;break;fi
    sleep 1
done
[[ "$healthy" == 1 ]]
site_changed=1
install -m 644 "$release/deploy/breakout.caddy" "$site"
caddy validate --config /etc/caddy/Caddyfile >/dev/null
systemctl reload caddy
trap - ERR
rm -rf "$backup"
printf 'Release ready: %s\n' "${release##*/}"
