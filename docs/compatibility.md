# Compatibility & troubleshooting

## Matrix

| | Status | Notes |
|---|---|---|
| KiroCrew 0.11.x | ✅ verified | 0.11.1 on Windows, `kiro_crew` from `site-packages` |
| KiroCrew ≤ 0.10 | ❓ untested | `provider.stream()` had a different signature; `doctor` reports missing seams |
| Windows 10/11 | ✅ verified | needs admin to write into `Program Files` |
| Linux / macOS | ❓ untested | `install.sh` exists; KiroCrew's sandbox story differs, expect to iterate |
| Endpoints | ✅ any `POST /v1/chat/completions` | OpenAI, Ollama, vLLM, LiteLLM, llama.cpp server, self-hosted gateways |
| Model switching | ✅ works | resets the session (KiroCrew only live-switches `AcpProvider`) |

`doctor.ps1` / `doctor.sh` prints `MISSING <seam>` when the installed KiroCrew no longer
has a seam this bridge patches. That is the signal to re-check after an upgrade.

## Known failure modes

### The app starts but answers are still slow / a harness process appears

`doctor` step 2 shows `opencode: 1` or `dsh: 1`.

- The hook did not run: `inject.log` is missing or its last line is older than the app start.
- Cause is almost always a **KiroCrew upgrade** — it replaced `Lib/site-packages`, which
  removed `hub_boot.pth`. Re-run the installer; it is idempotent.

### `TypeError: stream() got an unexpected keyword argument 'allow_image'`

The vendored provider is older than the installed KiroCrew: newer builds call
`provider.stream(message, allow_image=...)`. Fixed in this bridge (patch 1) and
submitted upstream; if you vendored a newer upstream yourself, re-apply
`patches/0001-*.patch`.

### The first turn of every session has no tools

`McpToolExecutor.tool_definitions()` is synchronous: called from inside a running event
loop it only *schedules* discovery and returns `[]`. Patch 2 awaits the discovery in
`stream()`. Symptom: the model answers but never calls a tool, and `doctor` step 5
reports `tool definitions: 0` when run outside the gateway.

### The UI says model A, the endpoint receives model B

Two causes, both fixed here: the provider had no `set_model()` (so a pick was dropped),
and an over-eager config refresh used to overwrite the model every turn. Verify with
`doctor` step 6 — it prints `served:` vs `requested:` from the gateway ledger.

### `httpx.InvalidURL: Invalid port: ':1]'`

Some Windows machines ship a bare IPv6 literal in `no_proxy` (for example
`localhost,127.0.0.1,::1,[::1]`). httpx parses every entry as `host:port` and dies.
The hook strips bracketed entries before anything else runs. If you call the provider
outside KiroCrew, sanitise the variable yourself.

### Tool calls are refused / the session will not start on Windows

KiroCrew's strong sandbox needs cgroups (Linux). On Windows only backends listed in
`tool_gate._LOCAL_UNENFORCED_BACKENDS` skip enforcement. That is a KiroCrew-side patch,
not part of this bridge — see `docs/architecture.md` and your KiroCrew setup notes.

### A model answers the first turn, then the session hangs (~2 min) and fails

Example: `claude-sonnet-5-5 does not support non-default temperature` (HTTP 400).

Some models reject sampling parameters. The vendored provider used to send
`temperature` on every request; it now sends it **only when a caller passes one**.
If you hit this on another parameter (`top_p`, `max_tokens` on a reasoning model,
...), set it to `null`/omit it in the request path the same way — a 400 that the
gateway queues can look like a hang, because the failure only surfaces when the
client timeout fires.

### The first message after picking a model still uses the old one

KiroCrew applies a model pick at the **turn boundary**: a pick made while a turn is
in flight is committed and applied to the next message. The bridge logs both sides
(`live model switch -> <model>` and `factory session=… -> <model>`), so the ordering
is visible in `inject.log`.

## Getting a useful bug report

```
doctor.ps1            (or ./doctor.sh)
```

plus the last ~20 lines of `inject.log` and the KiroCrew version from step 3. That
covers the whole chain: hook → seams → endpoint → tools → ledger.
