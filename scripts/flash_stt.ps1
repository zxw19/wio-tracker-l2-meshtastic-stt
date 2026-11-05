# Flash the STT build to a Wio Tracker L2 Pro.
#   .\flash_stt.ps1                # app + littlefs + model
#   .\flash_stt.ps1 -ModelOnly     # just re-flash the ASR weights
#   .\flash_stt.ps1 -Port COM84
param(
    [string]$Port = "COM83",
    [int]$Baud = 921600,
    [switch]$ModelOnly,
    [switch]$SkipFs
)

$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$fw   = Join-Path $root "research\meshtastic-firmware"
$pio  = Join-Path $env:USERPROFILE ".platformio\penv\Scripts\pio.exe"
$env_name = "seeed_wio_tracker_L2-tft"

# Offsets come from research\meshtastic-firmware\default_16MB_stt.csv
$ModelOffset = "0x950000"
$ModelFile   = Join-Path $root "models\oido_v2b.tnm"

function Invoke-Pio([string[]]$pioArgs) {
    & cmd /c "`"$pio`" $($pioArgs -join ' ')"
    if ($LASTEXITCODE -ne 0) { throw "pio failed: $($pioArgs -join ' ')" }
}

if (-not (Test-Path $ModelFile)) { throw "missing model: $ModelFile" }
"model  : $ModelFile ($((Get-Item $ModelFile).Length) bytes) -> $ModelOffset"

if (-not $ModelOnly) {
    Invoke-Pio @('run','-e',$env_name,'-t','upload',"--upload-port",$Port,"--upload-speed","$Baud")
    if (-not $SkipFs) {
        Invoke-Pio @('run','-e',$env_name,'-t','uploadfs',"--upload-port",$Port,"--upload-speed","$Baud")
    }
}

$esptool = Join-Path $env:USERPROFILE ".platformio\packages\tool-esptoolpy\esptool.py"
& python $esptool --chip esp32s3 --port $Port --baud $Baud write_flash $ModelOffset $ModelFile
if ($LASTEXITCODE -ne 0) { throw "esptool failed to write the model partition" }

"done. Open the serial monitor and look for lines starting with 'STT:'"