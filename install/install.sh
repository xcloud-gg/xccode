#!/bin/sh
# install.sh — install xccode on a fresh host (XC-CODE-001 §11, B-13, B-50).
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
CHECK=""
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

copy_file() {  # copy_file <src> <dst> [mode] — idempotent install of a tracked asset
    src="$1" dst="$2" mode="${3:-0644}"
    if [ -f "$dst" ]; then
        echo "already: $dst"
        return 0
    fi
    if [ "$CHECK" = 1 ]; then
        echo "would: install $dst"
        return 0
    fi
    echo ">> install $dst"
    install -m "$mode" "$src" "$dst"
}

# --- 1. account, group, subuid range, slice ------------------------------------
step_account() {
    if getent passwd xccode >/dev/null 2>&1; then
        echo "already: account xccode"
    else
        run "useradd xccode (system, nologin)" \
            useradd --system --no-create-home --shell /usr/sbin/nologin xccode
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
    for pkg in bubblewrap restic auditd python3-venv gpgv; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            echo "already: package $pkg"
        else
            run "apt-get install -y $pkg" apt-get install -y "$pkg"
        fi
    done
    # `uv` is not a Debian package: it is installed into the venv from the signed release, and
    # gitleaks from its pinned release binary (§11). Both land via the fetch step.
}

# --- 3. /opt/xcloud/xccode and /etc/xcloud/xccode (root-owned) ------------------
step_opt() {
    ensure_dir "$OPT" "root:root" 0755
    for sub in opencode venv hermes oac opencode-plugins openviking tei dsh bin; do
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
    copy_file "$(dirname "$0")/xccode-backup.sh" "$OPT/bin/xccode-backup.sh" 0755
    write_file "$ETC/backup.toml" <<'EOF'
# xccode backup target (XC-CODE-001 §11). Filled by the operator via SOPS — a second disk or
# USB drive until urd exists. An empty repository means the xccode-backup timer does nothing.
repository = ""
EOF
    # xcroute must start with safe defaults (no tokens -> 401, no pools -> refused) until the
    # operator's SOPS-provided config replaces this. Without it xcroute.service crash-loops.
    write_file "$ETC/serve.toml" <<'EOF'
# xccode-default-config — safe defaults until the operator's SOPS config replaces it.
# Deploy tooling must OVERWRITE this file unconditionally (never write-if-missing).
base_url = "http://127.0.0.1:18128"
tokens = {}
pools = {}
EOF
    # vetted plugins + approved skills (bundled, root-owned read-only; §4.3, §4.15).
    copy_file "$(dirname "$0")/../etc/opencode-plugins/shell_strategy.md" \
        "$OPT/opencode-plugins/shell_strategy.md" 0644
    ensure_dir "$OPT/skills/approved/karpathy-guidelines" "root:root" 0755
    copy_file "$(dirname "$0")/../etc/skills/karpathy-guidelines/SKILL.md" \
        "$OPT/skills/approved/karpathy-guidelines/SKILL.md" 0644
    copy_file "$(dirname "$0")/../etc/skills/karpathy-guidelines/EXAMPLES.md" \
        "$OPT/skills/approved/karpathy-guidelines/EXAMPLES.md" 0644
}

# --- 4. signed release: download, verify, extract into /opt/xcloud/xccode ---------
step_fetch() {
    if [ -z "$RELEASE" ]; then
        echo "skip: no --release (a real install needs an explicit signed tag; never main/latest)"
        return 0
    fi
    RELEASE_URL="${RELEASE_URL:-https://github.com/xcloud-gg/xccode/releases/download}"
    SIGNING_KEY="${XCCODE_SIGNING_KEY:-$ETC/signing-key.asc}"
    if [ "$CHECK" = 1 ]; then
        echo "would: fetch + verify release $RELEASE into $OPT"
    else
        sh "$(dirname "$0")/fetch-release.sh" --release "$RELEASE" --url "$RELEASE_URL" \
            --out "$OPT" --keyring "$SIGNING_KEY"
    fi
}

# --- 4b. venv: build xccode from the extracted source ----------------------------
step_venv() {
    src="$OPT/src"
    if [ -x "$OPT/venv/bin/xccode" ]; then
        echo "already: venv"
        return 0
    fi
    if [ "$CHECK" = 1 ]; then
        echo "would: create venv + pip install xccode from $src"
        return 0
    fi
    echo ">> create venv"
    python3 -m venv "$OPT/venv"
    echo ">> pip install xccode"
    "$OPT/venv/bin/pip" install --quiet "$src"
}

