#!/bin/sh
# xccode-learn — nightly Hermes learner (XC-CODE-001 §4.9).
#
# Headless only: runs as the `xccode` account from `xccode-learn.timer`, never interactively.
# The pre-check skips the run when there are no new digests, so an empty night costs nothing.
# Hermes's restricted `learner` profile (memory + skill_manage + read digests only — no
# terminal/browser/web) is enforced by its toolset; the model provider is xcroute (§4.9).
set -eu

STATE="/var/lib/xcloud/xccode"
HERMES="/opt/xcloud/xccode/hermes/.venv/bin/hermes"
PENDING="$STATE/learn/pending"

# Pre-check: no new digests -> nothing to learn, exit clean.
if [ ! -d "$PENDING" ] || [ -z "$(find "$PENDING" -type f -name '*.json' -print -quit 2>/dev/null)" ]; then
    echo "xccode-learn: no new digests, skipping"
    exit 0
fi

# Aggregate the pending digests into a bounded prompt. verifier-lite (in xcroute's /learn) gates
# every write Hermes proposes, so the learner never writes memory directly.
digest="$(find "$PENDING" -type f -name '*.json' -print | sort | head -n 20 | xargs -r cat | head -c 8000)"
if [ -z "$digest" ]; then
    echo "xccode-learn: digest content empty, skipping"
    exit 0
fi

echo "xccode-learn: running Hermes learner over $(echo "$digest" | wc -c) bytes of digests"
exec "$HERMES" chat --profile learner --non-interactive \
    "Review these finished-session digests and propose facts, preferences, pitfalls, and dated rules (no terminal/browser/web). Digests: $digest"
