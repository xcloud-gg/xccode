# VM test (B-50)

Reproduces the B-50 acceptance gate from XC-CODE-001 §11: on a **fresh Debian 13** host,
`install.sh --operator <login> --release <tag>` installs end-to-end and `xccode doctor` is green.

The test never touches a real host: it boots a checksum-verified Debian 13 cloud image under QEMU
(libvirt), cloud-init runs the installer from a **signed test release** served by `serve.py`, and the
result is POSTed back for inspection. Only mock providers and a throwaway test signing key are used.

## Layout

- `make-release.sh` — build the signed test release from `git archive` (never `main`/`latest`).
- `serve.py` — serves the release over HTTP and captures the guest's POSTed result.
- `seed/` — cloud-init NoCloud (`meta-data` + `user-data`); the guest finds the host at its
  default gateway, so no private address is committed.
- `domain.xml` — libvirt domain: boot disk, seed ISO, `default` NAT network, serial console.

## One run

Everything happens under a scratch dir (`/var/tmp/xccode-vm-test` below); adjust as needed.

```sh
ROOT=/var/tmp/xccode-vm-test && mkdir -p "$ROOT"

# 0. one-time: checksum-verified Debian 13 generic cloud image
curl -fL -o "$ROOT/debian-13-generic-amd64.qcow2" "$IMAGE_URL"
sha512sum -c <(echo "$IMAGE_SHA512  $ROOT/debian-13-generic-amd64.qcow2")
# the full install (build deps + OmniRoute/TEI builds + venvs) exceeds the image's 3 GiB root,
# and the OmniRoute Next.js + TEI Rust builds need real headroom — resize the disk and RAM:
qemu-img resize "$ROOT/debian-13-generic-amd64.qcow2" 20G   # cloud-init growpart fills the root

# 1. throwaway test signing key + signed release + the repo tarball the seed installs from
export GNUPGHOME="$ROOT/keys"
gpg --batch --passphrase '' --quick-gen-key 'xccode-test <test@example.invalid>' rsa2048 sign 1d
./make-release.sh --release test --out "$ROOT/release"
git archive --format=tar.gz -o "$ROOT/repo.tgz" HEAD

# 2. serve the release (background); the guest POSTs its result back to result.log
(cd "$ROOT" && VM_TEST_ROOT="$ROOT" python3 tools/vm-test/serve.py) &

# 3. build the seed ISO and reset a fresh copy of the image
xorriso -as mkisofs -output "$ROOT/seed.iso" -volid cidata -joliet -rock tools/vm-test/seed
# (the image in step 0 is the pristine copy; the domain boots "$ROOT/debian-13-generic-amd64.qcow2")

# 4. boot
virsh -c qemu:///system undefine xccode-test 2>/dev/null || true
virsh -c qemu:///system define tools/vm-test/domain.xml
virsh -c qemu:///system start xccode-test

# 5. wait for the result (install + venv build takes a few minutes), then read it
until grep -q XCCDONE "$ROOT/result.log" 2>/dev/null; do sleep 10; done
virsh -c qemu:///system destroy xccode-test
cat "$ROOT/result.log"
```

**Pass** = the log ends with `XCCDONE` and `doctor: N/N checks passed` (with `INSTALL_EXIT=0`).

Requires: `qemu-system-x86_64`, `libvirtd` with a `default` NAT network (`virsh net-start default`),
`xorriso`, `python3`, `gpg`. The guest reaches the host's `serve.py` through the NAT bridge's
gateway, discovered at boot (`ip route | awk '/default/ {print $3}'`).