# --- 4c. bun runtime: OpenCode v2 installs npm plugins with bun -------------------
step_runtime() {
    bun_ver="1.4.2"
    bun_bin="$OPT/bun/bun-linux-x64/bun"
    if [ -x "$bun_bin" ]; then
        echo "already: bun $bun_ver"
        return 0
    fi
    if [ "$CHECK" = 1 ]; then
        echo "would: install bun $bun_ver (OpenCode v2 npm-plugin runtime, §4.3)"
        return 0
    fi
    echo ">> install bun $bun_ver"
    mkdir -p "$OPT/bun"
    curl -fsSL -o "$OPT/bun/bun.zip" \
        "https://github.com/oven-sh/bun/releases/download/bun-v${bun_ver}/bun-linux-x64.zip"
    python3 -c "import zipfile; zipfile.ZipFile('$OPT/bun/bun.zip').extractall('$OPT/bun')" \
        || { echo "install.sh: bun extraction failed" >&2; exit 1; }
    rm -f "$OPT/bun/bun.zip"
}

# --- 4d. node 24: OmniRoute's secure runtime floor (Node 22+; §4.5) ---------------
step_node() {
    node_ver="24.14.1"
    node_bin="$OPT/node-v${node_ver}-linux-x64/bin/node"
    if [ -x "$node_bin" ]; then
        echo "already: node $node_ver"
        return 0
    fi
    if [ "$CHECK" = 1 ]; then
        echo "would: install node $node_ver (OmniRoute runtime, §4.5)"
        return 0
    fi
    echo ">> install node $node_ver"
    curl -fsSL -o "$OPT/node.tar.xz" \
        "https://nodejs.org/dist/v${node_ver}/node-v${node_ver}-linux-x64.tar.xz"
    tar -xf "$OPT/node.tar.xz" -C "$OPT"
    rm -f "$OPT/node.tar.xz"
}

# --- 4e. OmniRoute: fetch pinned source, install deps, build (private router §4.5) --
step_omniroute() {
    omni_commit=$(sed -n '/^\[omniroute\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^version = "\(.*\)"$/\1/p')
    omni_dir="$OPT/omniroute"
    node_bin="$OPT/node-v24.14.1-linux-x64/bin/node"
    bun_bin="$OPT/bun/bun-linux-x64/bun"
    if [ -z "$omni_commit" ]; then
        echo "skip: no omniroute pin in versions.lock"
        return 0
    fi
    if [ -f "$omni_dir/.build/next/BUILD_ID" ]; then
        echo "already: omniroute $omni_commit (built)"
        return 0
    fi
    if [ "$CHECK" = 1 ]; then
        echo "would: fetch + build OmniRoute $omni_commit into $omni_dir"
        return 0
    fi
    echo ">> fetch OmniRoute $omni_commit"
    if [ ! -d "$omni_dir/.git" ]; then
        git clone --depth 1 https://github.com/diegosouzapw/OmniRoute.git "$omni_dir"
    fi
    (cd "$omni_dir" && git fetch --depth 1 origin "$omni_commit" && git checkout -q "$omni_commit")
    echo ">> install OmniRoute deps (bun)"
    (cd "$omni_dir" && "$bun_bin" install)
    echo ">> build OmniRoute (node $node_ver)"
    (cd "$omni_dir" && "$node_bin" --max-old-space-size=8192 scripts/build/build-next-isolated.mjs)
    # OmniRoute's data dir must be writable by the xccode service user, not root.
    ensure_dir "$STATE/omniroute" "xccode:xccode" 0700
}

# --- 4f. dsh (DeepSeek Harness): npm package under the pinned Node runtime ---------
step_dsh() {
    dsh_ver=$(sed -n '/^\[dsh\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^version = "\(.*\)"$/\1/p')
    npm_bin="$OPT/node-v24.14.1-linux-x64/bin/npm"
    if [ -z "$dsh_ver" ]; then echo "skip: no dsh pin"; return 0; fi
    if [ -x "$OPT/node-v24.14.1-linux-x64/bin/dsh" ]; then
        echo "already: dsh $dsh_ver"; return 0
    fi
    if [ "$CHECK" = 1 ]; then echo "would: npm install -g @deepseek-ai/dsh@$dsh_ver"; return 0; fi
    echo ">> npm install -g @deepseek-ai/dsh@$dsh_ver"
    # npm's shebang is `#!/usr/bin/env node` — put the pinned Node 24 first so the install
    # resolves node 24 (dsh needs ^22.19||>=24) and lands in the pinned prefix, not the system's.
    PATH="$OPT/node-v24.14.1-linux-x64/bin:$PATH" "$npm_bin" install -g "@deepseek-ai/dsh@$dsh_ver"
}

