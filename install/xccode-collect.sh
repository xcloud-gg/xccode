#!/bin/sh
# xccode-collect — collect finished OpenCode sessions and plan/review notes into learning digests
# (XC-CODE-001 §6.1). Runs as marius from the hourly `xccode-collect.timer` (user unit).
#
# Reads OpenCode's session directory and ~/.local/share/xccode/thoughts/<repo>/ (OAC plan/review
# notes), runs Guard-lite over every field, spools the exact redacted payload to
# ~/.local/share/xccode/collect/ for audit, and posts the digests to xcroute's /learn door with the
# operator's token (XCC_TOKEN, injected by the unit). Without a token it still spools, so nothing is
# ever silently lost. Idempotent: the spool is content-addressed by digest id.
set -eu

XCC="$HOME/.local/bin/xcc"
if [ ! -x "$XCC" ]; then
    echo "xccode-collect: $XCC not installed; skipping"
    exit 0
fi

exec /opt/xcloud/xccode/venv/bin/xccode collect "$@"
