<#
.SYNOPSIS
  Windows-side probe launcher: establish the appsvc SMB session, then run DocIntake.Probe.

.DESCRIPTION
  SSM Run Command executes as SYSTEM, which presents the machine account to the SVM and has no ACL
  entry on the share, so the probe must authenticate as APPMOD\appsvc first. This reads the appsvc
  password from appmod/app-users (via the instance role; never echoed), creates an SMB session with
  New-SmbMapping (password as a parameter, not on a command line), then runs the deployed
  DocIntake.Probe against the share and prints its appmod-probe/1 JSON to stdout (which the Run
  Command uploads to the artifacts bucket).

  Invoked by run-probe.sh. Keeps the credential handling out of run-probe.sh's SSM command string.

  With -PairBehavior/-SyncId/-Bucket it runs one coordinated two-client behavior and bridges the
  Probe's local signal directory to the artifacts bucket, so the barrier never touches the volume.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$Stage,
    [Parameter(Mandatory = $true)][string]$RunId,
    [string]$Role = "holder",
    [string]$SvmNetbios = "APPMODSVM01",
    [string]$Region = "ap-northeast-1",
    [string]$ProbeExe = "C:\appmod\DocIntake\DocIntake.Probe.exe",
    # Coordinated two-client mode: one behavior against the other host, synchronized via S3.
    [string]$PairBehavior = "",
    [string]$SyncId = "",
    [string]$Bucket = ""
)

$ErrorActionPreference = "Stop"
Import-Module AWSPowerShell -ErrorAction Stop

$remote = "\\$SvmNetbios\appdata"
$appUsers = (Get-SECSecretValue -SecretId appmod/app-users -Region $Region).SecretString | ConvertFrom-Json
Get-SmbMapping -RemotePath $remote -ErrorAction SilentlyContinue | Remove-SmbMapping -Force -ErrorAction SilentlyContinue
New-SmbMapping -RemotePath $remote -UserName "APPMOD\appsvc" -Password $appUsers.appsvc | Out-Null
$appUsers = $null

# Clock offset against the Amazon Time Sync Service, recorded with the result. The merge treats a
# difference smaller than the two hosts' offsets as no difference.
$offsetMs = ""
$chart = w32tm /stripchart /computer:169.254.169.123 /dataonly /samples:2 2>$null
$last = $chart | Select-String -Pattern ',\s*([+-]?[0-9.]+)s' | Select-Object -Last 1
if ($last) {
    $offsetMs = ([double]::Parse($last.Matches[0].Groups[1].Value,
        [Globalization.CultureInfo]::InvariantCulture) * 1000).ToString([Globalization.CultureInfo]::InvariantCulture)
}

try {
    if (-not $PairBehavior) {
        & $ProbeExe --store smb --root $remote --stage $Stage --role $Role --run-id $RunId
        return
    }
    # Coordinated pair. The Probe signals through local directories only; this loop bridges them
    # to the artifacts bucket (s3://<bucket>/probe/<run-id>/sync/<sync-id>/<role>/<name>), so the
    # two hosts never synchronize through the volume under test.
    $peer = @{ holder = "contender"; contender = "holder"; writer = "reader"; reader = "writer" }[$Role]
    $sync = "C:\appmod\sync\$SyncId"
    New-Item -ItemType Directory -Force -Path "$sync\out", "$sync\in" | Out-Null
    $prefix = "probe/$RunId/sync/$SyncId"
    $probeArgs = @("--store", "smb", "--root", $remote, "--stage", $Stage, "--role", $Role, "--run-id", $RunId,
        "--pair-behavior", $PairBehavior, "--sync-id", $SyncId, "--sync-dir", $sync)
    if ($offsetMs) { $probeArgs += @("--ntp-offset-ms", $offsetMs) }
    $proc = Start-Process -FilePath $ProbeExe -ArgumentList $probeArgs -NoNewWindow -PassThru `
        -RedirectStandardOutput "$sync\stdout.json" -RedirectStandardError "$sync\stderr.txt"
    $sent = @{}; $got = @{}
    $deadline = (Get-Date).AddSeconds(300)
    $bridge = {
        Get-ChildItem -LiteralPath "$sync\out" -File | Where-Object { $_.Name -notlike "*.tmp" } | ForEach-Object {
            if (-not $sent.ContainsKey($_.Name)) {
                Write-S3Object -BucketName $Bucket -Key "$prefix/$Role/$($_.Name)" -File $_.FullName -Region $Region | Out-Null
                $sent[$_.Name] = $true
            }
        }
        Get-S3Object -BucketName $Bucket -KeyPrefix "$prefix/$peer/" -Region $Region | ForEach-Object {
            $name = ($_.Key -split "/")[-1]
            if ($name -and -not $got.ContainsKey($name)) {
                Read-S3Object -BucketName $Bucket -Key $_.Key -File "$sync\in\$name.tmp" -Region $Region | Out-Null
                Move-Item -LiteralPath "$sync\in\$name.tmp" -Destination "$sync\in\$name" -Force
                $got[$name] = $true
            }
        }
    }
    while (-not $proc.HasExited) {
        & $bridge
        if ((Get-Date) -gt $deadline) { $proc.Kill(); break }
        Start-Sleep -Milliseconds 200
    }
    & $bridge
    Get-Content -LiteralPath "$sync\stdout.json" -Raw
}
finally {
    Remove-SmbMapping -RemotePath $remote -Force -ErrorAction SilentlyContinue
}
