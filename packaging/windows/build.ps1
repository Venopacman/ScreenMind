<#
.SYNOPSIS
Builds the Windows installer: packaging/dist/ScreenMind-<version>-win-x64-setup.exe

.DESCRIPTION
1. uv sync --frozen
2. PyInstaller with packaging/screenmind.spec -> packaging/dist/ScreenMind/
3. Checks the bundled msvcp140.dll is 14.40 or newer (F8 in docs/plans/packaging.md)
4. makensis packaging/windows/screenmind.nsi -> the setup exe

Run from anywhere; paths are relative to the repo. Needs uv and NSIS 3
(makensis on PATH or in Program Files). Works in Windows PowerShell 5.1 and pwsh.
In CI: .github/workflows/package-windows.yml.

.PARAMETER SkipPyInstaller
Reuse packaging/dist/ScreenMind from an earlier run (no sync, no PyInstaller).

.PARAMETER SkipInstaller
Stop after the PyInstaller build and the DLL check (for example to sign the
exe and DLLs before the installer is made).

.PARAMETER MakeNsis
Path to makensis.exe, if it is not on PATH or in Program Files.
#>
param(
    [switch]$SkipPyInstaller,
    [switch]$SkipInstaller,
    [string]$MakeNsis
)

$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
$dist = Join-Path $repo "packaging\dist"
$app = Join-Path $dist "ScreenMind"
$pyinstallerVersion = "6.22.3"

function Invoke-Native {
    param([string]$Exe, [string[]]$Arguments)
    Write-Host "> $Exe $($Arguments -join ' ')"
    & $Exe @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Exe failed with exit code $LASTEXITCODE" }
}

# Version: screenmind/__init__.py is the one source (pyproject reads it too).
$init = Get-Content (Join-Path $repo "screenmind\__init__.py") -Raw
if ($init -notmatch '__version__\s*=\s*"([^"]+)"') { throw "No __version__ in screenmind/__init__.py" }
$version = $Matches[1]
# File properties need four numbers: 0.2.4 -> 0.2.4.0, 0.3.0rc1 -> 0.3.0.0
$nums = @([regex]::Matches($version, '\d+') | Select-Object -First 4 | ForEach-Object { $_.Value })
while ($nums.Count -lt 4) { $nums += "0" }
$version4 = $nums -join "."
Write-Host "ScreenMind $version ($version4)"

Push-Location $repo
try {
    if (-not $SkipPyInstaller) {
        Invoke-Native "uv" @("sync", "--frozen")
        Invoke-Native "uv" @("run", "--frozen", "--with", "pyinstaller==$pyinstallerVersion",
            "pyinstaller", "packaging/screenmind.spec", "--noconfirm",
            "--distpath", "packaging/dist", "--workpath", "packaging/build")
    }
    if (-not (Test-Path (Join-Path $app "ScreenMind.exe"))) { throw "No build at $app" }

    # F8: an old msvcp140.dll from another app on PATH crashed onnxruntime.
    $dll = Join-Path $app "_internal\msvcp140.dll"
    if (-not (Test-Path $dll)) { throw "msvcp140.dll is missing from the bundle ($dll)" }
    $vi = (Get-Item $dll).VersionInfo
    $dllVersion = [version]::new($vi.FileMajorPart, $vi.FileMinorPart, $vi.FileBuildPart, $vi.FilePrivatePart)
    if ($dllVersion -lt [version]"14.40") {
        throw "Bundled msvcp140.dll is $dllVersion; onnxruntime needs 14.40 or newer. Check PATH during the build (F8)."
    }
    Write-Host "msvcp140.dll $dllVersion OK"

    $files = Get-ChildItem -Recurse -File $app
    Write-Host ("App folder: {0:N0} MB, {1} files" -f (($files | Measure-Object Length -Sum).Sum / 1MB), $files.Count)

    if ($SkipInstaller) { return }

    if (-not $MakeNsis) {
        $cmd = Get-Command makensis -ErrorAction SilentlyContinue
        if ($cmd) { $MakeNsis = $cmd.Source }
    }
    if (-not $MakeNsis) {
        foreach ($p in @("${env:ProgramFiles(x86)}\NSIS\makensis.exe", "$env:ProgramFiles\NSIS\makensis.exe")) {
            if ($p -and (Test-Path $p)) { $MakeNsis = $p; break }
        }
    }
    if (-not $MakeNsis) { throw "makensis not found. Install NSIS 3, pass -MakeNsis, or build in CI (package-windows.yml)." }

    $out = Join-Path $dist "ScreenMind-$version-win-x64-setup.exe"
    if (Test-Path $out) { Remove-Item $out }
    Invoke-Native $MakeNsis @("/V2", "/INPUTCHARSET", "UTF8",
        "/DVERSION=$version", "/DVERSION4=$version4", "/DSRCDIR=$app", "/DOUTFILE=$out",
        (Join-Path $PSScriptRoot "screenmind.nsi"))
    Write-Host ("Installer: {0} ({1:N0} MB)" -f $out, ((Get-Item $out).Length / 1MB))
} finally {
    Pop-Location
}
