#!/bin/sh
# xccode-learn — nightly Hermes learner (XC-CODE-001 §4.9, §6).
#
# Headless only: runs as the `xccode` account from `xccode-learn.timer`, never interactively.
# Two phases each run: (1) promote anything the operator approved since the last run, and
# (2) if — and only if — there are new digests, the Hermes learner run. An empty night with no
# approved items and no digests costs zero tokens.
# Hermes runs with only the `memory` and `skills` toolsets (no terminal/browser/web); the model
# provider is xcroute, set in ~xccode/.hermes/config.yaml (§4.9). Its proposals are parsed and
# stored by `xccode store-learn` — facts/preferences/pitfalls to OpenViking (trust: observed),
# rules and skill drafts to the review queue (§6.5).
set -eu

STATE="/var/lib/xcloud/xccode"
HERMES="/opt/xcloud/xccode/hermes/.venv/bin/hermes"
XCC="/opt/xcloud/xccode/venv/bin/xccode"
PENDING="$STATE/learn/pending"
APPROVED="$STATE/learn/approved"

# --- phase 1: promote operator-approved review items (§6.6) ----------------------------------
# An approved rule enters the rules/ tier in OpenViking (every ctx_brief includes it); an
# approved skill installs as a native OpenCode skill under skills/approved/. Runs even with no
# new digests, so an approval never waits on new work.
if [ -d "$APPROVED" ] && [ -n "$(find "$APPROVED" -type f -name '*.json' -print -quit 2>/dev/null)" ]; then
    mkdir -p "$STATE/learn/processed"
    find "$APPROVED" -type f -name '*.json' -print | sort | while IFS= read -r f; do
        kind="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("kind",""))' "$f" 2>/dev/null || true)"
        case "$kind" in
            rule)  "$XCC" store-learn --promote-rule "$f" || true ;;
            skill) "$XCC" store-learn --promote-skill "$f" || true ;;
        esac
        mv "$f" "$STATE/learn/processed/"
    done
    echo "xccode-learn: promoted approved items"
fi

# --- phase 2: the Hermes learner over new digests --------------------------------------------
if [ ! -d "$PENDING" ] || [ -z "$(find "$PENDING" -type f -name '*.json' -print -quit 2>/dev/null)" ]; then
    echo "xccode-learn: no new digests, skipping"
    exit 0
fi

# Aggregate up to 20 pending digests into a bounded prompt. This exact list (and ONLY this list)
# is consumed on success below — a digest that arrives mid-run is left pending for next run and is
# never discarded unlearned (advisor O13). verifier-lite (in xcroute's /learn) gates every write
# Hermes proposes, so the learner never writes memory directly.
files="$(find "$PENDING" -type f -name '*.json' -print | sort | head -n 20)"
digest="$(printf '%s\n' "$files" | xargs -r cat | head -c 8000)"
if [ -z "$digest" ]; then
    echo "xccode-learn: digest content empty, skipping"
    exit 0
fi

# The repo the digests belong to (each digest carries a `repo` field); "first" is the first of
# the aggregated list, not a fresh find.
first="$(printf '%s\n' "$files" | head -1)"
repo="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("repo","default"))' "$first" 2>/dev/null || true)"
[ -n "$repo" ] || repo="default"

echo "xccode-learn: running Hermes learner over $(printf '%s' "$digest" | wc -c) bytes (repo $repo)"
# --query-file - reads the prompt from stdin, so the digest text is never shell-interpreted.
# Hermes must return only a JSON object; its reply is piped to store-learn.
if printf '%s\n' "Review these finished-session digests and propose facts, preferences, pitfalls, and dated rules (no terminal/browser/web). Return ONLY a JSON object with exactly these keys: \"facts\" (list of {\"path\": str, \"content\": str}), \"preferences\" (list of {\"content\": str}), \"pitfalls\" (list of {\"path\": str, \"content\": str}), \"rules\" (list of {\"content\": str}), \"skills\" (list of {\"name\": str, \"content\": str}). No prose, no markdown fences. Digests: $digest" \
    | "$HERMES" chat -t memory,skills --oneshot --query-file - --quiet \
    | "$XCC" store-learn "$repo"; then
    # Consume exactly the digests this run aggregated (captured above in $files), so the next run
    # does not re-learn them and a digest can never be consumed unlearned.
    mkdir -p "$STATE/learn/processed"
    printf '%s\n' "$files" | while IFS= read -r f; do [ -n "$f" ] && mv "$f" "$STATE/learn/processed/"; done
fi
