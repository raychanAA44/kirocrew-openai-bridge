# Benchmarks

Machine: Windows 11, Ryzen-class desktop, KiroCrew 0.11.1, model served by a local
OpenAI-compatible gateway (`deepseek-v4.1-flash`), 1M-token context advertised.

Method: the dashboard creates a session, one short message is sent, and the timestamp
of the first assistant token is read back from the session transcript. Three runs,
best and median reported. Hub-side prompt/ttft numbers come from the gateway's own
`usage` table.

## First turn after opening a session

| Setup | First turn | Notes |
|---|---|---|
| ACP harness (opencode), pool warm | 3.5 s | 2 runs measured; 13.7 / 15.4 / 48.8 s in a noisy window |
| ACP harness, cold | 13–15 s | one process per session, nothing to reuse |
| **This bridge** | **2.4 s** | no child process; tool catalogue built once |

## Later turns

| Setup | Median |
|---|---|
| ACP harness | 2.2–2.9 s |
| **This bridge** | **2.3 s** |

## Payload and cache

| | ACP harness (opencode) | This bridge |
|---|---|---|
| tools sent | 99 | **125** |
| first prompt | 36,774 tok | 35,392 tok |
| prefix-cache hit | 99.6 % (after pinning TMP) | 98.7 % |
| ttft (model side) | 645–1,020 ms | 632–1,664 ms |

The harness case only reached 99.6 % after pinning `TMP`/`TEMP` to a fixed directory:
opencode wrote a per-session random scratch path into the `bash` tool description, and
`bash` is always `tools[0]`, so the cached prefix broke on the first tool. This bridge
has no such rewriting step.

## Footprint

| | ACP harness | This bridge |
|---|---|---|
| processes per session | 1 | **0** |
| RSS per session | 300–450 MB | — |
| installer leftovers | ~185 MB per process | none |
| cold-start cost after an app restart | 13–15 s for the first session | none |

## Reproducing

1. `doctor.ps1` → confirms the hook, seams, endpoint and tool count.
2. Open a new chat, send one message, wait for the first token.
3. `doctor.ps1` step 6 prints the ledger rows: `served:` vs `requested:`.

Numbers depend heavily on the endpoint; the structural part (0 processes, no cold
start) is what this project changes.
