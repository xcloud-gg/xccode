# xccode release procedure

Releases are **tagged and signed by the operator's key** — never by an agent, never from `main` or
`latest`. `install.sh` then installs only from a signed tag (XC-CODE-001 §11 "Distribution").

## One release

1. **Tag the release.** On `main`, at a reviewed commit, the operator runs:

   ```sh
   git tag -s v1.0.0 -m "xccode v1.0.0"   # signed tag
   git push --tags
   ```

2. **Build the release tarball.** `git archive` the tag into `xccode-v1.0.0.tar.gz` so the tag and
   the tarball cannot drift:

   ```sh
   git archive --format=tar.gz -o xccode-v1.0.0.tar.gz v1.0.0
   ```

3. **Checksum + sign.** Produce `SHA256SUMS` and a detached signature over it:

   ```sh
   sha256sum xccode-v1.0.0.tar.gz > SHA256SUMS
   gpg --armor --detach-sign -o SHA256SUMS.sig SHA256SUMS
   ```

4. **Publish** the three assets under the release: `xccode-v1.0.0.tar.gz`, `SHA256SUMS`,
   `SHA256SUMS.sig`.

## What the installer does

`install.sh --release v1.0.0` runs `fetch-release.sh`, which downloads the three assets, then
`verify-release.sh`:

1. `sha256sum -c SHA256SUMS` — refuses on any missing file or checksum mismatch;
2. `gpgv --keyring <signing-key> SHA256SUMS.sig SHA256SUMS` — refuses unless the signature verifies.

Nothing is extracted or installed unless both pass.

## Operator responsibilities

- **Confirm the signing-key fingerprint out of band on first install** (a key fetched from the same
  host as the tarball proves nothing).
- Never install from `main` or `latest`; always an explicit signed tag.
- Fill `etc/versions.lock` (version + sha256 + commit) at release time — `xccode.versions.validate`
  refuses to ship or upgrade with an empty pin.
- `xccode upgrade` shows diffs of pinned components and applies only with `--apply`.
