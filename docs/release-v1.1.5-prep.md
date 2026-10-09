# W6 — release v1.1.5 prep (operator runs this)

The xccode build is fully wired and deployed on thor from the current `main` working tree. This is
the exact, paste-ready release procedure once you decide to cut it (you said to hold and batch more
— everything below is now batched). TEI binary asset `tei-v1.9.4` is already published and unchanged,
so only the tarball + sums + signature change.

## 0. Preconditions (already done)

- `repos/xccode` local `main` @ the hardening commit (contains: copy_file overwrite, learn/serve/
  jobs wiring, tool-call passthrough, security hardening). 344 tests green, ruff clean, ci_checks 0.
- spec `xcloud-docs` @ `09be835` (main) — gitleaks wording fixed; default branch now `main`.
- thor currently runs the same code (pre-release upgrade path, doctor 13/13). The release redeploy
  verifies it end-to-end from the signed tag.
- `gh` authenticated as `xc0-marius` (repo + workflow scope) — but **signing is your key**, not
  an agent's.

## 1. Push main

```sh
cd /opt/xcloud/repos/xccode
git push origin main            # brings GitHub up to the hardened build
```

(The xcloud-docs spec push is your call — it has the gitleaks wording fix and the default-branch
switch already landed remotely via `gh repo edit`.)

## 2. Tag + sign v1.1.5

```sh
cd /opt/xcloud/repos/xccode
git tag -s v1.1.5 -m "xccode v1.1.5 — full wiring + security hardening"
git push --tags
```

## 3. Build + sign the assets

```sh
cd /opt/xcloud/repos/xccode
git archive --format=tar.gz -o xccode-v1.1.5.tar.gz v1.1.5
sha256sum xccode-v1.1.5.tar.gz > SHA256SUMS
gpg --armor --detach-sign -o SHA256SUMS.sig SHA256SUMS
gpg --export > keyring.gpg
```

## 4. Publish the release

```sh
gh release create v1.1.5 \
    xccode-v1.1.5.tar.gz SHA256SUMS SHA256SUMS.sig keyring.gpg \
    --repo xcloud-gg/xccode \
    --title "xccode v1.1.5" \
    --notes "learning loop wired; bench scores applied; opencode serve unit; dsh background jobs; tool-call passthrough; security hardening (advisor C1-C6)"
```

(The `tei-v1.9.4` release with the embedder binary already exists — no action needed.)

## 5. Redeploy thor from the signed tag (proves the release path)

On thor, as root:

```sh
sudo /tmp/xc-pre/install/install.sh --operator marius --release v1.1.5
# then:
/opt/xcloud/xccode/venv/bin/xccode doctor   # expect 13/13
```

(`install.sh` fetches the signed tarball from GitHub releases, verifies checksum + gpg signature,
extracts over `$OPT/src`, and every step is now upgrade-safe: changed assets overwrite, timers
start, daemons try-restart.)

## Acceptance checklist (after the thor redeploy)

- [ ] `xccode doctor` = 13/13
- [ ] `xcc run --agent OpenCoder ... "fix the bug in x"` → completes, file written (tools work)
- [ ] `xccode doctor` shows `opencode-serve-unit` OK; port 18090 listening; anonymous = 401
- [ ] `ls /var/lib/xcloud/xccode/learn/pending/` consumed after the 03:30 learn run
- [ ] `cat /var/lib/xcloud/xccode/bench/scores.toml` after Sunday's bench run
- [ ] a `job_start` via OpenCode → commits land on `xc/job-<id>` (operator merges or discards)
- [ ] the hostile-job security smoke (MAR-21 comment) still refuses everything

## Deferred (not X1)

- X2: PQ-17 (20-task parity) · PQ-3 per-host creds
- X4: alternative dsh compositions, OpenShell comparison (D76)
- OpenViking VLM entity extraction (uses the VLM; optional, the memory-content search works)
