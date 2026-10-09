# xccode release / runbook (operator)

Cut a signed release from the local build and install it on thor. Pushing, tagging, and signing are
operator-only (agents never push, never sign, never install from `main`/`latest`).

State today: `repos/xccode` @ `f7adfb8` (38 commits ahead of `origin/main`), last tag `v1.0.0`
(stale). Spec + state repos are also ahead.

## 0. Prerequisites

- `gh` CLI authenticated (`gh auth status`), or GitHub web UI.
- The operator's signing key in `gpg` (currently EDDSA `42324EDAD…`, `xc0-marius <marius@xcloud.gg>`).
- SSH access to thor (`xcloud@THOR`, the operator's recorded thor address).

## 1. Push the three repos

```sh
# xccode (the build)
cd /opt/xcloud/repos/xccode
git push origin main

# spec changes (xccode-design.md, host-spec-thor.md)
cd /opt/xcloud/repos/xcloud-docs
git push origin main

# canonical state (DECISIONS, status)
cd /opt/xcloud/repos/xcloud-state
git push origin main
```

## 2. Tag + sign the release (on `main`, at the reviewed commit)

```sh
cd /opt/xcloud/repos/xccode
git tag -s v1.1.0 -m "xccode v1.1.0"   # signed tag; pick the version
git push --tags
```

## 3. Build + checksum + sign the release assets

```sh
cd /opt/xcloud/repos/xccode
git archive --format=tar.gz -o xccode-v1.1.0.tar.gz v1.1.0
sha256sum xccode-v1.1.0.tar.gz > SHA256SUMS
gpg --armor --detach-sign -o SHA256SUMS.sig SHA256SUMS
gpg --export > keyring.gpg
```

## 4. Upload the TEI binary (separate asset the installer downloads)

The TEI embedder ships as a prebuilt binary (built on thor, pinned by sha256 in `etc/versions.lock`
= `fbe70ee2…`). Fetch it from thor and publish it under a `tei-v1.9.4` release:

```sh
# from your workstation, pull the binary off thor
scp xcloud@THOR:/opt/xcloud/xccode/tei/bin/text-embeddings-router /tmp/
sha256sum /tmp/text-embeddings-router   # must equal fbe70ee2567f1e61f9b4b977eeee146b2fc4f01a61977a7196ace6badeca566f

# create a release named tei-v1.9.4 and upload text-embeddings-router as its asset
gh release create tei-v1.9.4 /tmp/text-embeddings-router --repo xcloud-gg/xccode --title "TEI v1.9.4"
```

(`install.sh` fetches it from
`https://github.com/xcloud-gg/xccode/releases/download/tei-v1.9.4/text-embeddings-router`.)

## 5. Publish the release

```sh
cd /opt/xcloud/repos/xccode
gh release create v1.1.0 \
    xccode-v1.1.0.tar.gz SHA256SUMS SHA256SUMS.sig keyring.gpg \
    --repo xcloud-gg/xccode --title "xccode v1.1.0" --notes "M1–M7 + advisor + learner store"
```

## 6. Install on thor (fresh, from the signed tag)

```sh
# on thor, as root:
#   first install: confirm the signing-key fingerprint OUT OF BAND (see §0)
#   RELEASE_URL is only needed if not using the default GitHub releases URL
sudo env XCCODE_SIGNING_KEY=/path/to/signing-key.asc \
     ./install.sh --operator marius --release v1.1.0
```

Then verify:

```sh
/opt/xcloud/xccode/venv/bin/xccode doctor   # expect 11/11
systemctl is-active xcroute omniroute openviking tei
```

## Operator-only notes (do not skip)

- **Confirm the signing-key fingerprint out of band on first install** — a key fetched from the same
  host as the tarball proves nothing.
- `versions.lock` is already filled (all pins + the TEI sha256); `xccode.versions.validate` refuses
  an empty pin, so do not hand-edit checksums.
- The OpenCode Zen provider key, the five xcroute tokens, and the advisor's Anthropic key
  are **not** in the repo — they are entered in OmniRoute / `serve.toml` / `advisor.toml` on thor
  (see `docs/OPERATOR-KEYS.md` + `docs/OPERATOR-KEYS.env`).
- Choose the licence and commit `LICENSE` (still unset) before the repo is broadly public.
