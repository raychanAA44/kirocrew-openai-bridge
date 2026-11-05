# kirocrew-openai-bridge

> **Unofficial.** Not affiliated with, endorsed by, or supported by KiroCrew.
> A community patch set that points KiroCrew's agent loop at **any OpenAI-compatible
> endpoint** — your own gateway, Ollama, vLLM, LiteLLM, OpenAI itself — with
> **zero ACP child processes**.

## Why

KiroCrew is a control plane (sessions, memory, skills, cron, approvals, ledger) that
delegates the actual agent loop to an external harness over ACP. That harness is a
separate process per session, which costs:

| With an ACP harness (e.g. opencode) | With this bridge |
|---|---|
| 13–15 s cold start per session | **0** (nothing to cold-start) |
| 300–450 MB per session | **0** |
| tool schema re-projected through a third party | tools come from KiroCrew's own registry |
| ~185 MB of installer leftovers per process | none |

Measured on KiroCrew 0.11.1, Windows, DeepSeek-v4.1-flash behind a local gateway:

| | ACP harness | **this bridge** |
|---|---|---|
| first turn | 3.5 s (pool warm) / 13–15 s (cold) | **2.4 s** |
| later turns (median) | 2.2–2.9 s | **2.3 s** |
| tools sent | 99 | **125** |
| prompt, first turn | 36,774 tok | 35,392 tok |
| prefix-cache hit | 99.6 % | 98.7 % |
| child processes | 1 per session | **0** |

## What it does

KiroCrew builds its provider through a few well-known seams. This project patches
those seams **at runtime** — not a fork, no source edits to KiroCrew:

```
KiroCrew backend (python -m kiro_crew gateway)
  └─ site-packages/hub_boot.pth  →  hub_boot.py  →  bootstrap/sitecustomize.py
        └─ openai_provider.install()      # swaps ProviderRegistry.create_factory
        └─ provider_config.resolve()      # where to point, which model
  └─ OpenAIProvider (in-process)  →  POST {base_url}/chat/completions
```

The hook only acts for the `gateway` command, so `mcp-core`, `mcp-cron`, pool workers
and every other Python process on the machine are untouched.

## Install

```powershell
# Windows (needs admin: it writes two files into KiroCrew's site-packages)
powershell -ExecutionPolicy Bypass -File .\install.ps1 `
  -BaseUrl https://gw.rayyy.me:9443/v1 -ApiKey $env:KWSA_KEY -Model oc/beam-free `
  -FallbackBaseUrl http://127.0.0.1:28791/v1

# then restart the KiroCrew desktop app and open a new chat
```

```bash
# Linux / macOS (untested — see docs/compatibility.md)
sudo ./install.sh --base-url https://gw.rayyy.me:9443/v1 --api-key "$KWSA_KEY" \
  --model oc/beam-free --fallback-base-url http://127.0.0.1:28791/v1
```

`-BaseUrl` is the endpoint chat goes to; `-FallbackBaseUrl` is only read when the
model catalog cannot be fetched from the primary, so a dead primary costs a
refresh instead of an empty picker. Both are optional flags — omit them and the
installer writes no `provider.json`, leaving an existing one alone.

The two values that used to be printed here (`127.0.0.1:8888` and `gpt-4o-mini`)
were a trap: 8888 is a different gateway that rejects this kind of key with 401,
and no gateway on this deployment serves `gpt-4o-mini`, so the very first request
after a clean install failed with "Model is unavailable" and nothing said why.

Dry run, diagnose, uninstall:

```powershell
.\install.ps1 -DryRun            # show what would change
.\doctor.ps1                     # 6-point health check (see below)
.\install.ps1 -Uninstall         # remove the two hook files
```

## `doctor`

Most issues are "installed but not active". One command answers for all of them:

```
[1] hook       hub_boot.pth + hub_boot.py present?  last inject.log line?
[2] processes  opencode / dsh / goose child processes (expect 0)
[3] seams      KiroCrew version + the patched seams still exist?
[4] endpoint   GET /v1/models and one timed chat completion
[5] tools      tool-definition count (must be > 0)
[6] ledger     last requests: requested model == the model the UI shows?
```

Point 6 is what catches the classic failure: the UI says model A while every
request goes to model B.

Real output from the dogfood machine (KiroCrew 0.11.1, Windows):

```
[3] KiroCrew seams
    ..   version 0.11.1
    OK   ok loader.build_provider_factory
    OK   ok KiroCrewConfig.create_provider_factory
    OK   ok openai_worker._register_openai_worker
    OK   ok agents.api_models
    OK   ok install._build_mcp_executor
    ..   optional-missing SessionManager._bg_provider_is_kiro
[4] configured endpoint
    OK   GET /models -> HTTP 200
    OK   POST /chat/completions -> HTTP 200 in 5393 ms
[5] tool catalogue
    OK   tool definitions: 125
[6] ledger (requested vs served)
    ..   served: gpt-6.1-sol | requested: regptplus/gpt-6.1-sol
```

The one `optional-missing` seam means background helpers (auto-title, link summary)
keep their default path; sessions themselves are unaffected, and the installer
reports it as a degradation rather than a failure.

## Compatibility

| | Status |
|---|---|
| KiroCrew 0.11.x | ✅ verified (Windows, 0.11.1) |
| KiroCrew ≤ 0.10 | ❓ untested |
| Windows | ✅ verified |
| Linux / macOS | ❓ untested — `install.sh` exists but the sandbox story differs |
| Endpoints | anything speaking `POST /v1/chat/completions`: OpenAI, Ollama, vLLM, LiteLLM, llama.cpp server, your own gateway |

Model switching from the dashboard **works**, but note the semantics: KiroCrew only
does a live in-session switch for `AcpProvider`; for this bridge it resets the
session, so the new model applies to the fresh session (the transcript is replayed).

## Configure

Two ways, in this order of precedence:

1. **Environment** (recommended): `OPENAI_BASE_URL`, `OPENAI_API_KEY`, `OPENAI_MODEL`,
   `OPENAI_MAX_TOKENS`, `OPENAI_CONTEXT_WINDOW`.
2. `bootstrap/provider.json` (written by `install.ps1`), or — if KiroCrew already has
   one — the ids it manages itself (`~/.config/opencode/opencode.json` `provider.hub-neo.options`
   for `baseURL`/`apiKey` and `~/.kiro/crew/config.json` `agent.model`). See
   `docs/architecture.md` for why that read-through exists.

## Safety

- The hook never raises into KiroCrew: a broken injection logs and leaves the backend
  to start normally (which then uses its configured ACP backend).
- Secrets: prefer environment variables or an OS keyring. If you let the installer write
  `provider.json`, it is created with owner-only permissions where the platform allows it.
- Uninstall restores the previous state: two files removed, nothing else touched.

## Upstream

The provider itself is [`lenovo1996/KiroCrew-OpenAI-Compatible`](https://github.com/lenovo1996/KiroCrew-OpenAI-Compatible)
(MIT). A snapshot lives in `vendor/`; the fixes this bridge needs are queued there as a
PR, and `patches/` keeps them replayable until they land.

## License

MIT — see [LICENSE](LICENSE) and [NOTICE](NOTICE).