# --- 4g. OpenViking: project memory server (native Python, loopback 18180) ---------
step_openviking() {
    ov_ver=$(sed -n '/^\[openviking\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^version = "\(.*\)"$/\1/p')
    if [ -z "$ov_ver" ]; then echo "skip: no openviking pin"; return 0; fi
    if [ -x "$OPT/openviking/venv/bin/openviking-server" ]; then
        echo "already: openviking $ov_ver"; return 0
    fi
    if [ "$CHECK" = 1 ]; then echo "would: pip install openviking==$ov_ver"; return 0; fi
    echo ">> pip install openviking==$ov_ver"
    python3 -m venv "$OPT/openviking/venv"
    "$OPT/openviking/venv/bin/pip" install --quiet "openviking==$ov_ver"
    ensure_dir "$STATE/openviking" "xccode:xccode" 0750
    # Minimal loopback config: local AGFS + local vectordb, no embedder/VLM (summaries empty
    # until the operator sets embedding.dense.provider and a VLM). Deploy tooling OVERWRITES.
    write_file "$ETC/ov.conf" <<'EOF'
{
  "server": {"host": "127.0.0.1", "port": 18180},
  "storage": {
    "workspace": "/var/lib/xcloud/xccode/openviking",
    "agfs": {"backend": "local"},
    "vectordb": {"backend": "local"}
  }
}
EOF
}

# --- 4h. Hermes: headless learner (own venv, pinned tag) --------------------------
step_hermes() {
    hermes_ver=$(sed -n '/^\[hermes\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^version = "\(.*\)"$/\1/p')
    if [ -z "$hermes_ver" ]; then echo "skip: no hermes pin"; return 0; fi
    if [ -x "$OPT/hermes/.venv/bin/hermes" ]; then
        echo "already: hermes $hermes_ver"; return 0
    fi
    if [ "$CHECK" = 1 ]; then echo "would: install hermes $hermes_ver (own venv)"; return 0; fi
    echo ">> fetch hermes $hermes_ver"
    if [ ! -d "$OPT/hermes/.git" ]; then
        git clone --depth 1 --branch "$hermes_ver" \
            https://github.com/NousResearch/hermes-agent.git "$OPT/hermes"
    fi
    echo ">> install hermes (own venv, core only)"
    # Headless learner: core install only — `.[all]` pulls browser/messaging/voice that the
    # learner profile forbids anyway. Python floor >=3.11,<3.14 (system python3 is 3.13).
    python3 -m venv "$OPT/hermes/.venv"
    "$OPT/hermes/.venv/bin/pip" install --quiet -e "$OPT/hermes"
    ensure_dir "$STATE/hermes" "xccode:xccode" 0750
}

# --- 4i. TEI: local embedder (native Rust build, loopback 18181) -------------------
step_tei() {
    tei_ver=$(sed -n '/^\[tei\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^version = "\(.*\)"$/\1/p')
    router="$OPT/tei/text-embeddings-router"
    if [ -z "$tei_ver" ]; then echo "skip: no tei pin"; return 0; fi
    if [ -x "$router" ]; then echo "already: tei $tei_ver"; return 0; fi
    if [ "$CHECK" = 1 ]; then echo "would: build tei $tei_ver (cargo) into $OPT/tei"; return 0; fi
    # Rust toolchain: rustup-init as a downloaded binary (never piped into a shell).
    if ! command -v cargo >/dev/null 2>&1; then
        echo ">> install rustup (binary, no pipe-to-shell)"
        curl -fsSL -o "$OPT/tei-rustup-init" https://sh.rustup.rs
        sh "$OPT/tei-rustup-init" -y --default-toolchain stable --profile minimal
        rm -f "$OPT/tei-rustup-init"
        export PATH="$HOME/.cargo/bin:$PATH"
    fi
    echo ">> fetch tei $tei_ver"
    if [ ! -d "$OPT/tei/.git" ]; then
        git clone --depth 1 --branch "$tei_ver" \
            https://github.com/huggingface/text-embeddings-inference.git "$OPT/tei"
    fi
    echo ">> cargo install text-embeddings-router (mkl)"
    (cd "$OPT/tei" && cargo install --path router -F mkl --root "$OPT/tei")
    ensure_dir "$STATE/tei" "xccode:xccode" 0750
}


