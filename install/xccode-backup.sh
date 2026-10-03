#!/bin/sh
# xccode-backup.sh — daily restic backup of xccode state (XC-CODE-001 §11).
# The repository comes from /etc/xcloud/xccode/backup.toml (filled by the operator via SOPS).
# No-op when the repository is unset, so the timer is safe before it is configured.
set -eu

cfg="${XCCODE_ETC:-/etc/xcloud/xccode}/backup.toml"
state="${XCCODE_STATE:-/var/lib/xcloud/xccode}"
restic_bin="${RESTIC:-restic}"

if [ ! -f "$cfg" ]; then
    echo "xccode-backup: no $cfg; skipping"
    exit 0
fi
repo=$(python3 -c "import tomllib,sys; print(tomllib.load(open('$cfg','rb')).get('repository','') or '')" 2>/dev/null || true)
if [ -z "$repo" ]; then
    echo "xccode-backup: repository not set; skipping"
    exit 0
fi
exec "$restic_bin" -r "$repo" backup "$state" --host xccode
