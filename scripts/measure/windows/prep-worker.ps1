<#
.SYNOPSIS
  Copy a built legacy Worker to a new run directory on the Windows host and print what was copied.
.DESCRIPTION
  Runs on the Windows host (send it with ssm_run.py --platform windows). Refuses an existing
  destination, so an earlier run directory (its stdout/stderr files included) is never overwritten.
  Copies the files of the build output directory without recursing, prints name, length and the
  first 16 hex of the SHA-256 per file, the SHA-256 of the executable in both places, the
  appSettings keys of the config file, and the runtime version the executable was built for.
  Deletes and moves nothing.
.PARAMETER SourceDir
  Build output directory holding DocIntake.Worker.exe (for example the Release folder under the
  Worker project of app/legacy).
.PARAMETER DestDir
  New run directory. Must not exist.
.PARAMETER ExeName
  Executable file name inside SourceDir (default DocIntake.Worker.exe).
.EXAMPLE
  prep-worker.ps1 -SourceDir C:\appmod\src\src\DocIntake.Worker\bin\Release -DestDir C:\appmod\DocIntake-worker
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SourceDir,
    [Parameter(Mandatory = $true)][string]$DestDir,
    [string]$ExeName = "DocIntake.Worker.exe"
)
$ErrorActionPreference = "Stop"
if ($ExeName -notmatch '^[A-Za-z0-9._-]+\.exe$') { throw "ExeName is not a plain .exe file name: $ExeName" }
if (-not (Test-Path -LiteralPath "$SourceDir\$ExeName")) { throw "$SourceDir\$ExeName not found" }
if (Test-Path -LiteralPath $DestDir) { throw "$DestDir already exists; not copying" }
New-Item -ItemType Directory -Path $DestDir | Out-Null
Copy-Item -Path "$SourceDir\*" -Destination $DestDir
Get-ChildItem -LiteralPath $DestDir | ForEach-Object {
    "{0}`t{1}`t{2}" -f $_.Name, $_.Length, (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.Substring(0, 16)
}
"src exe sha256: " + (Get-FileHash -LiteralPath "$SourceDir\$ExeName").Hash
"dst exe sha256: " + (Get-FileHash -LiteralPath "$DestDir\$ExeName").Hash
$config = "$DestDir\$ExeName.config"
if (Test-Path -LiteralPath $config) {
    "--- config"
    Get-Content -LiteralPath $config | Select-String 'add key'
}
$ver = [System.Reflection.Assembly]::ReflectionOnlyLoadFrom("$DestDir\$ExeName").ImageRuntimeVersion
"ImageRuntimeVersion: $ver"
