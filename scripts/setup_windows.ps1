# setup_windows.ps1 — Одноразове налаштування Windows ХОСТА (все САМО).
# Запуск (права адміна ТІЛЬКИ для testsigning/драйвера, решта без адміна):
#   powershell -ExecutionPolicy Bypass -File scripts/setup_windows.ps1
# Що робить САМ:
#   1. winget install: CMake, PlatformTools (adb), scrcpy
#   2. Перевірка testsigning (підказка команди, reboot НЕ робить сам без --allow-reboot)
#   3. Скачування+встановлення usbip-win2 (vhci.sys) якщо його нема
#   4. adb start-server + adb devices (перевірка)

param(
  [switch]$AllowReboot,
  [switch]$SkipDriver,
  [string]$UsbIpVersion = "latest"
)

$ErrorActionPreference = "Continue"
function Step($msg) { Write-Host "`n[AUTO] $msg" -ForegroundColor Cyan }
function Ok($msg)   { Write-Host "[AUTO] OK: $msg" -ForegroundColor Green }
function Warn($msg) { Write-Host "[AUTO] WARN: $msg" -ForegroundColor Yellow }

Step "Step 1/4: dependencies via winget (cmake, adb, scrcpy)..."
$pkgs = @(
  @{ Id = "Kitware.CMake"; Name = "cmake" },
  @{ Id = "Google.PlatformTools"; Name = "adb" },
  @{ Id = "Genymobile.scrcpy"; Name = "scrcpy" }
)
foreach ($p in $pkgs) {
  if (Get-Command $p.Name -ErrorAction SilentlyContinue) { Ok "$($p.Name) already present."; continue }
  Write-Host "[AUTO] `$ winget install $($p.Id) ..." -ForegroundColor Gray
  winget install --accept-source-agreements --accept-package-agreements -e --id $p.Id
  if (Get-Command $p.Name -ErrorAction SilentlyContinue) { Ok "$($p.Name) installed." }
  else { Warn "$($p.Name) did not appear in PATH. Reopen the terminal." }
}

Step "Step 2/4: Test Signing (required for usbip-win2 without an EV certificate)..."
$ts = (bcdedit /enum '{current}' | Select-String "testsigning") -join " "
Write-Host "[AUTO]   | $ts" -ForegroundColor Gray
if ($ts -match "Yes") { Ok "Test Signing ON." }
else {
  Warn "Test Signing OFF."
  Write-Host "[AUTO] Enable MANUALLY (needs ADMIN + reboot):" -ForegroundColor Yellow
  Write-Host "  bcdedit /set testsigning on" -ForegroundColor White
  $isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
    ).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
  if ($isAdmin) {
    Write-Host "[AUTO] `$ bcdedit /set testsigning on ..." -ForegroundColor Gray
    bcdedit /set testsigning on
    Warn "REBOOT required for Test Signing to take effect."
    if ($AllowReboot) { Restart-Computer -Force }
  } else {
    Warn "Run this script as Administrator to enable automatically."
  }
}

Step "Step 3/4: usbip-win2 driver (vhci.sys)..."
if ($SkipDriver) { Warn "Skipped (--SkipDriver)."; }
else {
  $vhci = $false
  foreach ($dev in "\\.\vhci", "\\.\USBIP_VHCI") {
    # Перевірка через наявність сервісу/файлу драйвера
    if (Test-Path $dev) { $vhci = $true }
  }
  $svc = Get-Service -Name "vhci" -ErrorAction SilentlyContinue
  if ($svc) { $vhci = $true; Ok "vhci service found ($($svc.Status))." }
  if ($vhci) { Ok "VHCI driver already installed." }
  else {
    Write-Host "[AUTO] No driver. Downloading usbip-win2 from GitHub Releases..." -ForegroundColor Gray
    try {
      $api = Invoke-RestMethod "https://api.github.com/repos/vadimgrn/usbip-win2/releases/latest" -TimeoutSec 30
      $msi = $api.assets | Where-Object { $_.name -match "\.msi$" } | Select-Object -First 1
      if (-not $msi) { throw "MSI not found in latest release" }
      $tmp = Join-Path $env:TEMP $msi.name
      Write-Host "[AUTO] `$ Invoke-WebRequest $($msi.browser_download_url) -> $tmp" -ForegroundColor Gray
      Invoke-WebRequest -Uri $msi.browser_download_url -OutFile $tmp -TimeoutSec 120
      Write-Host "[AUTO] `$ msiexec /i $tmp /qn ..." -ForegroundColor Gray
      Start-Process msiexec -ArgumentList "/i `"$tmp`" /qn /norestart" -Wait
      Ok "usbip-win2 installed (a reboot may be needed due to Test Signing)."
    } catch {
      Warn "Failed to install automatically: $($_.Exception.Message)"
      Write-Host "Install manually: https://github.com/vadimgrn/usbip-win2/releases" -ForegroundColor Yellow
    }
  }
}

Step "Step 4/4: adb start-server + adb devices (check)..."
Write-Host "[AUTO] `$ adb start-server" -ForegroundColor Gray
adb start-server
Write-Host "[AUTO] `$ adb devices -l" -ForegroundColor Gray
adb devices -l

Write-Host "`n[AUTO] ===== Done. Next just: START.bat  (or python tools/auto_run.py) =====" -ForegroundColor Green
