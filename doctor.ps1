<#
.SYNOPSIS
    Six-point health check for the kirocrew-openai-bridge injection.

.DESCRIPTION
    Most reports are "installed but not active". This walks the whole chain and
    prints a verdict per step, so an issue report can be one paste.

.EXAMPLE
    .\doctor.ps1
#>
[CmdletBinding()]
param(
    [string]$CrewBackend,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'kirocrew-openai-bridge'),
    [int]$Port = 5476
)

$ErrorActionPreference = 'Continue'

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    OK   $msg" -ForegroundColor Green }
function Bad($msg) { Write-Host "    FAIL $msg" -ForegroundColor Red }
function Info($msg) { Write-Host "    ..   $msg" -ForegroundColor Gray }

# ── locate the backend ───────────────────────────────────────────────────────
$candidates = @()
if ($CrewBackend) { $candidates += $CrewBackend }
$candidates += @(
    (Join-Path ${env:ProgramFiles} 'KiroCrew\resources\backend-dist\kirocrew-backend'),
    (Join-Path $env:LOCALAPPDATA 'Programs\KiroCrew\resources\backend-dist\kirocrew-backend')
)
$backend = $candidates | Where-Object { $_ -and (Test-Path (Join-Path $_ 'python.exe')) } | Select-Object -First 1
if (-not $backend) { Bad 'KiroCrew backend not found'; exit 1 }
$python = Join-Path $backend 'python.exe'
$sitePackages = Join-Path $backend 'Lib\site-packages'

# ── 1. hook files + last injection ───────────────────────────────────────────
Step 1 'injection hook'
$pth = Join-Path $sitePackages 'hub_boot.pth'
$hook = Join-Path $sitePackages 'hub_boot.py'
if (Test-Path $pth) { Ok "pth present: $pth" } else { Bad "missing $pth (run install.ps1)" }
if (Test-Path $hook) { Ok "module present: $hook" } else { Bad "missing $hook" }
$log = Join-Path $InstallDir 'inject.log'
if (Test-Path $log) {
    $tail = Get-Content $log -Tail 3
    $tail | ForEach-Object { Info $_ }
} else { Bad "no inject.log at $log (the gateway has not picked the hook up yet)" }

# ── 2. child processes that should NOT exist ────────────────────────────────
Step 2 'child harness processes (expect 0)'
foreach ($name in @('opencode', 'dsh', 'goose')) {
    $n = @(Get-Process -Name $name -ErrorAction SilentlyContinue).Count
    if ($n -eq 0) { Ok "$name: 0" } else { Info "$name: $n (a leftover ACP harness; harmless but it means the old backend is still in use somewhere)" }
}
$gw = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" |
        Where-Object { $_.CommandLine -match 'kiro_crew gateway' })
if ($gw.Count -gt 0) { Ok "gateway running (pid $($gw[0].ProcessId))" } else { Bad 'no kiro_crew gateway process (is the app running?)' }

# ── 3. seams the patch relies on ─────────────────────────────────────────────
Step 3 'KiroCrew seams'
$probe = @'
import importlib, sys
sys.path.insert(0, r"__INSTALL_DIR__")
import kiro_crew
try:
    import importlib.metadata as md
    ver = md.version("kiro-crew")
except Exception:
    ver = getattr(kiro_crew, "__version__", "?")
from kiro_crew.providers.registry import ProviderRegistry
import openai_provider
mod = importlib.import_module("openai_provider.install")
checks = {
    "ProviderRegistry.create_factory": hasattr(ProviderRegistry, "create_factory"),
    "install._build_mcp_executor": hasattr(mod, "_build_mcp_executor"),
    "install._patch_provider_factory": hasattr(mod, "_patch_provider_factory"),
}
print("version", ver)
for k, v in checks.items():
    print(("ok" if v else "MISSING"), k)
'@.Replace('__INSTALL_DIR__', $InstallDir)
$out = & $python -c $probe 2>&1
$out | ForEach-Object {
    $line = "$_"
    if ($line -match '^MISSING') { Bad $line } elseif ($line -match '^ok ') { Ok $line } else { Info $line }
}

# ── 4. endpoint ──────────────────────────────────────────────────────────────
Step 4 'configured endpoint'
$cfgProbe = @'
import json, sys
sys.path.insert(0, r"__INSTALL_DIR__")
import provider_config
c = provider_config.resolve()
print(json.dumps({"base_url": c["base_url"], "model": c["model"], "key": bool(c["api_key"]),
                  "sources": c["sources"]}))
'@.Replace('__INSTALL_DIR__', $InstallDir)
$cfgRaw = (& $python -c $cfgProbe 2>&1) -join ''
try { $cfg = $cfgRaw | ConvertFrom-Json } catch { $cfg = $null }
if (-not $cfg) { Bad "could not resolve config: $cfgRaw" }
else {
    Info "base_url = $($cfg.base_url)  [$($cfg.sources.base_url)]"
    Info "model    = $($cfg.model)  [$($cfg.sources.model)]"
    if ($cfg.key) { Ok 'api key present' } else { Bad 'api key missing' }
    try {
        $models = Invoke-WebRequest "$($cfg.base_url)/models" -UseBasicParsing -TimeoutSec 10 `
            -Headers @{ Authorization = 'Bearer ' + (& $python -c $cfgProbe > $null; '') }
        Info "GET /models -> HTTP $($models.StatusCode)"
    } catch { Info "GET /models -> $($_.Exception.Message)" }
    try {
        $body = @{ model = $cfg.model; messages = @(@{ role = 'user'; content = 'reply with one word: ok' }); max_tokens = 16 } | ConvertTo-Json -Depth 5
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $r = Invoke-WebRequest "$($cfg.base_url)/chat/completions" -Method Post -Body $body -ContentType 'application/json' -UseBasicParsing -TimeoutSec 60
        $sw.Stop()
        Ok "POST /chat/completions -> HTTP $($r.StatusCode) in $([int]$sw.ElapsedMilliseconds) ms"
    } catch { Bad "POST /chat/completions -> $($_.Exception.Message)" }
}

# ── 5. tool catalogue ────────────────────────────────────────────────────────
Step 5 'tool catalogue'
$toolProbe = @'
import sys
sys.path.insert(0, r"__INSTALL_DIR__")
from openai_provider.mcp_executor import McpToolExecutor
ex = McpToolExecutor()
defs = ex.tool_definitions()
print(len(defs))
'@.Replace('__INSTALL_DIR__', $InstallDir)
$n = (& $python -c $toolProbe 2>&1) -join ''
if ($n -match '^\d+$' -and [int]$n -gt 0) { Ok "tool definitions: $n" }
else { Bad "tool definitions: $n (expected > 0; a session would run without tools)" }

# ── 6. ledger: does the served model match the requested one? ───────────────
Step 6 'ledger (requested vs served)'
$ledger = Get-ChildItem 'D:\' -Recurse -Filter 'ledger.db' -ErrorAction SilentlyContinue |
          Where-Object { $_.FullName -match 'hub' } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($ledger) {
    Info "ledger: $($ledger.FullName)"
    $q = @"
import sqlite3
c = sqlite3.connect(r"$($ledger.FullName)")
c.row_factory = sqlite3.Row
for r in c.execute("select at, model, requested_model from usage order by id desc limit 5"):
    print(round(r["at"]), "served:", r["model"], "| requested:", r["requested_model"])
"@
    & $python -c $q 2>&1 | ForEach-Object { Info $_ }
} else { Info 'no hub ledger found - skip this check if you do not use a hub gateway' }

Write-Host "`nDone. Paste this output when reporting an issue." -ForegroundColor Cyan
