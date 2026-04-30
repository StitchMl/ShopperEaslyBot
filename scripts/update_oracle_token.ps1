param(
    [string]$HostName = "89.168.17.0",
    [string]$SshUser = "opc",
    [string]$KeyPath = "$env:USERPROFILE\.ssh\oci_shopper",
    [string]$RemoteDir = "/opt/shopper-easly-bot"
)

$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$envPath = Join-Path $projectRoot ".env"

if (-not (Test-Path $envPath)) {
    throw "Manca .env. Prima completa scripts\setup_env.py."
}

$secureToken = Read-Host "Incolla il NUOVO TELEGRAM_BOT_TOKEN rigenerato con BotFather" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
try {
    $token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
} finally {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
}

if ($token -notmatch '^\d{8,10}:[A-Za-z0-9_-]{30,}$') {
    throw "Il token non ha il formato atteso. Rigeneralo con BotFather e riprova."
}

$lines = [System.Collections.Generic.List[string]]::new()
$found = $false
foreach ($line in [System.IO.File]::ReadAllLines($envPath)) {
    if ($line -match '^TELEGRAM_BOT_TOKEN=') {
        $lines.Add("TELEGRAM_BOT_TOKEN=$token")
        $found = $true
    } else {
        $lines.Add($line)
    }
}
if (-not $found) {
    $lines.Add("TELEGRAM_BOT_TOKEN=$token")
}

$utf8NoBom = [System.Text.UTF8Encoding]::new($false)
[System.IO.File]::WriteAllLines($envPath, $lines, $utf8NoBom)

$remote = "${SshUser}@${HostName}"
scp -i $KeyPath $envPath "${remote}:~/shopper-easly.env"
ssh -i $KeyPath $remote "sudo cp ~/shopper-easly.env $RemoteDir/.env && sudo chown opc:opc $RemoteDir/.env && sudo restorecon $RemoteDir/.env || true && sudo systemctl restart shopper-easly-bot.service && sleep 8 && sudo systemctl status shopper-easly-bot.service --no-pager --full || true && sudo journalctl -u shopper-easly-bot.service -n 80 --no-pager"
