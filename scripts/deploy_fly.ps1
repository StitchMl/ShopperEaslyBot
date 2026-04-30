param(
    [string]$AppName = "shoppereaslybot",
    [string]$Region = "fra"
)

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)

$fly = Get-Command fly -ErrorAction SilentlyContinue
if (-not $fly) {
    $fly = Get-Command flyctl -ErrorAction SilentlyContinue
}

if (-not $fly) {
    $installedFly = Join-Path $env:USERPROFILE ".fly\bin\flyctl.exe"
    if (Test-Path $installedFly) {
        $flyExe = $installedFly
    } else {
        Write-Host "flyctl non e' installato. Installalo con:"
        Write-Host 'powershell -Command "iwr https://fly.io/install.ps1 -useb | iex"'
        exit 2
    }
} else {
    $flyExe = $fly.Source
}

& $flyExe auth whoami | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Non sei loggato su Fly.io. Esegui: fly auth login"
    exit 2
}

if (-not (Test-Path ".env")) {
    Write-Host "Manca .env. Prima completa: .\.venv\Scripts\python scripts\setup_env.py"
    exit 2
}

$secretKeys = @(
    "TELEGRAM_API_ID",
    "TELEGRAM_API_HASH",
    "TELEGRAM_SESSION",
    "TELEGRAM_BOT_TOKEN",
    "ADMIN_USER_IDS",
    "SOURCE_CHATS",
    "DESTINATION_CHAT",
    "ALLOW_PATTERNS",
    "SKIP_PATTERNS"
)

$secretLines = Get-Content ".env" |
    Where-Object {
        $line = $_.Trim()
        if (-not $line -or $line.StartsWith("#") -or -not $line.Contains("=")) {
            $false
        } else {
            $separator = $line.IndexOf("=")
            $key = $line.Substring(0, $separator).Trim()
            $value = $line.Substring($separator + 1).Trim()
            ($secretKeys -contains $key) -and $value
        }
    }

$required = @("TELEGRAM_API_ID", "TELEGRAM_API_HASH", "TELEGRAM_SESSION", "TELEGRAM_BOT_TOKEN")
foreach ($key in $required) {
    if (-not ($secretLines | Where-Object { $_.StartsWith("$key=") })) {
        Write-Host "Manca $key in .env. Completa il setup e rigenera il token bot."
        exit 2
    }
}

$flyToml = Get-Content "fly.toml" -Raw
$flyToml = $flyToml -replace 'app = ".*"', "app = `"$AppName`""
$flyToml = $flyToml -replace 'primary_region = ".*"', "primary_region = `"$Region`""
Set-Content "fly.toml" $flyToml -Encoding utf8

& $flyExe apps create $AppName --org personal 2>$null

$volumeOutput = & $flyExe volumes list -a $AppName 2>$null
if (($volumeOutput -join "`n") -notmatch "shopperbot_data") {
    & $flyExe volumes create shopperbot_data --size 1 --region $Region -a $AppName --yes
}

$secretText = ($secretLines -join "`n") + "`n"
$secretText | & $flyExe secrets import -a $AppName

& $flyExe deploy -a $AppName --ha=false

Write-Host ""
Write-Host "Deploy completato. Log live:"
Write-Host "fly logs -a $AppName"
