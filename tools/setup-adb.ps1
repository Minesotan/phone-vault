<#
.SYNOPSIS
    Gets this PC ready to talk to a rooted Android phone, and checks that it can.

.DESCRIPTION
    Installs Google's platform-tools if adb is missing, then walks through the
    checks that actually matter for backups: the device is authorised, root
    works, and the storage paths are readable. Optionally switches the phone
    to adb-over-Wi-Fi so backups can run without a cable.

    Nothing here is destructive. It installs adb and, with -Wireless, tells
    the phone to listen on a TCP port until it next reboots.

.PARAMETER Wireless
    After the checks, enable adb over Wi-Fi and connect to the phone.

.PARAMETER Port
    TCP port for wireless adb. Default 5555.

.EXAMPLE
    .\setup-adb.ps1
    .\setup-adb.ps1 -Wireless
#>

[CmdletBinding()]
param(
    [switch]$Wireless,
    [int]$Port = 5555
)

$ErrorActionPreference = 'Stop'

function Write-Step  { param($m) Write-Host "`n== $m" -ForegroundColor Cyan }
function Write-Good  { param($m) Write-Host "   [ok]   $m" -ForegroundColor Green }
function Write-Warn2 { param($m) Write-Host "   [warn] $m" -ForegroundColor Yellow }
function Write-Bad   { param($m) Write-Host "   [fail] $m" -ForegroundColor Red }

# -- 1. find or install adb ------------------------------------------------

Write-Step "Looking for adb"
$adb = $null
$candidates = @()
$onPath = Get-Command adb -ErrorAction SilentlyContinue
if ($onPath) { $candidates += $onPath.Source }
$candidates += Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages\Google.PlatformTools_Microsoft.Winget.Source_8wekyb3d8bbwe\platform-tools\adb.exe'
$candidates += 'C:\platform-tools\adb.exe'
if ($env:ANDROID_HOME) { $candidates += Join-Path $env:ANDROID_HOME 'platform-tools\adb.exe' }

foreach ($candidate in $candidates) {
    if ($candidate -and (Test-Path $candidate)) { $adb = $candidate; break }
}

if (-not $adb) {
    Write-Warn2 "adb not found - installing Google platform-tools with winget"
    winget install --id Google.PlatformTools -e --accept-source-agreements --accept-package-agreements --disable-interactivity
    $found = Get-ChildItem (Join-Path $env:LOCALAPPDATA 'Microsoft\WinGet\Packages') -Filter adb.exe -Recurse -ErrorAction SilentlyContinue |
             Select-Object -First 1
    if ($found) { $adb = $found.FullName }
}

if (-not $adb) {
    Write-Bad "Could not find or install adb."
    Write-Host "   Install it by hand from https://developer.android.com/tools/releases/platform-tools"
    Write-Host "   then re-run this script, or set ADB_PATH to the executable."
    exit 1
}
Write-Good "adb: $adb"
& $adb version | Select-Object -First 1 | ForEach-Object { Write-Host "   $_" }

# Make it available to phonevault in this session and in future ones.
$env:ADB_PATH = $adb
[Environment]::SetEnvironmentVariable('ADB_PATH', $adb, 'User')
Write-Good "ADB_PATH set for your user account"

# -- 2. the device ---------------------------------------------------------

Write-Step "Looking for a device"
& $adb start-server | Out-Null
$lines = (& $adb devices -l) | Select-Object -Skip 1 | Where-Object { $_.Trim() }

if (-not $lines) {
    Write-Bad "No device detected."
    Write-Host ""
    Write-Host "   On the phone:"
    Write-Host "     1. Settings > About phone > tap 'Build number' seven times"
    Write-Host "     2. Settings > System > Developer options > enable 'USB debugging'"
    Write-Host "     3. Plug in the USB cable, choose 'File transfer' for the USB mode"
    Write-Host "     4. Accept the 'Allow USB debugging' prompt"
    exit 1
}

