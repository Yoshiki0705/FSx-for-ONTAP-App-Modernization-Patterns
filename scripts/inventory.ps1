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
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)][string]$SharePath,
    [string]$Out = "-"
)

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

$payload = [ordered]@{
    store = [ordered]@{ kind = "smb"; root = $SharePath }
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
