#!/bin/sh
# make-release.sh — build a signed test release for the B-50 VM test (XC-CODE-001 §11).
#
# Mirrors docs/release.md, but with a throwaway test key (never the operator's key) and no git tag:
#     make-release.sh --release test --out release
# Produces, under <out>/<release>/:  xccode-<release>.tar.gz  SHA256SUMS  SHA256SUMS.sig  keyring.gpg
set -eu

RELEASE="test"
OUT="release"
while [ $# -gt 0 ]; do
    case "$1" in
        --release) RELEASE="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        *) echo "make-release: unknown argument: $1" >&2; exit 2 ;;
    esac
done

tarball="xccode-$RELEASE.tar.gz"
dest="$OUT/$RELEASE"
mkdir -p "$dest"

git archive --format=tar.gz -o "$dest/$tarball" HEAD
(cd "$dest" && sha256sum "$tarball" > SHA256SUMS)
gpg --batch --yes --armor --detach-sign -o "$dest/SHA256SUMS.sig" "$dest/SHA256SUMS"
gpg --export > "$dest/keyring.gpg"

echo "make-release: $RELEASE -> $dest ($tarball, SHA256SUMS, SHA256SUMS.sig, keyring.gpg)"