foreach ($line in $lines) { Write-Host "   $line" }

$serial = $null
foreach ($line in $lines) {
    $parts = $line -split '\s+'
    if ($parts.Length -ge 2 -and $parts[1] -eq 'device') { $serial = $parts[0]; break }
    if ($parts.Length -ge 2 -and $parts[1] -eq 'unauthorized') {
        Write-Bad "Device is connected but not authorised."
        Write-Host "   Unlock the phone and accept 'Allow USB debugging'. Tick"
        Write-Host "   'Always allow from this computer' so scheduled backups work."
        exit 1
    }
}
if (-not $serial) { Write-Bad "No usable device (check the states above)."; exit 1 }
Write-Good "Using device $serial"

$model   = (& $adb -s $serial shell getprop ro.product.model).Trim()
$release = (& $adb -s $serial shell getprop ro.build.version.release).Trim()
Write-Good "$model, Android $release"

# -- 3. root ---------------------------------------------------------------

Write-Step "Checking root"
$id = (& $adb -s $serial exec-out "su -c id" 2>$null)
if ($id -match 'uid=0') {
    Write-Good "root works (su granted)"
    Write-Host "   /data can be backed up."
} else {
    Write-Warn2 "root not available."
    Write-Host "   /sdcard will still back up fine; /data will not be readable."
    Write-Host "   If the phone is rooted, unlock it and approve the su request"
    Write-Host "   (Magisk shows a prompt), then re-run this script."
    Write-Host "   In Magisk, grant 'shell' permanent access to avoid prompts during"
    Write-Host "   scheduled backups."
}

# -- 4. what the backup will need ------------------------------------------

Write-Step "Checking the device tools phonevault relies on"
foreach ($tool in @('stat', 'find', 'tar', 'base64', 'md5sum')) {
    $probe = (& $adb -s $serial exec-out "command -v $tool" 2>$null)
    if ($probe) { Write-Good "$tool" } else { Write-Warn2 "$tool missing (phonevault will fall back)" }
}

Write-Step "Storage"
& $adb -s $serial exec-out "df -h /data /storage/emulated 2>/dev/null" | ForEach-Object { Write-Host "   $_" }

Write-Step "Sample listing of /sdcard"
& $adb -s $serial exec-out "ls /sdcard | head -15" | ForEach-Object { Write-Host "   $_" }

# -- 5. wireless -----------------------------------------------------------

if ($Wireless) {
    Write-Step "Switching to adb over Wi-Fi"
    $ip = (& $adb -s $serial exec-out "ip -f inet addr show wlan0 2>/dev/null | grep -o 'inet [0-9.]*' | cut -d' ' -f2").Trim()
    if (-not $ip) {
        Write-Bad "Could not read the phone's Wi-Fi address. Is it on Wi-Fi (not just mobile data)?"
    } else {
        & $adb -s $serial tcpip $Port | ForEach-Object { Write-Host "   $_" }
        Start-Sleep -Seconds 2
        $endpoint = "${ip}:${Port}"
        $result = & $adb connect $endpoint
        Write-Host "   $result"
        if ($result -match 'connected') {
            Write-Good "Reachable at $endpoint without a cable"
            Write-Host "   Pin it into your profile:"
            Write-Host "     phonevault config set device $endpoint" -ForegroundColor White
            Write-Host "   Note: this lasts until the phone reboots. Re-run with -Wireless after a reboot,"
            Write-Host "   or use 'phonevault wifi' which does the same thing."
        } else {
            Write-Warn2 "Could not connect to $endpoint"
        }
    }
}

# -- done ------------------------------------------------------------------

Write-Step "Ready"
Write-Host @"
   Next steps:

     phonevault init --repository \\NAS\backup\phone --save
     phonevault backup --preset media --dry-run
     phonevault backup
     phonevault ui

   Replace the repository path with your own NAS share. Map it in Explorer
   first if it needs credentials, or use an smb:// URL and set
   PHONEVAULT_SMB_PASSWORD.
"@
