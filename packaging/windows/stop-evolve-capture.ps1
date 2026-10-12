# Stops the EvolveCapture.exe that runs from one install folder. The NSIS
# installer and uninstaller run it before they touch files.
#
# It only looks at processes whose full path is -ExePath. A dev instance
# (python.exe) or a EvolveCapture.exe from another folder is never touched.
#
# 1. Ask each one to stop cleanly: set the event Local\EvolveCapture-Quit-<pid>
#    that packaging/entry.py waits on (same path as /api/shutdown).
# 2. Wait up to -TimeoutSec. The app forces its own exit after 45 s.
# 3. Kill what is left.
#
# Exit code 0 when none is left running, 1 otherwise.
param(
    [Parameter(Mandatory = $true)][string]$ExePath,
    [int]$TimeoutSec = 50
)

$target = [IO.Path]::GetFullPath($ExePath)

function Get-Ours {
    @(Get-CimInstance Win32_Process -Filter "Name = 'EvolveCapture.exe'" -ErrorAction SilentlyContinue |
        Where-Object {
            $_.ExecutablePath -and
            [string]::Equals([IO.Path]::GetFullPath($_.ExecutablePath), $target, [StringComparison]::OrdinalIgnoreCase)
        })
}

$procs = @(Get-Ours)
if ($procs.Count -eq 0) {
    Write-Output "Evolve Capture is not running from $target"
    exit 0
}

$asked = 0
foreach ($p in $procs) {
    try {
        $ev = [Threading.EventWaitHandle]::OpenExisting("Local\EvolveCapture-Quit-$($p.ProcessId)")
        [void]$ev.Set()
        $ev.Dispose()
        $asked++
        Write-Output "Asked Evolve Capture (pid $($p.ProcessId)) to stop"
    } catch {
        Write-Output "Evolve Capture (pid $($p.ProcessId)) has no quit event"
    }
}

if ($asked -gt 0) {
    $deadline = (Get-Date).AddSeconds($TimeoutSec)
    while ((Get-Date) -lt $deadline -and @(Get-Ours).Count -gt 0) {
        Start-Sleep -Milliseconds 500
    }
}

foreach ($p in @(Get-Ours)) {
    Write-Output "Killing Evolve Capture (pid $($p.ProcessId))"
    Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue
}
Start-Sleep -Milliseconds 500

if (@(Get-Ours).Count -gt 0) {
    Write-Output "Evolve Capture is still running"
    exit 1
}
Write-Output "Evolve Capture stopped"
exit 0
