<#
.SYNOPSIS
    Point KiroCrew's agent loop at any OpenAI-compatible endpoint.

.DESCRIPTION
    Writes two small files into KiroCrew's bundled Python environment
    (Lib\site-packages\hub_boot.pth and hub_boot.py) plus a support folder with the
    hook, the config resolver and the vendored provider. Nothing else is touched:
    no KiroCrew source file is edited, and -Uninstall removes exactly what this
    script created.

    Needs administrator rights because KiroCrew usually lives under Program Files.

.EXAMPLE
    .\install.ps1 -BaseUrl http://127.0.0.1:8888/v1 -ApiKey sk-xxx -Model ollama/llama3.3:70b

.EXAMPLE
    .\install.ps1 -DryRun

.EXAMPLE
    .\install.ps1 -Uninstall
#>
[CmdletBinding()]
param(
    [string]$CrewBackend,
    [string]$BaseUrl,
    [string]$ApiKey,
    [string]$Model,
    [int]$MaxTokens = 8192,
    [int]$ContextWindow = 128000,
    [string]$InstallDir = (Join-Path $env:LOCALAPPDATA 'kirocrew-openai-bridge'),
    [switch]$DryRun,
    [switch]$Uninstall
)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path

function Say($msg, $color = 'Gray') { Write-Host $msg -ForegroundColor $color }
function Step($msg) { Write-Host "== $msg" -ForegroundColor Cyan }

function Find-CrewBackend {
    param([string]$Explicit)
    $candidates = @()
    if ($Explicit) { $candidates += $Explicit }
    $candidates += @(
        (Join-Path ${env:ProgramFiles} 'KiroCrew\resources\backend-dist\kirocrew-backend'),
        (Join-Path ${env:LOCALAPPDATA} 'Programs\KiroCrew\resources\backend-dist\kirocrew-backend'),
        (Join-Path ${env:ProgramFiles(x86)} 'KiroCrew\resources\backend-dist\kirocrew-backend')
    )
    foreach ($c in $candidates) {
        if ($c -and (Test-Path (Join-Path $c 'python.exe')) -and
            (Test-Path (Join-Path $c 'Lib\site-packages\kiro_crew'))) {
            return (Resolve-Path $c).Path
        }
    }
    throw "KiroCrew backend not found. Pass -CrewBackend '<...>\kirocrew-backend'."
}

$backend = Find-CrewBackend -Explicit $CrewBackend
$sitePackages = Join-Path $backend 'Lib\site-packages'
$python = Join-Path $backend 'python.exe'
$pth = Join-Path $sitePackages 'hub_boot.pth'
$hookModule = Join-Path $sitePackages 'hub_boot.py'

Step "KiroCrew backend"
Say "   $backend"
# Native commands writing to stderr otherwise abort the script when
# $ErrorActionPreference is 'Stop', so the probes run with it relaxed.
function Invoke-Py([string]$code) {
    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { (& $python -c $code 2>$null) } finally { $ErrorActionPreference = $previous }
}
$versionProbe = "import importlib.metadata as md; print(md.version('kirocrew'))"
$crewVersion = (Invoke-Py $versionProbe | Select-Object -Last 1)
if (-not $crewVersion) { $crewVersion = 'unknown (kirocrew distribution not found)' }
Say "   kiro_crew version: $crewVersion"

if ($Uninstall) {
    Step "Uninstall"
    foreach ($f in @($pth, $hookModule)) {
        if (Test-Path $f) {
            if ($DryRun) { Say "   would remove $f" } else { Remove-Item $f -Force; Say "   removed $f" }
        } else { Say "   absent  $f" }
    }
    Say "   support folder kept: $InstallDir (delete it manually if you want)"
    Say "Restart the KiroCrew app to fall back to the ACP backend." 'Yellow'
    return
}

Step "Support folder"
if ($DryRun) { Say "   would populate $InstallDir" }
else {
    New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    Copy-Item (Join-Path $here 'bootstrap\hook.py') $InstallDir -Force
    Copy-Item (Join-Path $here 'bootstrap\provider_config.py') $InstallDir -Force
    Copy-Item (Join-Path $here 'vendor\openai_provider') $InstallDir -Recurse -Force
    Say "   populated $InstallDir"
}

Step "Configuration"
$providerJson = Join-Path $InstallDir 'provider.json'
if ($BaseUrl -or $ApiKey -or $Model) {
    if (-not $BaseUrl -or -not $Model) {
        Say "   -BaseUrl and -Model are both required when configuring" 'Yellow'
        throw "incomplete configuration"
    }
    $cfg = [ordered]@{
        base_url       = $BaseUrl
        api_key        = $ApiKey
        model          = $Model
        max_tokens     = $MaxTokens
        context_window = $ContextWindow
    }
    if ($DryRun) { Say "   would write $providerJson" }
    else {
        ($cfg | ConvertTo-Json) | Set-Content -Path $providerJson -Encoding UTF8
        # Owner-only where the filesystem supports it.
        try { icacls $providerJson /inheritance:r /grant:r "$($env:USERNAME):(R,W)" | Out-Null } catch { }
        Say "   wrote $providerJson (model=$Model)"
    }
}
elseif (Test-Path $providerJson) { Say "   kept existing $providerJson" }
else {
    if ($DryRun) { Say "   would copy config\provider.example.json" }
    else {
        Copy-Item (Join-Path $here 'config\provider.example.json') $providerJson -Force
        Say "   wrote $providerJson from the example - edit it, or re-run with -BaseUrl/-Model" 'Yellow'
    }
}

Step "Injection hook"
$template = Get-Content (Join-Path $here 'bootstrap\hub_boot.py') -Raw
$rendered = $template.Replace('@@BOOTSTRAP_DIR@@', $InstallDir.Replace('\', '\\'))
if ($DryRun) {
    Say "   would write $hookModule"
    Say "   would write $pth"
}
else {
    Set-Content -Path $hookModule -Value $rendered -Encoding UTF8
    Set-Content -Path $pth -Value 'import hub_boot' -Encoding ASCII -NoNewline
    Say "   wrote $hookModule"
    Say "   wrote $pth"
}

Step "Seam check"
$probe = @'
import importlib, sys
sys.path.insert(0, r"__INSTALL_DIR__")
import openai_provider
mod = importlib.import_module("openai_provider.install")
from kiro_crew.providers.registry import ProviderRegistry
assert hasattr(ProviderRegistry, "create_factory"), "ProviderRegistry.create_factory missing"
assert hasattr(mod, "_build_mcp_executor"), "_build_mcp_executor missing"
print("seams ok")
'@.Replace('__INSTALL_DIR__', $InstallDir)
if ($DryRun) { Say "   (skipped in dry run)" }
else {
    $out = & $python -c $probe 2>&1
    if ($LASTEXITCODE -ne 0) { Say "   seam check FAILED:`n$out" 'Red'; throw "incompatible KiroCrew build" }
    Say "   $out"
}

Step "Next"
Say "   1) restart the KiroCrew app (it spawns the backend itself)"
Say "   2) open a new chat; first turn should be a couple of seconds"
Say "   3) run .\doctor.ps1 if anything looks off"
