<#
.SYNOPSIS
  Take a file inventory of seed\ over SMB on the Windows EC2 host (every boundary b0..b3).

.DESCRIPTION
  Emits the same JSON shape as inventory.sh: for every file under <SharePath>\seed, the relative
  path (/-separated to match the Linux side), the size in bytes and the SHA-256. Read-only. The
  top-level listing used by the invariant check comes from Get-ChildItem -Force on the Windows side,
  which is where the ONTAP snapshot directories (~snapshot, .snapshot) are excluded.

.EXAMPLE
  pwsh scripts/inventory.ps1 -SharePath \\APPMODSVM01\appdata -Out C:\windows-inventory.json
#>
[CmdletBinding(DefaultParameterSetName = 'Help', PositionalBinding = $false)]
param(
    [Parameter(ParameterSetName = 'Run', Mandatory = $true)][string]$SharePath,
    [Parameter(ParameterSetName = 'Run')][string]$Out = "-",
    [Parameter(ParameterSetName = 'Help')][switch]$Help,
    # Collects anything the binder would otherwise bind by position, so an option spelled --help or
    # --no-such-flag is answered here instead of being taken as the value of a parameter.
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$ExtraArgs
)
# Argument handling comes before anything is read, mapped or written: a script that binds --help to
# a parameter runs for real when someone asks it for usage. The Run set holds the real parameters;
# with none of them given the script answers with usage instead of prompting.
$UsageLine = "usage: inventory.ps1 -SharePath <unc-path> [-Out <file>] | -Help"
if ($Help -or ($ExtraArgs -contains "--help") -or ($ExtraArgs -contains "-h")) {
    Write-Output $UsageLine
    Write-Output "  Read-only SHA-256 inventory of seed\\ over SMB; prints JSON, or writes it to -Out."
    return
}
if ($ExtraArgs -and $ExtraArgs.Count -gt 0) {
    [Console]::Error.WriteLine("inventory.ps1: unknown argument: $($ExtraArgs[0])")
    [Console]::Error.WriteLine($UsageLine)
    exit 2
}
if ($PSCmdlet.ParameterSetName -ne 'Run') {
    [Console]::Error.WriteLine("inventory.ps1: missing required parameters")
    [Console]::Error.WriteLine($UsageLine)
    exit 2
}

$ErrorActionPreference = "Stop"

$seedRoot = Join-Path $SharePath "seed"
if (-not (Test-Path -LiteralPath $seedRoot)) {
    Write-Error "inventory: $seedRoot does not exist"
    exit 2
}

$files = Get-ChildItem -LiteralPath $seedRoot -Recurse -File | Sort-Object FullName
$records = foreach ($file in $files) {
    $relative = $file.FullName.Substring($seedRoot.Length).TrimStart('\')
    $relative = "seed/" + ($relative -replace '\\', '/')
    $hash = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLower()
    [ordered]@{ path = $relative; size = $file.Length; sha256 = $hash }
}

# Top-level entries of the share, for the invariant's "only seed/, probe/, out/" check. -Force
# includes hidden entries; the ONTAP snapshot directories are excluded here, by name.
$topLevel = @(Get-ChildItem -LiteralPath $SharePath -Force |
    Where-Object { $_.Name -notin @("~snapshot", ".snapshot") } |
    Sort-Object Name | ForEach-Object { $_.Name })

$payload = [ordered]@{
    store = [ordered]@{ kind = "smb"; root = $SharePath }
    top_level = $topLevel
    files = @($records)
}

$json = $payload | ConvertTo-Json -Depth 5
if ($Out -eq "-") {
    Write-Output $json
}
else {
    Set-Content -LiteralPath $Out -Value $json -Encoding utf8
    Write-Information "inventory: wrote seed inventory to $Out"
}
