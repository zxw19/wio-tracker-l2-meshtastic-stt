param(
    [string]$Port = "",
    [int]$Baud = 921600
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$bin = Join-Path $root "research\meshtastic-firmware\.pio\build\seeed_wio_tracker_L2-tft\firmware-seeed_wio_tracker_L2-tft-2.8.3.51ac6ea.bin"
$esptool = Join-Path $env:USERPROFILE ".platformio\packages\tool-esptoolpy\esptool.py"

if (-not (Test-Path -LiteralPath $bin)) { throw "firmware not found: $bin" }

$ports = [System.IO.Ports.SerialPort]::GetPortNames()
Write-Host ("serial ports: " + ($(if ($ports.Count) { $ports -join ", " } else { "(none)" })))
if (-not $Port) {
    if ($ports.Count -eq 1) {
        $Port = $ports[0]
    } elseif ($ports.Count -eq 0) {
        throw "no serial port found. Plug the L2 Pro in with a USB *data* cable, then run again."
    } else {
        throw "several ports found. Run: .\flash_app.ps1 -Port COMx"
    }
}

Write-Host "flashing $bin"
Write-Host "        to $Port @ $Baud"
$env:PYTHONUTF8 = "1"
$env:PYTHONIOENCODING = "utf-8"
& python $esptool --chip esp32s3 --port $Port --baud $Baud write_flash 0x10000 $bin
if ($LASTEXITCODE -ne 0) { throw "esptool failed (exit $LASTEXITCODE)" }
Write-Host "done - the board reboots by itself; wait ~25 s for 'STT: ready'"
