#!/usr/bin/env bash
# Fixture: the same script with argument handling before the AWS call. check_help_is_safe.py must
# accept it.
set -euo pipefail
usage() { echo "usage: create.sh [-h|--help]"; }
case "${1:-}" in
  "") ;;
  -h | --help)
    usage
    exit 0
    ;;
  *)
    echo "create: unknown argument: $1" >&2
    usage >&2
    exit 2
    ;;
esac
aws secretsmanager create-secret --name demo/one --secret-string placeholder
echo "create: done"
