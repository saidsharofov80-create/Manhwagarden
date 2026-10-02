# @gardenhwa_bot - NOUTBUK (uch bosqichli: telefon > noutbuk > GitHub, hybrid.py).
#
# Og'ir Cloudflare darvozasi (`manhwa-gate*`) ENDI ISHLATILMAYDI - faqat @manhwatarjima_bot undan
# foydalanadi (foydalanuvchi qarori, 2026-10-02). Noutbuk shu bot uchun DOIMIY zaxira (HEARTBEAT_ROLE=
# laptop): telefon tirik ekan jim turadi, telefon 90 s dan ortiq signal bermasa o'zi yoqiladi.
#
# Ishga tushirish:  powershell -ExecutionPolicy Bypass -File C:\Users\Lenovo\gardenhwa-actions\run-local.ps1
$ErrorActionPreference = 'Continue'
$ProjectDir = 'C:\Users\Lenovo\gardenhwa-actions'
$PythonExe  = 'C:\Users\Lenovo\AppData\Local\Python\pythoncore-3.14-64\python.exe'
$LogDir     = Join-Path $ProjectDir 'logs'
$OutLog     = Join-Path $LogDir 'local.out'
$ErrLog     = Join-Path $LogDir 'local.err'
if (-not (Test-Path $LogDir)) { New-Item -ItemType Directory -Force -Path $LogDir | Out-Null }
Set-Location $ProjectDir

while ($true) {
    Add-Content -Path $OutLog -Value "[$((Get-Date).ToString('o'))] bot ishga tushmoqda..."
    & $PythonExe (Join-Path $ProjectDir 'bot.py') 1>> $OutLog 2>> $ErrLog
    Add-Content -Path $OutLog -Value "[$((Get-Date).ToString('o'))] bot to'xtadi (exit $LASTEXITCODE). 5 s dan keyin qayta."
    Start-Sleep -Seconds 5
}
