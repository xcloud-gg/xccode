#!/bin/sh
# install.sh — install xccode on a fresh host (XC-DES-001 §6.11, B-13, B-50).
#
# Run as root, never piped into a shell:
#     sudo ./install.sh --operator marius [--release <tag>] [--restore <restic repo>] [--check]
#
# Idempotent: every step records that it is done and re-runs as a no-op. `--check` is a dry run
# that prints exactly what would change and touches nothing. The only network access is the signed
# release download and verification (release host + providers, B-50).
set -eu

# --- paths (overridable for tests) ---
ETC="${XCCODE_ETC:-/etc/xcloud/xccode}"
STATE="${XCCODE_STATE:-/var/lib/xcloud/xccode}"
OPT="${XCCODE_OPT:-/opt/xcloud/xccode}"
SYSTEMD="${XCCODE_SYSTEMD:-/etc/systemd/system}"
RELEASE="${XCCODE_RELEASE:-}"

usage() {
    cat <<EOF
usage: install.sh --operator <login> [--release <tag>] [--restore <restic repo>] [--check]

  --operator <login>   desktop user who gets the xcc launcher and OpenCode profile (required)
  --release <tag>      signed release tag to install (never main, never latest); default from versions.lock
  --restore <repo>     restic repository to restore memory/skills/rules from after install
  --check              dry run: print every change, change nothing
EOF
}

OPERATOR=""
RESTORE=""
CHECK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --operator) OPERATOR="$2"; shift 2 ;;
        --release) RELEASE="$2"; shift 2 ;;
        --restore) RESTORE="$2"; shift 2 ;;
        --check) CHECK=1; shift ;;
        -h|--help) usage; exit 0 ;;
        *) echo "install.sh: unknown argument: $1" >&2; usage >&2; exit 2 ;;
    esac
done
[ -n "$OPERATOR" ] || { echo "install.sh: --operator <login> is required" >&2; usage >&2; exit 2; }
if [ "$CHECK" != 1 ] && [ "$(id -u)" != 0 ]; then
    echo "install.sh: must run as root (or use --check for a dry run)" >&2
    exit 1
fi

# --- dry-run / apply helpers ---------------------------------------------------
run() {  # run "<what>" <cmd...> — print in --check, otherwise execute
    desc="$1"; shift
    if [ "$CHECK" = 1 ]; then
        echo "would: $desc"
        return 0
    fi
    echo ">> $desc"
    "$@"
}

ensure_dir() {  # ensure_dir <path> <owner> <mode> — idempotent mkdir + chown/chmod
    path="$1" owner="$2" mode="$3"
    if [ -d "$path" ]; then
        echo "already: dir $path"
    else
        run "mkdir -p $path" mkdir -p "$path"
    fi
    [ "$CHECK" = 1 ] || chown "$owner" "$path"
    [ "$CHECK" = 1 ] || chmod "$mode" "$path"
}

write_file() {  # write_file "<path>" <<'EOF' ... EOF — idempotent file write from stdin
    path="$1"
    if [ -f "$path" ]; then
        echo "already: $path"
        cat >/dev/null
        return 0
    fi
    if [ "$CHECK" = 1 ]; then
        echo "would: write $path"
        cat >/dev/null
        return 0
    fi
    echo ">> write $path"
    cat > "$path"
}

# --- 1. account, group, subuid range, slice ------------------------------------
step_account() {
    if getent passwd xccode >/dev/null 2>&1; then
        echo "already: account xccode"
    else
        run "useradd xccode (system, nologin)" \
            useradd --system --no-create-home --shell /usr/sbin/nologin --uid 997 xccode
    fi
    if getent group xccode-users >/dev/null 2>&1; then
        echo "already: group xccode-users"
    else
        run "groupadd xccode-users" groupadd xccode-users
    fi
    if getent group xccode-users | cut -d: -f4 | tr ',' '\n' | grep -qx "$OPERATOR"; then
        echo "already: $OPERATOR in xccode-users"
    else
        run "add $OPERATOR to xccode-users" usermod -aG xccode-users "$OPERATOR"
    fi
    if grep -q '^xccode:' /etc/subuid 2>/dev/null; then
        echo "already: subuid range for xccode"
    else
        run "usermod --add-subuids 589824-655359 xccode" usermod --add-subuids 589824-655359 xccode
        run "usermod --add-subgids 589824-655359 xccode" usermod --add-subgids 589824-655359 xccode
    fi
    # The @xccode btrfs subvolume is laid out by the operator's filesystem step (L0); here we
    # only make sure its mount point exists (B-13 runs on a host where L0 is already done).
    ensure_dir "$STATE" "xccode:xccode" 0750
    write_file "$SYSTEMD/xccode.slice" <<'EOF'
[Slice]
MemoryHigh=3G
MemoryMax=4G
CPUWeight=50
ManagedOOMMemoryPressure=kill
EOF
}

# --- 2. packages (Debian main only); gitleaks from the signed release (step 4) --
step_packages() {
    for pkg in bubblewrap restic auditd uv; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            echo "already: package $pkg"
        else
            run "apt-get install -y $pkg" apt-get install -y "$pkg"
        fi
    done
    # gitleaks is installed from its pinned release binary, not a Debian package (§6.11).
    # Done in the signed-release fetch step (a later increment).
}

# --- 3. /opt/xcloud/xccode and /etc/xcloud/xccode (root-owned) ------------------
step_opt() {
    ensure_dir "$OPT" "root:root" 0755
    for sub in opencode venv hermes oac opencode-plugins; do
        ensure_dir "$OPT/$sub" "root:root" 0755
    done
    ensure_dir "$ETC" "root:root" 0755
    ensure_dir "$ETC/opencode" "root:root" 0755
    write_file "$ETC/opencode/opencode.json" <<'EOF'
{
  "share": "disabled",
  "autoupdate": false
}
EOF
}

main() {
    echo "install.sh --operator $OPERATOR${RELEASE:+ --release $RELEASE}${RESTORE:+ --restore $RESTORE}${CHECK:+ --check}"
    step_account
    step_packages
    step_opt
    # Upcoming increments: signed-release fetch + verify, services, marius profile, timers,
    # guardrails, --restore.
    if [ "$CHECK" = 1 ]; then
        echo "check: dry run complete; nothing was changed"
    else
        echo "install complete; run: xccode doctor"
    fi
}

main
