# @gardenhwa_bot ni VAQTINCHA noutbukda ishlatish (2026-10-02 kechasi).
#
# SABAB: Cloudflare bepul tarifining kunlik so'rov limiti (100 000, hisobdagi HAMMA worker uchun
# umumiy) tugadi - darvoza 429 qaytaryapti, shuning uchun bot GitHub Actions'da ishlay olmayapti.
# Limit 00:00 UTC (Toshkent 05:00) da o'zi tiklanadi.
#
# BU SKRIPT NIMA QILADI:
#   1) Telegram webhook'ini o'chiradi (shunda bot xabarlarni to'g'ridan-to'g'ri Telegram'dan oladi
#      va Cloudflare umuman kerak bo'lmaydi; Telegram kutayotgan xabarlarni ham beradi)
#   2) `python bot.py` ni tsiklda ishlatadi - qulasa 5 soniyadan keyin qayta ko'taradi
#   3) 00:00 UTC da to'xtaydi va webhook'ni darvozaga QAYTA o'rnatadi
#      -> bot ODATDAGI yo'liga qaytadi: Cloudflare darvozasi + GitHub Actions, avvalgidek.
#
# Ishga tushirish:  powershell -ExecutionPolicy Bypass -File C:\Users\Lenovo\gardenhwa-actions\run-local.ps1
# To'xtatish:       shu oynani yoping yoki python jarayonini to'xtating (webhook o'zi tiklanadi).
$ErrorActionPreference = 'Continue'

$ProjectDir = 'C:\Users\Lenovo\gardenhwa-actions'
$PythonExe  = 'C:\Users\Lenovo\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$GateHook   = 'https://manhwa-gate.compass-crm.workers.dev/hook'
$SecretsFile = 'C:\Users\Lenovo\manhwa-gate\.secrets.json'
$LogDir     = Join-Path $ProjectDir 'logs'
$OutLog     = Join-Path $LogDir 'local.out'
$ErrLog     = Join-Path $LogDir 'local.err'
# Cloudflare limiti 00:00 UTC da tiklanadi - shu paytda odatdagi yo'lga qaytamiz
$StopUtc    = [datetime]::UtcNow.Date.AddDays(1)

if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
Set-Location $ProjectDir

function Write-Log([string]$msg) {
    $line = "[{0}] {1}" -f (Get-Date).ToString('o'), $msg
    Add-Content -Path $OutLog -Value $line -Encoding utf8
}

# Token .env dan o'qiladi (fayl .gitignore da)
$Token = $null
Get-Content (Join-Path $ProjectDir '.env') | ForEach-Object {
    if ($_ -match '^\s*BOT_TOKEN\s*=\s*(.+)$') { $Token = $Matches[1].Trim() }
}
if (-not $Token) { Write-Log "TO'XTADI: .env da BOT_TOKEN yo'q"; exit 1 }

function Restore-Webhook {
    try {
        $secret = (Get-Content $SecretsFile -Raw | ConvertFrom-Json).TG_SECRET
        $body = @{ url = $GateHook; secret_token = $secret; drop_pending_updates = $false } | ConvertTo-Json
        $r = Invoke-RestMethod "https://api.telegram.org/bot$Token/setWebhook" -Method Post `
                -Body $body -ContentType 'application/json' -TimeoutSec 30
        Write-Log "Webhook darvozaga qaytarildi: ok=$($r.ok)"
    } catch {
        Write-Log "DIQQAT: webhook tiklanmadi - $($_.Exception.Message)"
    }
}

try {
    Write-Log "Boshlandi. 00:00 UTC ($($StopUtc.ToString('u'))) da to'xtaydi va webhook tiklanadi."
    try {
        Invoke-RestMethod "https://api.telegram.org/bot$Token/deleteWebhook?drop_pending_updates=false" -TimeoutSec 30 | Out-Null
        Write-Log "Webhook o'chirildi - xabarlar to'g'ridan-to'g'ri Telegram'dan olinadi."
    } catch {
        Write-Log "TO'XTADI: webhook o'chirilmadi - $($_.Exception.Message)"
        exit 1
    }

    while ([datetime]::UtcNow -lt $StopUtc) {
        Write-Log "Bot ishga tushmoqda..."
        & $PythonExe (Join-Path $ProjectDir 'bot.py') 1>> $OutLog 2>> $ErrLog
        Write-Log "Bot to'xtadi (exit code: $LASTEXITCODE)."
        if ([datetime]::UtcNow -ge $StopUtc) { break }
        Start-Sleep -Seconds 5
    }
    Write-Log "Vaqt tugadi - odatdagi yo'lga qaytilmoqda."
} finally {
    Restore-Webhook
    Write-Log "Tugadi."
}
