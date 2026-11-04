# Contributing

Small project, two rules: **evidence first**, and **never break the host app**.

## Before you send a patch

1. `python -m pytest -q tests` must stay green. The tests deliberately do not
   import KiroCrew, so this works on a bare Python install.
2. `python -m compileall -q bootstrap vendor` must stay clean.
3. Shell scripts: `bash -n install.sh doctor.sh` / parse `install.ps1` and
   `doctor.ps1` (CI does both on every push).

## The one hard rule

The injection hook runs **inside KiroCrew's backend process**. If it raises, the app
fails to start, or worse, starts and misbehaves in a way that looks like a KiroCrew
bug. So:

- anything the hook patches is wrapped in `try/except` and logged to `inject.log`;
- a failure degrades (feature off) but never blocks a session from being created;
- new patched seams get a check in `doctor.ps1` / `doctor.sh`.

## Reporting a bug

Paste the output of `doctor.ps1` (or `./doctor.sh`) plus the last ~20 lines of
`inject.log`. That covers the whole chain: hook → seams → endpoint → tools → ledger.
Without those two, the first useful answer is going to be "please run doctor".

## Updating the vendored provider

`vendor/openai_provider/` is a snapshot of
[`lenovo1996/KiroCrew-OpenAI-Compatible`](https://github.com/lenovo1996/KiroCrew-OpenAI-Compatible)
(MIT). To refresh it:

```bash
git clone https://github.com/lenovo1996/KiroCrew-OpenAI-Compatible /tmp/upstream
rsync -a --exclude __pycache__ /tmp/upstream/openai_provider/ vendor/openai_provider/
git apply -p1 --directory=vendor patches/*.patch   # our fixes, until they land upstream
```

If `patches/*.patch` no longer applies, upstream changed the same lines: re-read the
patch, port the intent by hand, and note it in `NOTICE`.
