#!/bin/sh
# verify-release.sh — verify a signed xccode release before install (XC-DES-001 §6.11 "Distribution").
#     verify-release.sh <tarball> <sha256sums> <signature> <keyring>
# Exits non-zero on any checksum or signature mismatch; reads nothing from the network.
set -eu

tarball="$1" sums="$2" sig="$3" keyring="$4"
SHA256SUM="${SHA256SUM:-sha256sum}"
GPGV="${GPGV:-gpgv}"

dir="$(cd "$(dirname "$tarball")" && pwd)"

# 1. checksum: refuse on any missing file or mismatch
(
    cd "$dir"
    "$SHA256SUM" -c "$(basename "$sums")"
) || { echo "verify-release: checksum mismatch" >&2; exit 1; }

# 2. signature: refuse unless SHA256SUMS verifies against the operator's key (confirmed out of band)
"$GPGV" --keyring "$keyring" "$sig" "$sums" \
    || { echo "verify-release: signature verification failed" >&2; exit 1; }

echo "verify-release: ok"
