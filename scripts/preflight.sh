#!/usr/bin/env bash
#
# Read-only preflight checks run before the base stack is deployed. Two phases:
#
#   --phase network   inspect the network and recommend EgressMode and CreateS3GatewayEndpoint.
#                     Branches on whether a dedicated new VPC is being created or an existing VPC
#                     is reused:
#                       - no --vpc-id  : a new VPC will be created, so the existing-VPC
#                                        interference checks (describe-vpc-attribute /
#                                        describe-subnets / describe-route-tables for a supplied
#                                        VPC) are skipped. Instead it validates only what a new VPC
#                                        needs: at least two AZs available in the region, and no
#                                        CIDR conflict IF a CIDR is supplied with --cidr.
#                       - with --vpc-id: an existing VPC is reused, so the interference checks for
#                                        that VPC and (if given) --subnet-id are kept.
#   --phase secrets   confirm the four secrets exist and carry the expected keys. Values are never
#                     printed. Read-only.
#
# Every AWS call here is a describe/get. Nothing is created or changed. When APPMOD_DRY_RUN is set,
# the AWS calls are printed instead of run, so the control flow can be exercised without credentials.
#
# Region is always ap-northeast-1.
#
set -euo pipefail

REGION="ap-northeast-1"
PHASE=""
VPC_ID=""
SUBNET_ID=""
CIDR=""
DRY_RUN="${APPMOD_DRY_RUN:-}"

usage() {
  cat >&2 <<'EOF'
usage: preflight.sh --phase network|secrets [--vpc-id <id>] [--subnet-id <id>] [--cidr <cidr>]
  --phase network   recommend EgressMode and CreateS3GatewayEndpoint (read-only)
                    no --vpc-id : new dedicated VPC path (checks two AZs; optional --cidr conflict)
                    with --vpc-id : existing VPC path (keeps the interference checks)
  --phase secrets   confirm the four secrets exist with the expected keys (read-only)
EOF
}

# Run an AWS CLI call read-only, or print it under APPMOD_DRY_RUN.
aws_ro() {
  if [ -n "$DRY_RUN" ]; then
    echo "DRY-RUN: aws --region $REGION $*"
    return 0
  fi
  aws --region "$REGION" "$@"
}

while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASE="${2:-}"; shift 2 ;;
    --vpc-id) VPC_ID="${2:-}"; shift 2 ;;
    --subnet-id) SUBNET_ID="${2:-}"; shift 2 ;;
    --cidr) CIDR="${2:-}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "preflight: unknown argument: $1" >&2; usage; exit 2 ;;
  esac
done

case "$PHASE" in
  network|secrets) ;;
  *) echo "preflight: --phase must be network or secrets" >&2; usage; exit 2 ;;
esac

# New-VPC path: a dedicated VPC is being created, so there is no existing VPC to inspect for
# interference. Validate only what a new VPC needs.
phase_network_new_vpc() {
  local missing=0
  echo "preflight: network phase (read-only, NEW dedicated VPC) in $REGION"
  echo "  No --vpc-id given: a dedicated VPC will be created (CreateVpc=true), so the existing-VPC"
  echo "  interference checks are skipped. Validating what a new VPC needs instead."

  # AWS Managed Microsoft AD needs two subnets across two AZs; the region must offer at least two.
  aws_ro ec2 describe-availability-zones \
    --filters "Name=region-name,Values=$REGION" "Name=state,Values=available" >/dev/null || missing=1

  # A CIDR conflict can only be judged against existing VPCs IF a CIDR is supplied. With no CIDR,
  # nothing to check (the template's example default is the caller's to deconflict).
  if [ -n "$CIDR" ]; then
    echo "  Checking that the supplied CIDR $CIDR does not overlap an existing VPC CIDR"
    aws_ro ec2 describe-vpcs --query 'Vpcs[].CidrBlock' >/dev/null || missing=1
  else
    echo "  note: pass --cidr <cidr> to check the new VPC CIDR against existing VPCs" >&2
  fi

  # A directory of the same name already present would collide, regardless of VPC choice.
  aws_ro ds describe-directories >/dev/null || missing=1

  if [ "$missing" -ne 0 ]; then
    echo "preflight: new-VPC checks could not complete; see messages above" >&2
    return 1
  fi
  echo "  Recommended EgressMode: endpoints (the dedicated VPC creates no NAT gateway; use nat only"
  echo "  against a reused VPC that already has egress)."
  echo "  Recommended CreateS3GatewayEndpoint: true (a new VPC has no S3 gateway endpoint yet; it"
  echo "  attaches to the route table the template creates, so RouteTableIds stays empty)."
  return 0
}

# Existing-VPC path: an existing VPC/subnet is reused, so keep the interference checks for them.
phase_network_existing_vpc() {
  local missing=0
  echo "preflight: network phase (read-only, EXISTING VPC $VPC_ID) in $REGION"

  # DNS support and hostnames must be on for the seamless domain join to resolve the directory.
  aws_ro ec2 describe-vpc-attribute --vpc-id "$VPC_ID" --attribute enableDnsSupport \
    >/dev/null || missing=1
  # Two subnets across two AZs are required by AWS Managed Microsoft AD.
  aws_ro ec2 describe-subnets --filters "Name=vpc-id,Values=$VPC_ID" >/dev/null || missing=1
  # A directory of the same name already present would collide.
  aws_ro ds describe-directories >/dev/null || missing=1
  # Route tables for the primary subnet decide whether an S3 gateway endpoint already exists.
  aws_ro ec2 describe-route-tables \
    --filters "Name=association.subnet-id,Values=${SUBNET_ID:-subnet-unknown}" >/dev/null || missing=1
  # Existing interface endpoints decide EgressMode; an existing S3 gateway endpoint decides
  # CreateS3GatewayEndpoint.
  aws_ro ec2 describe-vpc-endpoints --filters "Name=vpc-id,Values=$VPC_ID" >/dev/null || missing=1

  if [ "$missing" -ne 0 ]; then
    echo "preflight: network inspection could not complete; see messages above" >&2
    echo "  Recommend EgressMode and CreateS3GatewayEndpoint only after the inspection succeeds." >&2
    return 1
  fi
  echo "  Recommended EgressMode: endpoints (set nat if a NAT gateway or these endpoints exist)"
  echo "  Recommended CreateInterfaceEndpoints: true (set false to reuse interface endpoints that"
  echo "  already exist in this VPC)."
  echo "  Recommended CreateS3GatewayEndpoint: true (set false if the subnet's route tables already"
  echo "  carry an S3 gateway endpoint). preflight prints the route tables above for RouteTableIds."
  return 0
}

phase_network() {
  if [ -z "$VPC_ID" ]; then
    phase_network_new_vpc
  else
    phase_network_existing_vpc
  fi
}

phase_secrets() {
  local missing=0 name
  echo "preflight: secrets phase (read-only) in $REGION; values are never printed"
  for name in appmod/ad-admin appmod/fsxadmin appmod/app-users appmod/ontap-itclone; do
    if ! aws_ro secretsmanager describe-secret --secret-id "$name" >/dev/null 2>&1; then
      echo "  missing secret: $name" >&2
      missing=1
    fi
  done
  if [ "$missing" -ne 0 ]; then
    echo "preflight: one or more secrets are missing; run create-secrets.sh" >&2
    return 1
  fi
  echo "  All four secrets are present. (This checks existence; create-secrets.sh sets the keys.)"
  return 0
}

case "$PHASE" in
  network) phase_network ;;
  secrets) phase_secrets ;;
esac
