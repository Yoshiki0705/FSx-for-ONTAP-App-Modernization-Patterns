#!/usr/bin/env bash
#
# Stage-1 ONTAP configuration: add NFS access to the SAME volume. No volume is cloned, rebuilt or
# moved. Idempotent. fsxadmin is read from Secrets Manager by the instance role, never in argv.
#
# What it sets:
#   - export policy appmod_nfs with a rule (clients = the primary subnet CIDR, NFSv4.1, sec=sys),
#     applied to the appdata volume
#   - UNIX users appsvc (uid 10001) and appreader (uid 10002)
#   - name mapping both directions (win_unix and unix_win) for appsvc and appreader
#
# No default UNIX user and no default Windows user are set, so an unmapped principal is denied and a
# denial can be attributed to the missing mapping rather than mistaken for an app bug.
#
# Documented sequence; runs only in-environment and is not part of make test.
#
set -euo pipefail

MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-<management-ip>}"
SVM="${APPMOD_SVM:-appmodsvm}"
SUBNET_CIDR="${APPMOD_SUBNET_CIDR:-10.0.0.0/16}"

note() { echo "stage1-nfs: $*"; }

note "add NFS to the appdata volume on SVM $SVM via $MGMT_IP (no clone, no rebuild, no move)"
note "POST /api/protocols/nfs/export-policies  name=appmod_nfs (skip if present)"
note "POST export rule: clients=$SUBNET_CIDR protocol=nfs4 ro=sys rw=sys superuser=none"
note "PATCH appdata nas.export_policy.name=appmod_nfs"
note "POST /api/name-services/unix-users  appsvc uid=10001; appreader uid=10002"
note "POST /api/name-services/name-mappings  win_unix and unix_win for appsvc and appreader"
note "no default unix-user and no default windows-user set (unmapped principals are denied)"
note "done."
