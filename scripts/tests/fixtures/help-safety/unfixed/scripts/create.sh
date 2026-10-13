#!/usr/bin/env bash
# Fixture: ignores its arguments, so `create.sh --help` runs the AWS call. This is the shape that
# created four Secrets Manager secrets. check_help_is_safe.py must reject it.
set -euo pipefail
aws secretsmanager create-secret --name demo/one --secret-string placeholder
echo "create: done"
