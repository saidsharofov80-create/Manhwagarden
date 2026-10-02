# @gardenhwa_bot webhook'ini Cloudflare darvozasiga qaytaradi (zaxira chorasi, 2026-10-02).
# run-local.ps1 buni to'xtaganda o'zi qiladi; bu skript esa noutbuk o'chib qolgan holat uchun -
# rejali vazifa (ManhwaGardenWebhook) uni 05:10 da, noutbuk o'chiq bo'lsa yoqilishi bilan ishlatadi.
# Qo'lda:  powershell -ExecutionPolicy Bypass -File C:\Users\Lenovo\gardenhwa-actions\restore-webhook.ps1
$ErrorActionPreference = 'Stop'
$GateHook = 'https://manhwa-gate.compass-crm.workers.dev/hook'
$Token = $null
Get-Content 'C:\Users\Lenovo\gardenhwa-actions\.env' | ForEach-Object {
    if ($_ -match '^\s*BOT_TOKEN\s*=\s*(.+)$') { $Token = $Matches[1].Trim() }
}
if (-not $Token) { throw ".env da BOT_TOKEN yo'q" }
$info = Invoke-RestMethod "https://api.telegram.org/bot$Token/getWebhookInfo" -TimeoutSec 30
if ($info.result.url -eq $GateHook) { "Webhook allaqachon joyida - tegilmadi."; exit 0 }
$secret = (Get-Content 'C:\Users\Lenovo\manhwa-gate\.secrets.json' -Raw | ConvertFrom-Json).TG_SECRET
$body = @{ url = $GateHook; secret_token = $secret; drop_pending_updates = $false } | ConvertTo-Json
$r = Invoke-RestMethod "https://api.telegram.org/bot$Token/setWebhook" -Method Post -Body $body `
        -ContentType 'application/json' -TimeoutSec 30
"Webhook qaytarildi: ok=$($r.ok)"
