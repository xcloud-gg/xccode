# xccode operator keys — what to provide and where

This is the fill-in sheet for the secrets xccode needs to route real models and run the memory
semantic layer. None of these values live in the repo; they are entered by the operator, either
through the OmniRoute dashboard or into the SOPS-managed config files (`serve.toml`, `ov.conf`).
Loopback addresses and ports are public (XC-CODE-001 §15.3); only the secrets are private.

## 1. LLM provider API keys (OmniRoute)

xccode does not hold provider keys — OmniRoute does. xcroute sends each request to OmniRoute with
`model: <pool>`, and OmniRoute routes that pool to the provider combo you configure.

**Where:** OmniRoute dashboard at `http://127.0.0.1:18128` (from `marius`'s browser, via SSH tunnel
or the thor desktop). Add one provider per upstream, then a combo whose **model name matches the
xcroute pool name** in §2.

| Pool (xcroute) | What it must be (OmniRoute combo/model name) | Provider(s) you configure |
|---|---|---|
| `fast` | `fast` | a cheap/fast model |
| `coding-fast` | `coding-fast` | a fast coding model |
| `coding-strong` | `coding-strong` | a strong frontier coding model |
| `reasoning` | `reasoning` | a reasoning model |

Keys you will paste into OmniRoute (one per provider you use), e.g.:

| Field | Example shape | Value |
|---|---|---|
| OpenAI API key | `sk-proj-…` | `<fill in>` |
| Anthropic API key | `sk-ant-…` | `<fill in>` |
| …any other provider | | `<fill in>` |

The combo model name is the only coupling to xccode: it must equal the pool's `provider` in §2.

## 2. xcroute agent tokens + pools — `serve.toml`

**Where:** `/etc/xcloud/xccode/serve.toml` (operator-owned, SOPS). Tokens are stored only as their
sha256 digest, so xcroute never keeps the raw token.

Generate five random tokens and record their sha256 (or use the digests you already minted):

```sh
TOKEN=$(openssl rand -hex 32); echo "raw=$TOKEN"; echo "sha256=$(printf %s "$TOKEN" | sha256sum | cut -d' ' -f1)"
```

| Agent | Purpose | sha256 digest of your token |
|---|---|---|
| `opencode` | OpenCode / the operator (also OpenViking's VLM) | `<fill in>` |
| `hermes` | the nightly learner | `<fill in>` |
| `dsh-bench` | benchmark runs | `<fill in>` |
| `dsh-job` | background jobs | `<fill in>` |
| `openviking` | L0/L1 summary generation | `<fill in>` |

`serve.toml` (fill the digests and the pool costs):

```toml
base_url = "http://127.0.0.1:18128"      # OmniRoute
api_key = ""                              # OmniRoute bearer, only if REQUIRE_API_KEY=true
tokens = { opencode = "<sha256>", hermes = "<sha256>", dsh-bench = "<sha256>",
           dsh-job = "<sha256>", openviking = "<sha256>" }

[pools.fast]           provider = "fast"           est_cost_micro_usd = 10
[pools.coding-fast]    provider = "coding-fast"    est_cost_micro_usd = 25
[pools.coding-strong]  provider = "coding-strong"  est_cost_micro_usd = 40
[pools.reasoning]      provider = "reasoning"      est_cost_micro_usd = 80

[budget]
per_request = 1000000
per_day = 10000000
```

The `provider` value under each pool is the model name sent to OmniRoute — it must match the combo
name you created in §1.

## 3. OpenViking VLM — `ov.conf`

OpenViking's semantic layer (extracting searchable entities/preferences/events from written memory)
needs a text model. It is pointed at **xcroute** (which routes to OmniRoute), using the `opencode`
token from §2. The dense embedder is already xccode's own TEI — no key needed there.

**Where:** `/etc/xcloud/xccode/ov.conf` — add the `vlm` section (the rest is already written by
`install.sh`):

```json
{
  "server": {"host": "127.0.0.1", "port": 18180},
  "embedding": { "dense": { "provider": "openai", "model": "Qwen/Qwen3-Embedding-0.6B",
                            "api_base": "http://127.0.0.1:18181/v1", "dimension": 1024, "input": "text" } },
  "vlm": { "provider": "openai", "model": "xc/auto",
           "api_base": "http://127.0.0.1:18080/v1", "api_key": "<opencode raw token from §2>" },
  "storage": { "workspace": "/var/lib/xcloud/xccode/openviking",
               "agfs": {"backend": "local"}, "vectordb": {"backend": "local"} }
}
```

## Summary of the values you must supply

1. **OmniRoute provider API keys** (paste in dashboard) — at least one per pool above.
2. **OmniRoute combos** named `fast` / `coding-fast` / `coding-strong` / `reasoning`.
3. **Five xcroute tokens** (raw + sha256) — fill `serve.toml [tokens]`.
4. **Pool cost estimates** — fill `serve.toml [pools.*] est_cost_micro_usd`.
5. **OpenViking VLM `api_key`** — the `opencode` raw token, in `ov.conf`.
