<#
.SYNOPSIS
  Windows-side inventory launcher: establish the appsvc SMB session, then run inventory.ps1 against
  the share and print the seed/ inventory JSON to stdout.

.DESCRIPTION
  SSM Run Command executes as SYSTEM, which cannot reach the share, so this authenticates as
  APPMOD\appsvc (New-SmbMapping, password from appmod/app-users via the instance role, never
  echoed) before invoking inventory.ps1. Invoked for the boundary records (b0..b3). inventory.ps1
  must be staged at C:\appmod\inventory.ps1.
#>
[CmdletBinding()]
param(
    [string]$SvmNetbios = "APPMODSVM01",
    [string]$Region = "ap-northeast-1",
    [string]$InventoryScript = "C:\appmod\inventory.ps1"
)

$ErrorActionPreference = "Stop"
Import-Module AWSPowerShell -ErrorAction Stop

$remote = "\\$SvmNetbios\appdata"
$appUsers = (Get-SECSecretValue -SecretId appmod/app-users -Region $Region).SecretString | ConvertFrom-Json
Get-SmbMapping -RemotePath $remote -ErrorAction SilentlyContinue | Remove-SmbMapping -Force -ErrorAction SilentlyContinue
New-SmbMapping -RemotePath $remote -UserName "APPMOD\appsvc" -Password $appUsers.appsvc | Out-Null
$appUsers = $null

try {
    & $InventoryScript -SharePath $remote -Out "-"
}
finally {
    Remove-SmbMapping -RemotePath $remote -Force -ErrorAction SilentlyContinue
}
