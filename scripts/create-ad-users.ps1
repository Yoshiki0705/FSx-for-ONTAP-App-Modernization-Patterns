<#
.SYNOPSIS
  Create the appsvc and appreader AD users (stage-0 prerequisite), on the domain-joined Windows EC2
  host via SSM Run Command, over LDAP (no ADWS / RSAT dependency).

.DESCRIPTION
  The design has the SVM AD-joined and the SMB share / NTFS ACLs grant to APPMOD\appsvc and
  APPMOD\appreader, but no step created those directory accounts; base.yaml only installs the RSAT
  AD tools on the Windows host and grants it read on the secrets. This script fills that gap. It is
  a prerequisite for scripts/ontap/stage0-smb.sh, which cannot resolve the SIDs when the users are
  absent.

  It uses System.DirectoryServices over LDAP rather than the ActiveDirectory module, because on this
  environment ADWS (TCP 9389, which the AD cmdlets require) is not reachable from the Windows host
  while LDAP (389) and LDAPS (636) are. The bind uses Secure (sign+seal) authentication so the
  SetPassword call is carried over an encrypted channel.

  Both users are created in OU=Users,OU=APPMOD,DC=appmod,DC=example,DC=com with NO group membership
  (the share and NTFS ACLs grant to each SID directly). The domain-root CN=Users is NOT writable by
  the AWS Managed Microsoft AD delegated admin (CommitChanges returns Access is denied); OU=APPMOD
  is the delegated tree the admin can write, and it already holds OU=Users and OU=Computers.
  Passwords come from appmod/app-users (keys appsvc,
  appreader); the directory credential comes from appmod/ad-admin (key password, user Admin). Both
  secrets are read in-script via the instance role (Get-SECSecretValue) and never written to disk,
  argv or output.

  Idempotent: if a user already exists, its password is reset and the account enabled, rather than
  erroring.

.NOTES
  Reads only the ad-admin and app-users secrets; creates nothing chargeable. The server is picked
  from the directory DNS IPs so the LDAP bind does not round-robin to an unreachable node.
#>
[CmdletBinding()]
param(
    [string]$Region = "ap-northeast-1",
    [string]$DomainDn = "OU=Users,OU=APPMOD,DC=appmod,DC=example,DC=com",
    [string]$AdAdminSecret = "appmod/ad-admin",
    [string]$AppUsersSecret = "appmod/app-users",
    [string]$AdminUser = "Admin",
    # DC addresses are resolved at runtime from the domain name by default (the directory DNS IPs
    # are not hardcoded here, so no internal IP lives in a tracked file). Pass -DcIps to override.
    [string]$DomainName = "appmod.example.com",
    [string[]]$DcIps = @()
)

# Build UPNs from parts so no literal name@domain string sits in a tracked file.
$AdminUpn = "$AdminUser@$DomainName"

$ErrorActionPreference = "Stop"

# AWS Tools for PowerShell ships on the AMI; the aws CLI is not on PATH. Read secrets via the
# instance role.
Import-Module AWSPowerShell -ErrorAction Stop

function Get-SecretJson {
    param([string]$SecretId)
    $raw = (Get-SECSecretValue -SecretId $SecretId -Region $Region).SecretString
    if (-not $raw) { throw "could not read secret $SecretId" }
    return $raw | ConvertFrom-Json
}

$adAdmin = Get-SecretJson -SecretId $AdAdminSecret
$appUsers = Get-SecretJson -SecretId $AppUsersSecret

# Resolve DC addresses: use -DcIps if supplied, otherwise resolve the domain name via DNS (the
# AWS Managed Microsoft AD A records for the domain point at the DCs). No internal IP is hardcoded.
if (-not $DcIps -or $DcIps.Count -eq 0) {
    $DcIps = (Resolve-DnsName -Name $DomainName -Type A -ErrorAction Stop |
        Where-Object { $_.IPAddress } | Select-Object -ExpandProperty IPAddress)
}
if (-not $DcIps -or $DcIps.Count -eq 0) { throw "could not resolve any DC address for $DomainName" }