# --- 5. services (systemd units, all loopback) -----------------------------------
step_services() {
    units="$(dirname "$0")/units"
    for unit in xcroute.service omniroute.service openviking.service tei.service xccode-guard.service xccode-guard.path xccode-nft.service xccode-backup.service xccode-backup.timer; do
        copy_file "$units/$unit" "$SYSTEMD/$unit"
    done
    if [ "$CHECK" = 1 ]; then
        echo "would: systemctl daemon-reload + enable units"
        return 0
    fi
    systemctl daemon-reload
    for unit in xcroute.service omniroute.service openviking.service tei.service xccode-guard.path xccode-nft.service xccode-backup.timer; do
        systemctl enable "$unit" >/dev/null 2>&1 || true
    done
}

# --- 7. guardrails: render + write layers for the accounts that exist --------------
step_guard() {
    run "xccode guard apply" "$OPT/venv/bin/xccode" guard apply --config "$ETC/guardrails.toml"
    if [ "$CHECK" != 1 ]; then
        # Reload each layer so the boundary is live now, not at the next reboot.
        systemctl daemon-reload >/dev/null 2>&1 || true
        augenrules --load >/dev/null 2>&1 || true
        systemctl start xccode-nft.service >/dev/null 2>&1 || true
    fi
}

# --- 8. restore: bring back memory, skills, rules, routing history, bench results ----
step_restore() {
    if [ -z "$RESTORE" ]; then
        echo "skip: no --restore"
        return 0
    fi
    if [ "$CHECK" = 1 ]; then
        echo "would: restic restore $RESTORE latest -> $STATE"
        return 0
    fi
    echo ">> restic restore"
    restic -r "$RESTORE" restore latest --target / --include "$STATE" \
        || { echo "install.sh: --restore failed" >&2; exit 1; }
    chown -R xccode:xccode "$STATE"
}

# --- 6. operator profile: xcc launcher (OpenCode profile, OAC, skills next) --------
step_profile() {
    dst="/home/$OPERATOR/.local/bin/xcc"
    ensure_dir "/home/$OPERATOR/.local/bin" "$OPERATOR:$OPERATOR" 0755
    copy_file "$(dirname "$0")/xcc" "$dst" 0755
    # OpenCode profile (provider -> xcroute, model xc/auto, xccode-mcp, OAC agents). The xcc launcher
    # points OPENCODE_CONFIG here, so xccode's OpenCode never reads another install's config.
    prof="/home/$OPERATOR/.config/xccode/opencode"
    ensure_dir "$prof" "$OPERATOR:$OPERATOR" 0755
    oac_commit=$(sed -n '/^\[openagentscontrol\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^commit = "\(.*\)"$/\1/p')
    if [ -n "$oac_commit" ] && [ "$CHECK" != 1 ]; then
        ensure_dir "$OPT/oac" "root:root" 0755
        if [ ! -d "$OPT/oac/.git" ]; then
            echo ">> fetch OpenAgentsControl"
            git clone --depth 1 https://github.com/darrenhinde/OpenAgentsControl.git "$OPT/oac" >/dev/null 2>&1 || true
            (cd "$OPT/oac" && git fetch --depth 1 origin "$oac_commit" >/dev/null 2>&1 && git checkout -q "$oac_commit") || true
        fi
        echo ">> write profile with OAC agents"
        "$OPT/venv/bin/xccode" oac --oac-dir "$OPT/oac" \
            --profile "$(dirname "$0")/../etc/opencode/profile.json" --out "$prof/opencode.json"
    else
        copy_file "$(dirname "$0")/../etc/opencode/profile.json" "$prof/opencode.json" 0644
    fi
    chown "$OPERATOR:$OPERATOR" "$prof/opencode.json" 2>/dev/null || true
    # Upcoming: plugins, native skills, opencode serve user unit.
}

main() {
    echo "install.sh --operator $OPERATOR${RELEASE:+ --release $RELEASE}${RESTORE:+ --restore $RESTORE}${CHECK:+ --check}"
    step_account
    step_packages
    step_opt
    step_fetch
    step_venv
    step_runtime
    step_node
    step_omniroute
    step_dsh
    step_openviking
    step_hermes
    step_tei
    step_services
    step_profile
    step_guard
    step_restore
    # Upcoming increments: collect/learn/bench timers (as their commands land), full profile.
    if [ "$CHECK" = 1 ]; then
        echo "check: dry run complete; nothing was changed"
    else
        echo "install complete; run: xccode doctor"
    fi
}

main
