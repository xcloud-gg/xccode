#!/bin/sh
# fetch-release.sh — download, verify, and extract a signed xccode release (XC-CODE-001 §11).
#     fetch-release.sh --release <tag> --url <base> --out <dir> --keyring <file> [--check]
# Never runs on an unverified archive: download, verify-release.sh, then extract.
set -eu

DOWNLOADER="${DOWNLOADER:-curl}"
VERIFY="$(dirname "$0")/verify-release.sh"

RELEASE="" URL="" OUT="" KEYRING="" CHECK=0
while [ $# -gt 0 ]; do
    case "$1" in
        --release) RELEASE="$2"; shift 2 ;;
        --url) URL="$2"; shift 2 ;;
        --out) OUT="$2"; shift 2 ;;
        --keyring) KEYRING="$2"; shift 2 ;;
        --check) CHECK=1; shift ;;
        *) echo "fetch-release: unknown argument: $1" >&2; exit 2 ;;
    esac
done
[ -n "$RELEASE" ] && [ -n "$URL" ] && [ -n "$OUT" ] && [ -n "$KEYRING" ] \
    || { echo "fetch-release: --release --url --out --keyring are required" >&2; exit 2; }

tarball="xccode-$RELEASE.tar.gz"
if [ "$CHECK" = 1 ]; then
    echo "would: fetch $URL/$RELEASE/$tarball (+ SHA256SUMS, .sig), verify, extract into $OUT"
    exit 0
fi

mkdir -p "$OUT"
"$DOWNLOADER" -fsSL -o "$OUT/$tarball" "$URL/$RELEASE/$tarball"
"$DOWNLOADER" -fsSL -o "$OUT/SHA256SUMS" "$URL/$RELEASE/SHA256SUMS"
"$DOWNLOADER" -fsSL -o "$OUT/SHA256SUMS.sig" "$URL/$RELEASE/SHA256SUMS.sig"
sh "$VERIFY" "$OUT/$tarball" "$OUT/SHA256SUMS" "$OUT/SHA256SUMS.sig" "$KEYRING"
mkdir -p "$OUT/src"
tar -xzf "$OUT/$tarball" -C "$OUT/src"
echo "fetch-release: $RELEASE extracted into $OUT/src"
