<#
.SYNOPSIS
    Six-point health check for the kirocrew-openai-bridge injection.

.DESCRIPTION
    Most reports are "installed but not active". This walks the whole chain and
    prints a verdict per step so an issue report can be a single paste.

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
$python = $null

function Step($n, $msg) { Write-Host "`n[$n] $msg" -ForegroundColor Cyan }
function Ok($msg) { Write-Host "    OK   $msg" -ForegroundColor Green }
function Bad($msg) { Write-Host "    FAIL $msg" -ForegroundColor Red }
function Info($msg) { Write-Host "    ..   $msg" -ForegroundColor Gray }

function Invoke-PyFile([string]$code, [string]$tag) {
    $path = Join-Path $env:TEMP ("kirocrew-doctor-" + $tag + "-" + [guid]::NewGuid().ToString('N') + ".py")
    Set-Content -Path $path -Value $code -Encoding UTF8
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & $python $path 2>&1
    $ErrorActionPreference = $previous
    Remove-Item $path -Force -ErrorAction SilentlyContinue
    return $output
}

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
if (Test-Path $log) { Get-Content $log -Tail 3 | ForEach-Object { Info $_ } }
else { Bad "no inject.log at $log (the gateway has not picked the hook up yet)" }

# ── 2. child harness processes that should NOT exist ────────────────────────
Step 2 'child harness processes (expect 0)'
foreach ($procName in @('opencode', 'dsh', 'goose')) {
    $count = @(Get-Process -Name $procName -ErrorAction SilentlyContinue).Count
    if ($count -eq 0) { Ok "${procName}: 0" } else { Info "${procName}: $count (a leftover ACP harness)" }
}
$gw = @(Get-CimInstance Win32_Process -Filter "Name='python.exe'" | Where-Object { $_.CommandLine -match 'kiro_crew gateway' })
if ($gw.Count -gt 0) { Ok "gateway running (pid $($gw[0].ProcessId))" } else { Bad 'no kiro_crew gateway process (is the app running?)' }
try {
    $probe = Invoke-WebRequest "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 6
    Ok "dashboard HTTP $($probe.StatusCode) on port $Port"
} catch { Bad "dashboard not answering on port $Port" }

# ── 3. seams the patch relies on ─────────────────────────────────────────────
Step 3 'KiroCrew seams'
$seamProbe = @'
import importlib, sys
sys.path.insert(0, r"__INSTALL_DIR__")
import openai_provider
try:
    import importlib.metadata as md
    print("version " + md.version("kirocrew"))
except Exception:
    print("version ?")
from kiro_crew.config import loader
from kiro_crew.config.loader import KiroCrewConfig
from kiro_crew.session import SessionManager
from kiro_crew.dashboard.handlers import agents
from openai_provider import openai_worker
mod = importlib.import_module("openai_provider.install")
for name, ok in (
    ("loader.build_provider_factory", hasattr(loader, "build_provider_factory")),
    ("KiroCrewConfig.create_provider_factory", hasattr(KiroCrewConfig, "create_provider_factory")),
    ("openai_worker._register_openai_worker", hasattr(openai_worker, "_register_openai_worker")),
    ("agents.api_models", hasattr(agents, "api_models")),
    ("install._build_mcp_executor", hasattr(mod, "_build_mcp_executor")),
):
    print(("ok " if ok else "MISSING ") + name)
print(("ok " if hasattr(SessionManager, "_bg_provider_is_kiro") else "optional-missing ") + "SessionManager._bg_provider_is_kiro")
'@.Replace('__INSTALL_DIR__', $InstallDir)
Invoke-PyFile $seamProbe 'seams' | ForEach-Object {
    $line = "$_"
    if ($line -match '^MISSING') { Bad $line }
    elseif ($line -match '^optional-missing') { Info $line }
    elseif ($line -match '^ok ') { Ok $line }
    else { Info $line }
}

