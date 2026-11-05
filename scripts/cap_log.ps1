param(
    [string]$Port = "",
    [int]$Seconds = 90,
    [string]$Out = "stt_run3.log"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $MyInvocation.MyCommand.Path
$script = Join-Path $root "cap_stt.py"
$ports = [System.IO.Ports.SerialPort]::GetPortNames()
if (-not $Port) {
    if ($ports.Count -eq 1) { $Port = $ports[0] }
    elseif ($ports.Count -eq 0) { throw "no serial port found - is the board plugged in?" }
    else { throw "several ports found. Run: .\cap_log.ps1 -Port COMx" }
}
Set-Location $root
& python $script $Port $Seconds $Out
