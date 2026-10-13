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
[CmdletBinding(PositionalBinding = $false)]
param(
    [string]$SvmNetbios = "APPMODSVM01",
    [string]$Region = "ap-northeast-1",
    [string]$InventoryScript = "C:\appmod\inventory.ps1",
    [switch]$Help,
    # Collects anything the binder would otherwise bind by position, so an option spelled --help or
    # --no-such-flag is answered here instead of being taken as the value of -SvmNetbios.
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArgs
)

# Argument handling comes before any secret read or SMB mapping: a script that binds --help to a
# parameter runs for real when someone asks it for usage.
$UsageLine = "usage: inventory-launch.ps1 [-SvmNetbios <name>] [-Region <region>] [-InventoryScript <path>] [-Help]"
if ($Help -or ($ExtraArgs -contains "--help") -or ($ExtraArgs -contains "-h")) {
    Write-Output $UsageLine
    Write-Output "  Authenticates as the appsvc user over SMB, runs inventory.ps1 against the share and prints the inventory JSON."
    return
}
if ($ExtraArgs -and $ExtraArgs.Count -gt 0) {
    [Console]::Error.WriteLine("inventory-launch: unknown argument: $($ExtraArgs[0])")
    [Console]::Error.WriteLine($UsageLine)
    exit 2
}

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
