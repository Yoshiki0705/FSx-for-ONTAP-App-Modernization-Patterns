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
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][int]$Stage,
    [Parameter(Mandatory = $true)][string]$RunId,
    [string]$Role = "holder",
    [string]$SvmNetbios = "APPMODSVM01",
    [string]$Region = "ap-northeast-1",
    [string]$ProbeExe = "C:\appmod\DocIntake\DocIntake.Probe.exe"
)

$ErrorActionPreference = "Stop"
Import-Module AWSPowerShell -ErrorAction Stop

$remote = "\\$SvmNetbios\appdata"
$appUsers = (Get-SECSecretValue -SecretId appmod/app-users -Region $Region).SecretString | ConvertFrom-Json
Get-SmbMapping -RemotePath $remote -ErrorAction SilentlyContinue | Remove-SmbMapping -Force -ErrorAction SilentlyContinue
New-SmbMapping -RemotePath $remote -UserName "APPMOD\appsvc" -Password $appUsers.appsvc | Out-Null
$appUsers = $null

try {
    & $ProbeExe --store smb --root $remote --stage $Stage --role $Role --run-id $RunId
}
finally {
    Remove-SmbMapping -RemotePath $remote -Force -ErrorAction SilentlyContinue
}
