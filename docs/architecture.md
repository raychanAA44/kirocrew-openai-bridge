# How it works

## The problem in one picture

```
KiroCrew is a control plane: sessions, memory, skills, cron, approvals, ledger.
Its agent loop is delegated over ACP to a harness process — one process per session.

  KiroCrew  ── ACP (stdio JSON-RPC) ──▶  opencode.exe / kiro-cli / dsh  ──▶  model API
                                          ^ 13–15 s cold start, 300–450 MB,
                                            tool schema re-projected, cache broken
```

This bridge removes the middle box:

```
  KiroCrew  ── in-process OpenAIProvider ──▶  model API
              (agent loop + tool calls happen inside KiroCrew's own process)
```

## Where the injection happens

The desktop shell starts the backend itself:

```
python.exe -s -P -m kiro_crew gateway --no-open --port 5476
```

`-s` drops *user* site-packages and `-P` drops the script directory — but neither
ignores the main `site-packages`, and `site` imports every `.pth` file it finds
there. That is the seam:

```
Lib/site-packages/hub_boot.pth        "import hub_boot"
Lib/site-packages/hub_boot.py         -> runs bootstrap/hook.py
bootstrap/hook.py                     -> openai_provider.install()
bootstrap/provider_config.py          -> where to point, which model
bootstrap/openai_provider/            -> the vendored provider (patched)
```

Guards, all verified:

| Guard | Effect |
|---|---|
| `HUB_BRIDGE=0` | nothing runs |
| `"gateway" not in sys.argv` | `mcp-core`, `mcp-cron`, pool workers, CLI chats stay untouched |
| any exception | logged to `inject.log`, the backend starts normally |

Environment-variable injection (`PYTHONPATH`) is **not** an option here: the desktop
shell hands the backend a curated environment, so a user-level `PYTHONPATH` never
reaches it. That is why a `.pth` inside the application's own `site-packages` is used
instead — and why installing needs administrator rights on Windows.

## What gets patched inside KiroCrew

| Seam | Why |
|---|---|
| `ProviderRegistry.create_factory` | every session is built by this factory; returning `OpenAIProvider` removes ACP |
| `KiroCrewConfig.create_provider_factory` | the config layer has its own entry point; missing it leaves some sessions on ACP |
| `LLMPool._create_worker` | background work (memory extraction, summaries) would otherwise spawn a harness |
| `SessionManager._bg_provider_is_kiro` | switches that background path off for good |
| `/api/models` | the model picker should list what the endpoint actually serves |

## The tool catalogue

`McpToolExecutor` turns KiroCrew's own MCP registry into OpenAI `tools`. Nothing is
re-projected through a third party, which is why the bridge sends **125** tools where
the opencode harness sent 99 (opencode drops the ones it cannot express and ignores
`agent.tool_search`, see `docs/compatibility.md`).

Building that catalogue costs ~3.7 s. `bootstrap/hook.py` builds it **once per
process** and shares the instance: the executor holds a lock and two caches, no
per-session state.

## Model selection semantics

- The dashboard picker calls `provider.set_model(...)`. KiroCrew only performs a
  *live* in-session switch for `AcpProvider`; for this bridge it falls back to
  resetting the session, so the pick applies to the fresh session (the transcript is
  replayed from the compaction snapshot).
- `provider_config` deliberately does **not** rewrite the model on every turn: the
  model belongs to the session. Only `base_url` / `api_key` are refreshed per turn, so
  editing the endpoint takes effect without a restart.