# Pick a DC that answers LDAP. Bind with Secure authentication (Kerberos/NTLM sign+seal) so
# SetPassword is allowed over the sealed channel.
$server = $null
foreach ($ip in $DcIps) {
    if ((Test-NetConnection -ComputerName $ip -Port 389 -WarningAction SilentlyContinue).TcpTestSucceeded) {
        $server = $ip; break
    }
}
if (-not $server) { throw "no DC answered LDAP 389 among the resolved addresses" }
Write-Output ("LDAP-SERVER=$server")

$authType = [System.DirectoryServices.AuthenticationTypes]::Secure -bor `
    [System.DirectoryServices.AuthenticationTypes]::Sealing -bor `
    [System.DirectoryServices.AuthenticationTypes]::Signing

$containerPath = "LDAP://$server/$DomainDn"
$container = New-Object System.DirectoryServices.DirectoryEntry(
    $containerPath, $AdminUpn, $adAdmin.password, $authType)
# Force a bind now so an auth failure surfaces here, not mid-loop.
$null = $container.NativeObject

function Find-User {
    param([string]$Sam)
    $searchRoot = New-Object System.DirectoryServices.DirectoryEntry(
        "LDAP://$server/DC=appmod,DC=example,DC=com", $AdminUpn, $adAdmin.password, $authType)
    $searcher = New-Object System.DirectoryServices.DirectorySearcher($searchRoot)
    $searcher.Filter = "(&(objectClass=user)(sAMAccountName=$Sam))"
    $searcher.PageSize = 1
    return $searcher.FindOne()
}

$targets = @(
    @{ Sam = "appsvc";    Display = "App Service Account";   Password = $appUsers.appsvc },
    @{ Sam = "appreader"; Display = "App Read-Only Account"; Password = $appUsers.appreader }
)

$UF_NORMAL_ACCOUNT = 0x0200
$UF_DONT_EXPIRE_PASSWD = 0x10000

foreach ($t in $targets) {
    if (-not $t.Password) { throw ("no password for {0} in {1}" -f $t.Sam, $AppUsersSecret) }

    $found = Find-User -Sam $t.Sam
    if ($found) {
        $user = $found.GetDirectoryEntry()
        $user.Invoke("SetPassword", @($t.Password)) | Out-Null
        $uac = [int]$user.Properties["userAccountControl"].Value
        $uac = ($uac -bor $UF_NORMAL_ACCOUNT -bor $UF_DONT_EXPIRE_PASSWD) -band (-bnot 0x2)  # clear DISABLE
        $user.Properties["userAccountControl"].Value = $uac
        $user.CommitChanges()
        Write-Output ("EXISTS-RESET {0}" -f $t.Sam)
    }
    else {
        $user = $container.Children.Add("CN=$($t.Sam)", "user")
        $user.Properties["sAMAccountName"].Value = $t.Sam
        $user.Properties["userPrincipalName"].Value = ("{0}@{1}" -f $t.Sam, $DomainName)
        $user.Properties["displayName"].Value = $t.Display
        $user.CommitChanges()                         # create disabled first
        $user.Invoke("SetPassword", @($t.Password)) | Out-Null
        $uac = $UF_NORMAL_ACCOUNT -bor $UF_DONT_EXPIRE_PASSWD   # enabled, no expiry
        $user.Properties["userAccountControl"].Value = $uac
        $user.CommitChanges()
        Write-Output ("CREATED {0}" -f $t.Sam)
    }

    # Report the resolved SID (no secret) so the caller can confirm the account resolves, which is
    # exactly what the ONTAP share / NTFS ACL set needs.
    $again = Find-User -Sam $t.Sam
    if ($again) {
        $sidBytes = $again.Properties["objectSid"][0]
        $sid = New-Object System.Security.Principal.SecurityIdentifier($sidBytes, 0)
        Write-Output ("SID {0} = {1}" -f $t.Sam, $sid.Value)
    }
    else {
        Write-Output ("WARN {0} not found after create" -f $t.Sam)
    }
}

$adAdmin = $null; $appUsers = $null
Write-Output "AD-USERS-DONE"
