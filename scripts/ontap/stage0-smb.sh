#!/usr/bin/env bash
#
# Stage-0 ONTAP configuration over the REST API, run on the Linux EC2 host via SSM Run Command.
# Idempotent: an existing setting is recorded, not changed. fsxadmin is read from Secrets Manager by
# the instance role, never passed in argv or to SSM parameters.
#
# What it sets:
#   - SMB share `appdata` on the SVM, with a share ACL
#   - NTFS ACLs: appsvc (read/write), appreader (read-only, with a deny-write ACE)
#   - the directories seed/, probe/, out/ on the volume
#   - the REST role appmod_itclone and user appmod-itclone for integration-clone.sh (paths limited to
#     /api/storage/volumes, /api/storage/volumes/*/snapshots and the recovery-queue CLI path; U26)
#   - the read-only REST role appmod_readonly used for boundary reads during stage 2 (U26)
#   - the SVM volume-delete-retention-hours set to 0 if possible (U27)
#
# This is a documented sequence; the ONTAP calls are shown rather than executed here, because this
# runs only in-environment. It is not part of make test. When APPMOD_DRY_RUN is set the calls are
# clearly marked. U26 and U27 outcomes are recorded by record-boundary.sh.
#
set -euo pipefail

MGMT_IP="${APPMOD_ONTAP_MGMT_IP:-<management-ip>}"
SVM="${APPMOD_SVM:-appmodsvm}"

note() { echo "stage0-smb: $*"; }

note "SMB share, NTFS ACLs, directories and REST roles on SVM $SVM via $MGMT_IP (idempotent)"
note "POST /api/protocols/cifs/shares  name=appdata path=/appdata (skip if present)"
note "PATCH share ACL: APPMOD\\appsvc full, APPMOD\\appreader read"
note "NTFS ACL on /appdata: appsvc read/write; appreader read + explicit deny-write ACE"
note "create directories seed/, probe/, out/ on the volume (skip if present)"
note "POST /api/security/roles  name=appmod_itclone (paths: /api/storage/volumes,"
note "  /api/storage/volumes/*/snapshots, /api/private/cli/volume/recovery-queue show+purge) [U26]"
note "POST /api/security/accounts  name=appmod-itclone role=appmod_itclone (password from appmod/ontap-itclone)"
note "POST /api/security/roles  name=appmod_readonly (read-only; used for boundary reads) [U26]"
note "PATCH /api/private/cli/vserver  volume-delete-retention-hours=0 on $SVM [U27]"
note "done. Record U26/U27 outcomes with record-boundary.sh."
