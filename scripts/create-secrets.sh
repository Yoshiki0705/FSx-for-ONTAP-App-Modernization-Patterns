#!/usr/bin/env bash
#
# Create the four Secrets Manager secrets the base stack and the ONTAP scripts need:
#
#   appmod/ad-admin        directory Admin / SVM join service account   (key: password)
#   appmod/fsxadmin        ONTAP cluster admin                           (key: password)
#   appmod/app-users       appsvc and appreader                          (keys: appsvc, appreader)
#   appmod/ontap-itclone   REST user appmod-itclone for integration-clone.sh (key: password)
#
# Passwords are generated with `aws secretsmanager get-random-password` and passed to create-secret
# via --cli-input-json on stdin, so no password appears in argv, in the shell history or in a
# process list. Run after approval, before deploy.sh base.
#
# When APPMOD_DRY_RUN is set, the AWS calls are printed instead of run, so the flow can be exercised
# without credentials and without creating anything.
#
set -euo pipefail

REGION="ap-northeast-1"
DRY_RUN="${APPMOD_DRY_RUN:-}"

aws_call() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION $*"
    return 0
  fi
  aws --region "$REGION" "$@"
}

# Emit a password that satisfies Directory Service (8-64, three of four classes) and ONTAP (8-50).
# Printed to stdout for capture into a here-doc; never placed in argv.
random_password() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN-PASSWORD"
    return 0
  fi
  # Exclude characters that would need JSON or shell escaping downstream (double quote, @, slash,
  # backslash). The trailing \\ is a literal backslash in the format string, intentional here.
  local exclude
  # shellcheck disable=SC1003
  printf -v exclude '%s\\' '"@/'
  aws --region "$REGION" secretsmanager get-random-password \
    --password-length 24 \
    --require-each-included-type \
    --exclude-characters "$exclude" \
    --query RandomPassword --output text
}

# Create a secret whose SecretString is read from stdin as the create-secret --cli-input-json body,
# so the secret value never crosses the command line.
create_from_stdin() {
  local secret_name="$1"
  if [ -n "$DRY_RUN" ]; then
    # Drain stdin so the pipeline does not break, then report the intended call.
    cat >/dev/null
    echo "DRY-RUN: aws --region $REGION secretsmanager create-secret (name=$secret_name, body on stdin)"
    return 0
  fi
  aws --region "$REGION" secretsmanager create-secret --cli-input-json file:///dev/stdin
  echo "created: $secret_name"
}

json_escape() {
  # Minimal JSON string escaping for a generated password (we exclude the risky characters above).
  printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g'
}

make_single_password_secret() {
  local name="$1" pw escaped
  pw="$(random_password)"
  escaped="$(json_escape "$pw")"
  create_from_stdin "$name" <<EOF
{"Name": "$name", "SecretString": "{\"password\": \"$escaped\"}"}
EOF
  unset pw escaped
}

make_app_users_secret() {
  local name="appmod/app-users" pw_svc pw_reader esc_svc esc_reader
  pw_svc="$(random_password)"
  pw_reader="$(random_password)"
  esc_svc="$(json_escape "$pw_svc")"
  esc_reader="$(json_escape "$pw_reader")"
  create_from_stdin "$name" <<EOF
{"Name": "$name", "SecretString": "{\"appsvc\": \"$esc_svc\", \"appreader\": \"$esc_reader\"}"}
EOF
  unset pw_svc pw_reader esc_svc esc_reader
}

main() {
  echo "create-secrets: creating four secrets in $REGION (passwords generated, never in argv)"
  make_single_password_secret "appmod/ad-admin"
  make_single_password_secret "appmod/fsxadmin"
  make_app_users_secret
  make_single_password_secret "appmod/ontap-itclone"
  echo "create-secrets: done"
}

main "$@"
