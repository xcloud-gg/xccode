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

copy_file() {  # copy_file <src> <dst> [mode] — idempotent install of a tracked asset.
    # Overwrites when the tracked asset changed so an upgrade actually applies it; a no-op when the
    # installed copy is byte-identical. (Skip-if-exists would hide every asset change on re-install.)
    src="$1" dst="$2" mode="${3:-0644}"
    if [ -f "$dst" ] && cmp -s "$src" "$dst"; then
        echo "already: $dst"
        return 0
    fi
    if [ "$CHECK" = 1 ]; then
        if [ -f "$dst" ]; then echo "would: update $dst"; else echo "would: install $dst"; fi
        return 0
    fi
    if [ -f "$dst" ]; then echo ">> update $dst"; else echo ">> install $dst"; fi
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

# --- 2. packages (Debian main only) ---
step_packages() {
    for pkg in git curl bubblewrap restic auditd python3-venv gpgv; do
        if dpkg -s "$pkg" >/dev/null 2>&1; then
            echo "already: package $pkg"
        else
            run "apt-get install -y $pkg" apt-get install -y "$pkg"
        fi
    done
    # `uv` is not a Debian package: it is installed into the venv from the signed release (step 4b).
    # gitleaks is a reserved pin only (Guard-lite uses its own regex rule set at runtime; nothing
    # invokes the gitleaks binary), so there is deliberately no step_gitleaks — see versions.lock.
    # TEI's prebuilt text-embeddings-router ships as a binary (no Rust build at install time; §4.8).
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
    copy_file "$(dirname "$0")/xccode-learn.sh" "$OPT/bin/xccode-learn.sh" 0755
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
    if [ "$CHECK" = 1 ]; then
        echo "would: create venv + pip install xccode (refresh) from $src"
        return 0
    fi
    if [ ! -x "$OPT/venv/bin/xccode" ]; then
        echo ">> create venv"
        python3 -m venv "$OPT/venv"
    fi
    # Always (re)install: the release source may have changed since the last install, and pip is a
    # no-op when it is unchanged. This is what makes an upgrade actually apply code changes.
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
    chmod +x "$bun_bin"  # zipfile.extractall does not preserve the executable bit
}

# --- 4c2. OpenCode: pinned npm package (the `xcc` front door, §4.1) -----------------
# OpenCode is the operator-facing coding agent; `xcc` execs it with xccode's own config. It is
# installed as the published V2 package `@opencode/cli` (pinned in versions.lock); npm enforces
# package integrity, so no hand-recorded sha256 is needed here.
step_opencode() {
    oc_ver=$(sed -n '/^\[opencode\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^version = "\(.*\)"$/\1/p')
    node_home="$OPT/node-v24.14.1-linux-x64"
    oc_bin="$OPT/opencode/bin/opencode"
    if [ -z "$oc_ver" ]; then echo "skip: no opencode pin"; return 0; fi
    if [ -x "$oc_bin" ] && "$oc_bin" --version 2>/dev/null | grep -q "v${oc_ver}\$"; then
        echo "already: opencode $oc_ver"
        return 0
    fi
    if [ "$CHECK" = 1 ]; then echo "would: npm install -g @opencode/cli@${oc_ver}"; return 0; fi
    echo ">> npm install -g @opencode/cli@${oc_ver}"
    # OpenCode lives at $OPT/opencode (spec §15.3 "opencode/ OpenCode release binary"), not the
    # pinned Node's prefix. npm's shebang is `#!/usr/bin/env node`, so put the pinned Node 24 first
    # for the install + postinstall, and pass --prefix so NPM_CONFIG_PREFIX / .npmrc can't redirect
    # the install elsewhere.
    PATH="$node_home/bin:$PATH" "$node_home/bin/npm" install -g --prefix "$OPT/opencode" "@opencode/cli@${oc_ver}" \
        || { echo "install.sh: opencode install failed" >&2; exit 1; }
    [ -x "$oc_bin" ] \
        || { echo "install.sh: opencode not found after install" >&2; exit 1; }
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
    # Turbopack is OmniRoute's default bundler but deadlocks on the production build (the spawned
    # `next-build` process stalls with 0 CPU and no output after "Creating an optimized production
    # build"; seen on thor, 24 vCPU / 31 GiB — not a memory limit). OMNIROUTE_USE_TURBOPACK=0 is
    # OmniRoute's own documented escape hatch to webpack (build-next-isolated.mjs reads it); webpack
    # completes in ~2.5 min. (XC-CODE-001 §4.5)
    (cd "$omni_dir" && OMNIROUTE_USE_TURBOPACK=0 "$node_bin" --max-old-space-size=8192 scripts/build/build-next-isolated.mjs)
    # OmniRoute's data dir must be writable by the xccode service user, not root.
    ensure_dir "$STATE/omniroute" "xccode:xccode" 0700
    # The build runs as root but the service runs as xccode; OmniRoute's startup regenerates its
    # fumadocs MDX (`.source/`) and touches the Next.js build output (`.build/next`, `dist`), so
    # those runtime-writable trees must belong to xccode or the service crash-loops with
    # "Failed to write to output file ... permission denied".
    for d in "$omni_dir/.source" "$omni_dir/.build" "$omni_dir/dist"; do
        [ -d "$d" ] && chown -R xccode:xccode "$d" 2>/dev/null || true
    done
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
    # Local AGFS + local vectordb. The dense embedder is xccode's own TEI (an OpenAI-compatible
    # endpoint on 127.0.0.1:18181, 1024-dim Qwen3-Embedding-0.6B) — no llama-cpp local embedder, so
    # no third-party model download. The VLM (semantic extraction of entities/preferences) is left
    # empty until the operator points it at xcroute/OmniRoute (see OPERATOR-KEYS.md). Deploy tooling
    # OVERWRITES this file. NOTE: the OpenViking vectordb collection is dimension-locked; switching
    # the embedder dimension requires wiping /var/lib/xcloud/xccode/openviking.
    write_file "$ETC/ov.conf" <<'EOF'
{
  "server": {"host": "127.0.0.1", "port": 18180},
  "embedding": {
    "dense": {
      "provider": "openai",
      "model": "Qwen/Qwen3-Embedding-0.6B",
      "api_base": "http://127.0.0.1:18181/v1",
      "dimension": 1024,
      "input": "text"
    }
  },
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
    # Hermes's model is xcroute (an OpenAI-compatible endpoint). The api_key (the `hermes` token
    # from serve.toml) is operator-owned — left unset here; SOPS/deploy tooling fills it. Hermes
    # reads its config from ~/.hermes/config.yaml, whose parent is the xccode account's home (the
    # state dir). (Earlier drafts wrote ~/.config/hermes; Hermes has never read from there.)
    ensure_dir "$STATE/.hermes" "xccode:xccode" 0700
    write_file "$STATE/.hermes/config.yaml" <<'EOF'
model:
  default: "xc/auto"
  provider: "custom"
  base_url: "http://127.0.0.1:18080/v1"
  # api_key: "<hermes token>"   # operator fills; matches serve.toml [tokens] hermes
EOF
}

# --- 4i. TEI: local embedder (prebuilt binary, loopback 18181) ---------------------
# Built once at release time (the from-source `cargo install` is ~3-4 h — impractical), shipped as a
# pinned binary like opencode/gitleaks, verified by its sha256 in versions.lock (XC-CODE-001 §4.8).
step_tei() {
    tei_ver=$(sed -n '/^\[tei\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^version = "\(.*\)"$/\1/p')
    tei_sha=$(sed -n '/^\[tei\]/,/^\[/p' "$(dirname "$0")/../etc/versions.lock" \
        | sed -n 's/^sha256 = "\(.*\)"$/\1/p')
    router="$OPT/tei/bin/text-embeddings-router"
    if [ -z "$tei_ver" ] || [ -z "$tei_sha" ]; then echo "skip: no tei pin"; return 0; fi
    if [ -x "$router" ]; then echo "already: tei $tei_ver"; return 0; fi
    if [ "$CHECK" = 1 ]; then echo "would: download + verify tei $tei_ver (prebuilt)"; return 0; fi
    echo ">> download tei $tei_ver (prebuilt)"
    mkdir -p "$OPT/tei/bin"
    TEI_URL="${TEI_URL:-https://github.com/xcloud-gg/xccode/releases/download/tei-$tei_ver/text-embeddings-router}"
    curl -fsSL -o "$router" "$TEI_URL"
    echo "$tei_sha  $router" | sha256sum -c - \
        || { echo "install.sh: tei checksum mismatch" >&2; exit 1; }
    chmod +x "$router"
    ensure_dir "$STATE/tei" "xccode:xccode" 0750
}


# --- 5. services (systemd units, all loopback) -----------------------------------
step_services() {
    units="$(dirname "$0")/units"
    for unit in xcroute.service omniroute.service openviking.service tei.service xccode-guard.service xccode-guard.path xccode-nft.service xccode-backup.service xccode-backup.timer xccode-bench.service xccode-bench.timer xccode-learn.service xccode-learn.timer; do
        copy_file "$units/$unit" "$SYSTEMD/$unit"
    done
    if [ "$CHECK" = 1 ]; then
        echo "would: systemctl daemon-reload + enable/start timers"
        return 0
    fi
    systemctl daemon-reload
    for unit in xcroute.service omniroute.service openviking.service tei.service xccode-guard.path xccode-nft.service xccode-backup.timer xccode-bench.timer xccode-learn.timer; do
        systemctl enable "$unit" >/dev/null 2>&1 || true
    done
    # `enable` only wires boot-time start; an install on a running host must also start the
    # timers now or collect/learn/bench stay dead until a reboot (found on thor 2026-10-09).
    for timer in xccode-backup.timer xccode-bench.timer xccode-learn.timer; do
        systemctl start "$timer" >/dev/null 2>&1 || true
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

# --- 9. collect: marius's user units (hourly + session end) ------------------------
step_collect() {
    copy_file "$(dirname "$0")/xccode-collect.sh" "$OPT/bin/xccode-collect.sh" 0755
    # User units live in the operator's home, not /etc/systemd/system: they run as the desktop
    # user and only exist when there is a desktop session (linger not required — the timer is
    # wanted-by default.target in the user manager). Enabling happens on first login.
    udir="/home/$OPERATOR/.config/systemd/user"
    ensure_dir "$udir" "$OPERATOR:$OPERATOR" 0755
    copy_file "$(dirname "$0")/units/xccode-collect.service" "$udir/xccode-collect.service"
    copy_file "$(dirname "$0")/units/xccode-collect.timer" "$udir/xccode-collect.timer"
    chown "$OPERATOR:$OPERATOR" "$udir/xccode-collect.service" "$udir/xccode-collect.timer" 2>/dev/null || true
    # "Enable" the timer the way `systemctl --user enable` does: a wants symlink under
    # timers.target.wants. Doing it as install (root, no session bus) — not `systemctl --user` —
    # means it picks up on the operator's next login without any manual step (found dead on thor).
    if [ "$CHECK" = 1 ]; then
        echo "would: link $udir/timers.target.wants/xccode-collect.timer"
        return 0
    fi
    wants="$udir/timers.target.wants"
    mkdir -p "$wants"
    ln -sf ../xccode-collect.timer "$wants/xccode-collect.timer"
    chown -h "$OPERATOR:$OPERATOR" "$wants/xccode-collect.timer" 2>/dev/null || true
    chown -R "$OPERATOR:$OPERATOR" "$wants" 2>/dev/null || true
}

# --- 10. tokens: the five xcroute agent tokens (generated here, not operator-provided) -----
step_tokens() {
    tokfile="$ETC/tokens.env"
    if [ -f "$tokfile" ]; then echo "already: tokens"; return 0; fi
    if [ "$CHECK" = 1 ]; then echo "would: generate the five xcroute agent tokens"; return 0; fi
    echo ">> generate xcroute agent tokens"
    # Raw tokens land in root:xccode 0640; only their sha256 digests go into serve.toml. Consumers
    # read the raw token they need from here (the Hermes config.yaml, OpenViking's VLM, xccode-mcp).
    : > "$tokfile.tmp"
    for agent in opencode hermes dsh-bench dsh-job openviking; do
        printf '%s=%s\n' "$agent" "$(openssl rand -hex 32)" >> "$tokfile.tmp"
    done
    chown root:xccode "$tokfile.tmp" 2>/dev/null || true
    chmod 0640 "$tokfile.tmp"
    mv "$tokfile.tmp" "$tokfile"
    # serve.toml: token digests + the default pools. The pool `provider` is the combo name the
    # operator creates in OmniRoute (fast / coding-fast / coding-strong / reasoning); the cost is a
    # per-request budget estimate (OmniRoute reports the real cost at call time).
    {
        echo "# xccode-default-config — tokens generated at install; pools match the OmniRoute combos."
        echo 'base_url = "http://127.0.0.1:18128"'
        echo "[tokens]"
        while IFS='=' read -r agent token; do
            printf '%s = "%s"\n' "$agent" "$(printf %s "$token" | sha256sum | cut -d' ' -f1)"
        done < "$tokfile"
        echo ""
        echo "[pools.fast]"
        echo 'provider = "fast"'
        echo 'est_cost_micro_usd = 10'
        echo ""
        echo "[pools.coding-fast]"
        echo 'provider = "coding-fast"'
        echo 'est_cost_micro_usd = 25'
        echo ""
        echo "[pools.coding-strong]"
        echo 'provider = "coding-strong"'
        echo 'est_cost_micro_usd = 60'
        echo ""
        echo "[pools.reasoning]"
        echo 'provider = "reasoning"'
        echo 'est_cost_micro_usd = 80'
        echo ""
        echo "[budget]"
        echo 'per_request = 1000000'
        echo 'per_day = 10000000'
    } > "$ETC/serve.toml"
    # Distribute the two raw tokens consumed by xccode's own services.
    hermes_token="$(sed -n 's/^hermes=//p' "$tokfile")"
    openviking_token="$(sed -n 's/^openviking=//p' "$tokfile")"
    if [ -n "$hermes_token" ]; then
        ensure_dir "$STATE/.hermes" "xccode:xccode" 0700
        cat > "$STATE/.hermes/config.yaml" <<EOF
model:
  default: "xc/auto"
  provider: "custom"
  base_url: "http://127.0.0.1:18080/v1"
  api_key: "$hermes_token"
EOF
        chown xccode:xccode "$STATE/.hermes/config.yaml" 2>/dev/null || true
    fi
    if [ -n "$openviking_token" ]; then
        python3 - "$ETC/ov.conf" "$openviking_token" <<'PY'
import json, sys
path, token = sys.argv[1], sys.argv[2]
with open(path) as f:
    cfg = json.load(f)
cfg["vlm"] = {"provider": "openai", "model": "xc/auto",
              "api_base": "http://127.0.0.1:18080/v1", "api_key": token}
with open(path, "w") as f:
    json.dump(cfg, f, indent=2)
PY
    fi
}

# --- 10b. operator's xcc token: hand the raw opencode token to marius (spec §4.1) ---
# The `xcc` launcher sources ~/.config/xccode/xcc.env for XCC_TOKEN; xcroute's other consumers
# (Hermes, OpenViking VLM, xccode-mcp) read their raw tokens directly from tokens.env as xccode.
step_xcc_env() {
    tokfile="$ETC/tokens.env"
    xcc_env="/home/$OPERATOR/.config/xccode/xcc.env"
    if [ ! -f "$tokfile" ]; then echo "skip: no tokens"; return 0; fi
    if [ -f "$xcc_env" ]; then echo "already: xcc.env"; return 0; fi
    if [ "$CHECK" = 1 ]; then echo "would: write $xcc_env (XCC_TOKEN)"; return 0; fi
    echo ">> write xcc.env (XCC_TOKEN for xcc)"
    ensure_dir "$(dirname "$xcc_env")" "$OPERATOR:$OPERATOR" 0700
    opencode_tok="$(sed -n 's/^opencode=//p' "$tokfile")"
    printf 'XCC_TOKEN=%s\n' "$opencode_tok" > "$xcc_env.tmp"
    chown "$OPERATOR:$OPERATOR" "$xcc_env.tmp" 2>/dev/null || true
    chmod 0600 "$xcc_env.tmp"
    mv "$xcc_env.tmp" "$xcc_env"
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
    step_opencode
    step_omniroute
    step_dsh
    step_openviking
    step_hermes
    step_tei
    step_services
    step_profile
    step_collect
    step_tokens
    step_xcc_env
    step_guard
    step_restore
    # Upcoming increments: full profile.
    if [ "$CHECK" = 1 ]; then
        echo "check: dry run complete; nothing was changed"
    else
        echo "install complete; run: xccode doctor"
    fi
}

main