# ── 4. endpoint ──────────────────────────────────────────────────────────────
Step 4 'configured endpoint'
$cfgProbe = @'
import json, sys
sys.path.insert(0, r"__INSTALL_DIR__")
import provider_config
c = provider_config.resolve()
print(json.dumps({"base_url": c["base_url"], "model": c["model"],
                  "api_key": c["api_key"], "sources": c["sources"]}))
'@.Replace('__INSTALL_DIR__', $InstallDir)
$cfgRaw = (Invoke-PyFile $cfgProbe 'config' | Where-Object { $_ -match '^\s*\{' } | Select-Object -Last 1)
try { $cfg = $cfgRaw | ConvertFrom-Json } catch { $cfg = $null }
if (-not $cfg) {
    Bad "could not resolve config: $cfgRaw"
}
else {
    Info "base_url = $($cfg.base_url)  [$($cfg.sources.base_url)]"
    Info "model    = $($cfg.model)  [$($cfg.sources.model)]"
    if ($cfg.api_key) { Ok 'api key present' } else { Bad 'api key missing' }

    $headers = @{}
    if ($cfg.api_key) { $headers['Authorization'] = "Bearer $($cfg.api_key)" }
    try {
        $models = Invoke-WebRequest "$($cfg.base_url)/models" -UseBasicParsing -TimeoutSec 10 -Headers $headers
        Ok "GET /models -> HTTP $($models.StatusCode)"
    } catch { Bad "GET /models -> $($_.Exception.Message)" }

    try {
        $body = @{
            model      = $cfg.model
            messages   = @(@{ role = 'user'; content = 'reply with one word: ok' })
            max_tokens = 16
        } | ConvertTo-Json -Depth 5
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $resp = Invoke-WebRequest "$($cfg.base_url)/chat/completions" -Method Post -Body $body `
            -ContentType 'application/json' -UseBasicParsing -TimeoutSec 90 -Headers $headers
        $sw.Stop()
        Ok "POST /chat/completions -> HTTP $($resp.StatusCode) in $([int]$sw.ElapsedMilliseconds) ms"
    } catch { Bad "POST /chat/completions -> $($_.Exception.Message)" }
}

# ── 5. tool catalogue ────────────────────────────────────────────────────────
Step 5 'tool catalogue'
$toolProbe = @'
import sys
sys.path.insert(0, r"__INSTALL_DIR__")
from openai_provider.mcp_executor import McpToolExecutor
print(len(McpToolExecutor().tool_definitions()))
'@.Replace('__INSTALL_DIR__', $InstallDir)
$toolCount = (Invoke-PyFile $toolProbe 'tools' | Where-Object { $_ -match '^\d+$' } | Select-Object -Last 1)
if ($toolCount -and [int]$toolCount -gt 0) { Ok "tool definitions: $toolCount" }
else { Bad "tool definitions: $toolCount (expected > 0; sessions would run without tools)" }

# ── 6. ledger: served vs requested ───────────────────────────────────────────
Step 6 'ledger (requested vs served)'
$ledger = Get-ChildItem -Path @('D:\', (Join-Path $HOME 'AppData\Local')) -Recurse -Filter 'ledger.db' `
    -ErrorAction SilentlyContinue | Sort-Object LastWriteTime -Descending | Select-Object -First 1
if ($ledger) {
    Info "ledger: $($ledger.FullName)"
    $ledgerProbe = @'
import sqlite3, sys
c = sqlite3.connect(sys.argv[1])
c.row_factory = sqlite3.Row
for row in c.execute("select at, model, requested_model from usage order by id desc limit 5"):
    print(round(row["at"]), "served:", row["model"], "| requested:", row["requested_model"])
'@
    $path = Join-Path $env:TEMP ("kirocrew-doctor-ledger-" + [guid]::NewGuid().ToString('N') + ".py")
    Set-Content -Path $path -Value $ledgerProbe -Encoding UTF8
    & $python $path $ledger.FullName 2>&1 | ForEach-Object { Info $_ }
    Remove-Item $path -Force -ErrorAction SilentlyContinue
}
else { Info 'no hub ledger found - skip this check if you do not use a hub gateway' }

Write-Host "`nDone. Paste this output when reporting an issue." -ForegroundColor Cyan
