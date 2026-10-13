<#
.SYNOPSIS
  Run the legacy Worker once against the SMB share and print the result. Refuses to run when
  out\<Stage> already exists.
.DESCRIPTION
  Runs on the Windows host (send it with ssm_run.py --platform windows, after prep-worker.ps1).
  SSM Run Command executes as SYSTEM, which has no ACL entry on the share, so the script first
  opens an SMB session as <Domain>\appsvc with the password read from the secret (through the
  instance role; the value is never printed and the variable is cleared right after the mapping).
  Guards, all before the Worker starts:
    - Stage must match s<digit>[a-z]{0,2}.
    - The Worker's config file must exist and its Stage key must equal -Stage, and its SmbRoot key
      must equal the share this script maps. The legacy Worker takes its stage and share from that
      file, so a mismatch would write somewhere other than the stage that was checked.
    - out\<Stage> must not exist as seen over SMB: "GUARD: out\<Stage> already exists; not running".
  The SMB mapping is removed in a finally block whatever happens. Prints UTC start and end times,
  WORKER-EXIT=<code>, the captured stdout and stderr, and the SMB listing of out\<Stage>. Deletes
  nothing on the share or on the host.
.PARAMETER Stage
  Stage directory name under out\ (default s0, the legacy baseline).
.PARAMETER ExeDir
  Directory holding the Worker prepared by prep-worker.ps1.
.PARAMETER ExeName
  Worker executable file name (default DocIntake.Worker.exe).
.PARAMETER SvmNetbios
  NetBIOS name of the SVM that serves the share (default APPMODSVM01).
.PARAMETER Domain
  NetBIOS domain of the service account (default APPMOD, the name probe-launch.ps1 uses).
.PARAMETER SecretId
  Secrets Manager secret with the application users (default appmod/app-users).
.PARAMETER Region
  AWS region for the secret (default ap-northeast-1).
.EXAMPLE
  run-worker.ps1 -ExeDir C:\appmod\DocIntake-worker -Stage s0
#>
[CmdletBinding(DefaultParameterSetName = 'Help', PositionalBinding = $false)]
param(
    [Parameter(ParameterSetName = 'Run')][ValidatePattern('^s[0-9][a-z]{0,2}$')][string]$Stage = "s0",
    [Parameter(ParameterSetName = 'Run', Mandatory = $true)][string]$ExeDir,
    [Parameter(ParameterSetName = 'Run')][string]$ExeName = "DocIntake.Worker.exe",
    [Parameter(ParameterSetName = 'Run')][string]$SvmNetbios = "APPMODSVM01",
    [Parameter(ParameterSetName = 'Run')][string]$Domain = "APPMOD",
    [Parameter(ParameterSetName = 'Run')][string]$SecretId = "appmod/app-users",
    [Parameter(ParameterSetName = 'Run')][string]$Region = "ap-northeast-1",
    [Parameter(ParameterSetName = 'Help')][switch]$Help,
    # Collects anything the binder would otherwise bind by position, so an option spelled --help or
    # --no-such-flag is answered here instead of being taken as the value of a parameter.
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArgs
)
# Argument handling comes before anything is read, mapped or written: a script that binds --help to
# a parameter runs for real when someone asks it for usage. The Run set holds the real parameters;
# with none of them given the script answers with usage instead of prompting.
$UsageLine = "usage: run-worker.ps1 -ExeDir <dir> [-Stage <sN>] [-ExeName <file>] [-SvmNetbios <name>] [-Domain <name>] [-SecretId <id>] [-Region <region>] | -Help"
if ($Help -or ($ExtraArgs -contains "--help") -or ($ExtraArgs -contains "-h")) {
    Write-Output $UsageLine
    Write-Output "  Runs the published Worker once against the share as the appsvc user."
    return
}
if ($ExtraArgs -and $ExtraArgs.Count -gt 0) {
    [Console]::Error.WriteLine("run-worker.ps1: unknown argument: $($ExtraArgs[0])")
    [Console]::Error.WriteLine($UsageLine)
    exit 2
}
if ($PSCmdlet.ParameterSetName -ne 'Run') {
    [Console]::Error.WriteLine("run-worker.ps1: missing required parameters")
    [Console]::Error.WriteLine($UsageLine)
    exit 2
}
$ErrorActionPreference = "Stop"
if ($ExeName -notmatch '^[A-Za-z0-9._-]+\.exe$') { throw "ExeName is not a plain .exe file name: $ExeName" }
$remote = "\\$SvmNetbios\appdata"
$exe = "$ExeDir\$ExeName"
$config = "$exe.config"
if (-not (Test-Path -LiteralPath $exe)) { throw "$exe not found" }
if (-not (Test-Path -LiteralPath $config)) { throw "GUARD: $config not found; the Worker's stage cannot be checked" }
[xml]$cfg = Get-Content -LiteralPath $config -Raw
$settings = @{}
foreach ($n in $cfg.configuration.appSettings.add) { $settings[$n.key] = $n.value }
if ($settings["Stage"] -ne $Stage) { throw "GUARD: config Stage is '$($settings["Stage"])', not '$Stage'; not running" }
if ($settings["SmbRoot"] -ne $remote) { throw "GUARD: config SmbRoot is '$($settings["SmbRoot"])', not '$remote'; not running" }
Import-Module AWSPowerShell -ErrorAction Stop
$appUsers = (Get-SECSecretValue -SecretId $SecretId -Region $Region).SecretString | ConvertFrom-Json
Get-SmbMapping -RemotePath $remote -ErrorAction SilentlyContinue | Remove-SmbMapping -Force -ErrorAction SilentlyContinue
New-SmbMapping -RemotePath $remote -UserName "$Domain\appsvc" -Password $appUsers.appsvc | Out-Null
$appUsers = $null
$exit = $null
try {
    if (Test-Path -LiteralPath "$remote\out\$Stage") { throw "GUARD: out\$Stage already exists; not running" }
    "guard ok: out\$Stage absent (SMB view)"
    "run-start-utc: " + (Get-Date).ToUniversalTime().ToString("o")
    $p = Start-Process -FilePath $exe -WorkingDirectory $ExeDir -NoNewWindow -PassThru -Wait `
        -RedirectStandardOutput "$ExeDir\run.stdout.txt" -RedirectStandardError "$ExeDir\run.stderr.txt"
    $exit = $p.ExitCode
    "run-end-utc: " + (Get-Date).ToUniversalTime().ToString("o")
    "WORKER-EXIT=$exit"
    "--- stdout"
    Get-Content -LiteralPath "$ExeDir\run.stdout.txt" -Raw
    "--- stderr"
    Get-Content -LiteralPath "$ExeDir\run.stderr.txt" -Raw
    "--- SMB listing of out\$Stage"
    Get-ChildItem -LiteralPath "$remote\out\$Stage" -Recurse -ErrorAction SilentlyContinue | ForEach-Object { "{0}`t{1}" -f $_.FullName, $_.Length }
}
finally {
    Remove-SmbMapping -RemotePath $remote -Force -ErrorAction SilentlyContinue
}
