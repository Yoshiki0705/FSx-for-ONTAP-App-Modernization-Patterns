#!/usr/bin/env python3
"""Approval-gate estimate generator for the app-modernization Spoke (stdlib only).

An estimate is the thing a human approves before any billed AWS operation. This script builds one
from AWS Price List API unit prices and writes it to .private/estimates/<UTC>-<target>.json with a
Markdown summary. It never performs a billed operation; it only reads prices.

Three jobs, selected by flags:

  --target <t>   Build an estimate for a billed operation. Validates inputs per the design table.
                 --target base additionally rechecks the Single-AZ first-generation minimum storage
                 and throughput and returns exit 4 if the template's fixed 1024 GiB / 128 MBps no
                 longer match the current minimum (so a human decides whether to adjust the fixed
                 values before deploying).
                 --target atx requires --transformation (one of ATX_TRANSFORMATIONS) and
                 --limit-minutes N (a positive integer). The estimate records both as parameters,
                 run-atx.sh passes N to `atx custom def exec --limit`, and the total is the ceiling
                 N x the agent-minute unit price. For AWS/dotnet-modernization the expected charge
                 within the account's monthly no-cost quota is recorded separately as $0, with the
                 caveat that this script cannot observe how much of the quota is left.
                 --target stage3 prices only the add-on stack (Lambda requests and GB-seconds for
                 --invocations manual runs at the template's memory and timeout ceiling, S3 Access
                 Point requests priced at the FSx for ONTAP S3 Access Points PUT/LIST rate as an
                 assumed upper bound, CloudWatch Logs, and a $0 DISABLED schedule) and records the
                 stage-3 deploy parameters, which deploy.sh stage3 passes to create-stack.
  --elapsed      Compute the elapsed fraction of the approved base time from approval.json and
                 return exit 3 when it exceeds 80%. The origin is the approval time of the most
                 recent base approval (largest approved_at); atx / stage3 / teardown / base-update
                 approvals do not move it, extend hours add to the denominator.

Exit codes: 0 ok; 1 Price List API unreachable (no estimate written); 2 invalid input; 3 elapsed
fraction over 80%; 4 (--target base only) the fixed minimum no longer matches the current minimum.

Prices and minimums reach this script through the AWS Price List API in production. In tests they
are supplied as JSON fixtures via --price-fixture and --minimums-fixture, so no test makes a live
call. When neither a fixture nor a reachable API is available, --target returns exit 1 rather than
inventing numbers. The atx unit price key is transform_agent_minute (Price List service
AWSTransform, usagetype APN1-AgentMinute). The stage3 S3 Access Points request keys are read from
Price List service AmazonS3, usagetypes APN1-Requests-FSXONTAP-Tier1 and -Tier2.
"""

from __future__ import annotations

import argparse
import datetime as dt
import ipaddress
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ESTIMATES_DIR = ROOT / ".private" / "estimates"
APPROVAL_FILE = ROOT / ".private" / "runs" / "approval.json"

REGION = "ap-northeast-1"
VALID_TARGETS = ("base", "base-update", "stage3", "atx", "teardown", "extend")
HOURS_REQUIRED = ("base", "stage3", "extend")

# The template's fixed values (held by cfn-guard minimum_fsx). --target base compares these against
# the current documented minimum and returns exit 4 on a mismatch.
FIXED_STORAGE_GIB = 1024
FIXED_THROUGHPUT_MBPS = 128

# Hours of each resource that cannot be stopped, used only to label the estimate; the arithmetic
# multiplies unit price by the requested hours.
HOURS_IN_MONTH = 730.0

# The approved deployment configuration is recorded in the estimate as CloudFormation
# ParameterKey/ParameterValue pairs, so the estimate a human approves and the parameters deploy.sh
# passes to create-stack are the same artifact. The five switchable kinds default to create-new;
# an Existing* ID is required and recorded only when its matching Create<X> is false. The ID
# formats below mirror the AllowedPattern on each parameter in templates/base.yaml (a reused ID is
# validated here before it can reach create-stack).
BOOL_VALUES = ("true", "false")
EXISTING_ID_PATTERNS = {
    "ExistingVpcId": re.compile(r"^vpc-[0-9a-f]{8,}$"),
    "ExistingPrimarySubnetId": re.compile(r"^subnet-[0-9a-f]{8,}$"),
    "ExistingSecondAzSubnetId": re.compile(r"^subnet-[0-9a-f]{8,}$"),
    "ExistingDirectoryId": re.compile(r"^d-[0-9a-f]{10}$"),
}


# The AWS Transform custom transformations an atx estimate may authorize. run-atx.sh holds the same
# two names in a `case`; dryrun_shell_tests.sh fails when the two lists differ.
ATX_TRANSFORMATIONS = (
    "AWS/comprehensive-codebase-analysis",
    "AWS/dotnet-modernization",
)
ATX_DOTNET = "AWS/dotnet-modernization"
# Monthly agent minutes included at no additional charge for AWS/dotnet-modernization
# (https://aws.amazon.com/transform/pricing/, read 2026-10-10). The quota is per account per month;
# how much of it is left cannot be read by this script.
DOTNET_QUOTA_MINUTES = 50000
DOTNET_EXPECTED_BASIS = (
    f"within the {DOTNET_QUOTA_MINUTES:,} agent-minute monthly no-cost quota "
    f"for {ATX_DOTNET}"
)
DOTNET_EXPECTED_CAVEAT = (
    "quota consumption is not observable from this script; "
    "if the quota is exhausted the ceiling applies"
)
LIMIT_MINUTES_PATTERN = re.compile(r"^[1-9][0-9]*$")

# --target stage3 prices only what appmod-stage3 adds to the running base environment; the base
# environment's hours are already covered by the base (and extend) estimates. The stage-3 deploy
# parameters are recorded like base's, and deploy.sh stage3 passes exactly those. The defaults
# below equal the Default values in templates/stage3-serverless.yaml (test_estimate.py fails when
# they diverge); the VPC, subnet, and security group are not parameters (the template imports the
# first two and creates the third).
STAGE3_DEFAULTS = {
    "BaseStackName": "appmod-base",
    "LambdaPackageKey": "lambda/worker.zip",
    "LambdaHandler": "DocIntake.Lambda::DocIntake.Lambda.Function::Handler",
    "LambdaRuntime": "dotnet10",
    "FileSystemIdentityUser": "APPMOD\\appsvc",
}
# The template's LambdaRuntime AllowedValues.
STAGE3_RUNTIMES = ("dotnet10", "provided.al2023")
# The template's WorkerFunction MemorySize (MiB) and Timeout (s): the per-invocation duration
# ceiling the GB-second line is priced at.
STAGE3_MEMORY_MB = 512
STAGE3_TIMEOUT_S = 120
# Manual `aws lambda invoke` runs priced when --invocations is not given. The schedule is created
# DISABLED, so these are the only invocations.
STAGE3_DEFAULT_INVOCATIONS = 20
STAGE3_MAX_INVOCATIONS = 10000
# Stated ceilings, not measurements: S3 requests per invocation (Worker: list, a read and a write per
# inbox document, the index write and read-back; Probe: about ten) and log bytes per invocation.
STAGE3_S3AP_REQUESTS_PER_INVOCATION = 100
STAGE3_LOG_MIB_PER_INVOCATION = 1
# Unit prices confirmed by two routes, both read 2026-10-12 (JST), Tokyo (ap-northeast-1):
#  (1) the English Amazon FSx for NetApp ONTAP pricing page, tab "S3 Access Points for FSx". The tab is
#      rendered client-side, so a plain HTTP fetch does not show it, and the Japanese page did not show
#      it on that date.
#  (2) the AWS Price List API under service code AmazonS3 (not AmazonFSx, which has no S3 Access Points
#      request SKU in this region): usagetype APN1-Requests-FSXONTAP-Tier1, SKU ZE45RAAH9EX5HNT4,
#      $0.00108 per 1,000 PUT/COPY/POST/LIST requests; usagetype APN1-Requests-FSXONTAP-Tier2, SKU
#      W647VRU94Q4WFUS7, $0.000029 per 1,000 GET and all other requests. The sibling usagetypes
#      APN1-Requests-FSXOPENZFS-Tier1/Tier2 belong to a different product and are not used.
# Evidence tier: documented (two vendor sources). Nobody has been billed, so it is not verified.
# Amazon S3 list prices are higher and are NOT used.
FSX_S3AP_REQUEST_SOURCE = (
    "AWS Price List API, service code AmazonS3, usagetype APN1-Requests-FSXONTAP-Tier1/Tier2, "
    "SKU ZE45RAAH9EX5HNT4/W647VRU94Q4WFUS7, read 2026-10-12; matches the FSx for ONTAP pricing page "
    "tab 'S3 Access Points for FSx' (https://aws.amazon.com/fsx/netapp-ontap/pricing/ , English page, "
    "Asia Pacific (Tokyo) ap-northeast-1). Tier1: $0.00108 per 1,000 PUT/COPY/POST/LIST requests; "
    "Tier2: $0.000029 per 1,000 GET and all other requests. The pricing page tab is rendered "
    "client-side and is not visible to a plain HTTP fetch. Amazon S3 list prices are higher and were "
    "not used"
)
S3AP_REQUEST_CEILING_NOTE = (
    "upper bound: every assumed request is priced at the PUT/COPY/POST/LIST rate "
    "(fsx_s3ap_put_list_request); the GET and all-other rate (fsx_s3ap_get_request) is lower and "
    "shown on the next line for reference only. Documented by the pricing page and the Price List "
    "API, read 2026-10-12; not confirmed billing"
)
S3_DATA_TRANSFER_NOTE = (
    "not included in the total: data transfer through an S3 Access Point is billed as for an S3 "
    "bucket (https://aws.amazon.com/fsx/netapp-ontap/pricing/, read 2026-10-12), and the volume "
    "depends on object sizes this estimate does not assume"
)
STACK_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9-]{0,127}$")
PACKAGE_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,1000}\.zip$")
HANDLER_PATTERN = re.compile(r"^[A-Za-z0-9_.:]{1,128}$")
IDENTITY_USER_PATTERN = re.compile(r"^([A-Za-z0-9._-]{1,15}\\)?[A-Za-z0-9._-]{1,20}$")
BUCKET_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
INVOCATIONS_PATTERN = re.compile(r"^[1-9][0-9]*$")


class InputError(ValueError):
    """Raised for an invalid command-line input (exit code 2)."""


def _utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _parse_iso(value: str) -> dt.datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = dt.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed


def _parse_cidr(label: str, value: str) -> ipaddress.IPv4Network:
    """Parse an IPv4 CIDR with the network bits set (strict). Raises InputError (exit 2)."""
    try:
        return ipaddress.ip_network(value, strict=True)
    except ValueError as exc:
        raise InputError(
            f"{label} is not a well-formed IPv4 CIDR: {value!r} ({exc})"
        ) from exc


def _require_bool(label: str, value: str) -> str:
    text = value.strip().lower()
    if text not in BOOL_VALUES:
        raise InputError(
            f"{label} must be one of {', '.join(BOOL_VALUES)} (got {value!r})"
        )
    return text


def build_base_parameters(args: argparse.Namespace) -> list[dict[str, str]]:
    """Return the CloudFormation parameters for a base deploy as ordered ParameterKey/Value pairs.

    The five switchable kinds are always recorded (Create<X>=true|false). When a kind is created
    new, the three CIDRs are validated (well-formed, and both subnet CIDRs inside the VPC CIDR) and
    recorded. When a kind is reused, its Existing* ID(s) must be present and well-formed and are
    recorded instead. Parameters with safe template defaults (NamePrefix, EgressMode, ...) are
    recorded only when overridden on the command line. Raises InputError (exit 2) on any violation.
    """
    create_vpc = _require_bool("--create-vpc", args.create_vpc)
    create_subnets = _require_bool("--create-subnets", args.create_subnets)
    create_interface_endpoints = _require_bool(
        "--create-interface-endpoints", args.create_interface_endpoints
    )
    create_s3_gateway_endpoint = _require_bool(
        "--create-s3-gateway-endpoint", args.create_s3_gateway_endpoint
    )
    create_directory = _require_bool("--create-directory", args.create_directory)

    # A dedicated new VPC always creates its own subnets (base.yaml: CreateVpc true requires
    # CreateSubnets true). Reject the unsupported combination here rather than at create-stack.
    if create_vpc == "true" and create_subnets == "false":
        raise InputError(
            "--create-vpc true requires --create-subnets true "
            "(a newly created VPC always creates its own subnets)"
        )

    params: list[dict[str, str]] = []

    def add(key: str, value: str) -> None:
        params.append({"ParameterKey": key, "ParameterValue": value})

    def require_existing(flag_label: str, key: str, value: str) -> str:
        text = (value or "").strip()
        if not text:
            raise InputError(f"{flag_label} is false; {key} is required")
        pattern = EXISTING_ID_PATTERNS[key]
        if not pattern.fullmatch(text):
            raise InputError(f"{key} is not well-formed: {text!r}")
        return text

    # VPC and subnets: validate CIDRs on create, existing IDs on reuse.
    add("CreateVpc", create_vpc)
    add("CreateSubnets", create_subnets)
    if create_vpc == "true":
        vpc_net = _parse_cidr("--vpc-cidr", args.vpc_cidr)
        add("VpcCidr", str(vpc_net))
    else:
        add(
            "ExistingVpcId",
            require_existing("--create-vpc", "ExistingVpcId", args.existing_vpc_id),
        )
        vpc_net = None

    if create_subnets == "true":
        primary_net = _parse_cidr("--primary-subnet-cidr", args.primary_subnet_cidr)
        second_net = _parse_cidr("--second-az-subnet-cidr", args.second_az_subnet_cidr)
        if vpc_net is not None:
            # Basic containment: each created subnet CIDR must sit inside the created VPC CIDR.
            if not primary_net.subnet_of(vpc_net):
                raise InputError(
                    f"--primary-subnet-cidr {primary_net} is not inside --vpc-cidr {vpc_net}"
                )
            if not second_net.subnet_of(vpc_net):
                raise InputError(
                    f"--second-az-subnet-cidr {second_net} is not inside --vpc-cidr {vpc_net}"
                )
        add("PrimarySubnetCidr", str(primary_net))
        add("SecondAzSubnetCidr", str(second_net))
    else:
        add(
            "ExistingPrimarySubnetId",
            require_existing(
                "--create-subnets",
                "ExistingPrimarySubnetId",
                args.existing_primary_subnet_id,
            ),
        )
        add(
            "ExistingSecondAzSubnetId",
            require_existing(
                "--create-subnets",
                "ExistingSecondAzSubnetId",
                args.existing_second_az_subnet_id,
            ),
        )

    # Endpoints.
    add("CreateInterfaceEndpoints", create_interface_endpoints)
    add("CreateS3GatewayEndpoint", create_s3_gateway_endpoint)
    # RouteTableIds is required when the S3 gateway endpoint is created against a reused VPC.
    if create_s3_gateway_endpoint == "true" and create_vpc == "false":
        route_tables = (args.route_table_ids or "").strip()
        if not route_tables:
            raise InputError(
                "--create-s3-gateway-endpoint true with --create-vpc false requires "
                "--route-table-ids"
            )
        add("RouteTableIds", route_tables)

    # Directory.
    add("CreateDirectory", create_directory)
    if create_directory == "false":
        add(
            "ExistingDirectoryId",
            require_existing(
                "--create-directory", "ExistingDirectoryId", args.existing_directory_id
            ),
        )
        dns_ips = (args.existing_directory_dns_ips or "").strip()
        if not dns_ips:
            raise InputError(
                "--create-directory is false; --existing-directory-dns-ips is required"
            )
        for ip in [p.strip() for p in dns_ips.split(",") if p.strip()]:
            try:
                ipaddress.IPv4Address(ip)
            except ValueError as exc:
                raise InputError(
                    f"--existing-directory-dns-ips has a bad address {ip!r}: {exc}"
                ) from exc
        add("ExistingDirectoryDnsIps", dns_ips)

    # EgressMode is a base.yaml parameter with a safe default; record it when it was supplied.
    if args.egress_mode is not None:
        add("EgressMode", args.egress_mode)

    # Other base.yaml parameters that have safe defaults are recorded only when overridden.
    for key, value in (("NamePrefix", args.name_prefix),):
        if value is not None:
            add(key, value)

    return params


def validate_target_args(args: argparse.Namespace) -> None:
    """Validate --target inputs per the design table. Raises InputError (exit 2)."""
    if args.target not in VALID_TARGETS:
        raise InputError(f"--target must be one of {', '.join(VALID_TARGETS)}")
    if args.region is not None and args.region != REGION:
        raise InputError(f"--region must be {REGION} (got {args.region!r})")
    if args.target in HOURS_REQUIRED:
        if args.hours is None:
            raise InputError(f"--hours is required for --target {args.target}")
        if not (1 <= args.hours <= 240):
            raise InputError("--hours must be an integer from 1 to 240")
    if args.target == "base":
        if args.egress_mode is None:
            raise InputError("--egress-mode is required for --target base")
        if args.egress_mode not in ("nat", "endpoints"):
            raise InputError("--egress-mode must be nat or endpoints")
    if args.target != "stage3":
        # Same reasoning as the atx flags below: a stage-3 flag on another target would do nothing.
        for flag, attr in STAGE3_FLAGS:
            if getattr(args, attr, None) is not None:
                raise InputError(f"{flag} applies only to --target stage3")
    transformation = getattr(args, "transformation", None)
    limit_minutes = getattr(args, "limit_minutes", None)
    if args.target == "atx":
        if transformation is None:
            raise InputError(
                "--transformation is required for --target atx "
                f"(one of {', '.join(ATX_TRANSFORMATIONS)})"
            )
        if transformation not in ATX_TRANSFORMATIONS:
            raise InputError(
                f"--transformation must be one of {', '.join(ATX_TRANSFORMATIONS)} "
                f"(got {transformation!r})"
            )
        if limit_minutes is None:
            raise InputError(
                "--limit-minutes is required for --target atx "
                "(the agent-minute cap passed to atx --limit)"
            )
        if not LIMIT_MINUTES_PATTERN.fullmatch(limit_minutes):
            raise InputError(
                f"--limit-minutes must be a positive integer (got {limit_minutes!r})"
            )
    else:
        # A stray flag on another target would silently do nothing; refuse it instead.
        for flag, value in (
            ("--transformation", transformation),
            ("--limit-minutes", limit_minutes),
        ):
            if value is not None:
                raise InputError(f"{flag} applies only to --target atx")


def build_atx_parameters(args: argparse.Namespace) -> list[dict[str, str]]:
    """Return the atx run configuration as ParameterKey/Value pairs (same shape as base).

    run-atx.sh reads Transformation and LimitMinutes from these pairs and nowhere else, so the cap a
    human approved is the cap atx runs with. validate_target_args has already checked both values.
    """
    return [
        {"ParameterKey": "Transformation", "ParameterValue": args.transformation},
        {"ParameterKey": "LimitMinutes", "ParameterValue": args.limit_minutes},
    ]


STAGE3_FLAGS = (
    ("--base-stack-name", "base_stack_name"),
    ("--lambda-package-key", "lambda_package_key"),
    ("--lambda-handler", "lambda_handler"),
    ("--lambda-runtime", "lambda_runtime"),
    ("--file-system-identity-user", "file_system_identity_user"),
    ("--artifacts-bucket-name", "artifacts_bucket_name"),
    ("--invocations", "invocations"),
)


def stage3_invocations(args: argparse.Namespace) -> int:
    """Return the number of manual invocations to price. Raises InputError (exit 2)."""
    value = getattr(args, "invocations", None)
    if value is None:
        return STAGE3_DEFAULT_INVOCATIONS
    if not INVOCATIONS_PATTERN.fullmatch(value) or int(value) > STAGE3_MAX_INVOCATIONS:
        raise InputError(
            f"--invocations must be an integer from 1 to {STAGE3_MAX_INVOCATIONS} "
            f"(got {value!r})"
        )
    return int(value)


def build_stage3_parameters(args: argparse.Namespace) -> list[dict[str, str]]:
    """Return the CloudFormation parameters for a stage3 deploy as ParameterKey/Value pairs.

    BaseStackName, LambdaPackageKey, LambdaHandler, LambdaRuntime, and FileSystemIdentityUser are
    always recorded (the approval shows the file-system identity the access point maps to);
    ArtifactsBucketName only when given, since the template's empty default imports the base
    stack's bucket. No value may contain a comma: deploy.sh passes the pairs in the CLI shorthand
    ParameterKey=<k>,ParameterValue=<v>. Raises InputError (exit 2) on any violation.
    """

    def value(attr: str, key: str) -> str:
        given = getattr(args, attr, None)
        text = STAGE3_DEFAULTS[key] if given is None else given.strip()
        if not text:
            raise InputError(f"{key} must not be empty")
        return text

    checks = (
        ("base_stack_name", "BaseStackName", STACK_NAME_PATTERN),
        ("lambda_package_key", "LambdaPackageKey", PACKAGE_KEY_PATTERN),
        ("lambda_handler", "LambdaHandler", HANDLER_PATTERN),
        ("lambda_runtime", "LambdaRuntime", None),
        ("file_system_identity_user", "FileSystemIdentityUser", IDENTITY_USER_PATTERN),
    )
    params: list[dict[str, str]] = []
    for attr, key, pattern in checks:
        text = value(attr, key)
        if key == "LambdaRuntime":
            if text not in STAGE3_RUNTIMES:
                raise InputError(
                    f"--lambda-runtime must be one of {', '.join(STAGE3_RUNTIMES)} (got {text!r})"
                )
        elif not pattern.fullmatch(text) or ".." in text:
            raise InputError(f"{key} is not well-formed: {text!r}")
        params.append({"ParameterKey": key, "ParameterValue": text})

    bucket = getattr(args, "artifacts_bucket_name", None)
    if bucket is not None:
        text = bucket.strip()
        if not BUCKET_NAME_PATTERN.fullmatch(text) or ".." in text:
            raise InputError(f"ArtifactsBucketName is not well-formed: {text!r}")
        params.append({"ParameterKey": "ArtifactsBucketName", "ParameterValue": text})
    return params


def load_prices(fixture: Path | None) -> dict[str, float] | None:
    """Return a mapping of price keys to unit prices, or None if unavailable.

    In production this would call the AWS Price List API (the pricing endpoint is us-east-1). Here it
    is read from a fixture in tests. Returning None signals 'API unreachable' and causes exit 1; it
    never falls back to remembered values. A fixture key that starts with "_" (for example
    "_sources", which records where a price was read) is metadata and is not a price.
    """
    if fixture is not None:
        data = json.loads(fixture.read_text(encoding="utf-8"))
        return {
            str(k): float(v) if v is not None else None
            for k, v in data.items()
            if not str(k).startswith("_")
        }
    # No live call is made here; a caller without a fixture is treated as unreachable.
    return None


def load_minimums(fixture: Path | None) -> dict[str, int] | None:
    """Return {'storage_gib': int, 'throughput_mbps': int} or None if unavailable."""
    if fixture is not None:
        data = json.loads(fixture.read_text(encoding="utf-8"))
        return {
            "storage_gib": int(data["storage_gib"]),
            "throughput_mbps": int(data["throughput_mbps"]),
        }
    return None


def stage3_line_items(
    prices: dict[str, float], invocations: int
) -> list[dict[str, object]]:
    """Return the stage-3 add-on line items: usage ceilings for `invocations` manual runs.

    Price keys and their Price List source (ap-northeast-1, OnDemand):
      lambda_request    AWSLambda, usagetype APN1-Request (per request)
      lambda_gb_second  AWSLambda, usagetype APN1-Lambda-GB-Second, first tier (x86_64)
      cwl_ingest_gb     AmazonCloudWatch, usagetype APN1-DataProcessing-Bytes (Standard log class)
      fsx_s3ap_put_list_request  AmazonS3, usagetype APN1-Requests-FSXONTAP-Tier1 (SKU
                        ZE45RAAH9EX5HNT4); same value as the FSx for ONTAP pricing page tab "S3 Access
                        Points for FSx" (FSX_S3AP_REQUEST_SOURCE), per request: PUT/COPY/POST/LIST.
                        Prices every assumed S3 Access Points request, as an upper bound
      fsx_s3ap_get_request  AmazonS3, usagetype APN1-Requests-FSXONTAP-Tier2 (SKU W647VRU94Q4WFUS7),
                        per request: GET and all other requests. Reference line only, not in the
                        total
      A price key that is absent is recorded as "not retrieved" with a null subtotal, never
      defaulted.
    The Lambda free tier is not applied. The base environment is not re-priced here.
    """
    gb_seconds = invocations * (STAGE3_MEMORY_MB / 1024) * STAGE3_TIMEOUT_S
    s3ap_requests = invocations * STAGE3_S3AP_REQUESTS_PER_INVOCATION
    log_gb = invocations * STAGE3_LOG_MIB_PER_INVOCATION / 1024

    def usage(
        label: str, key: str, quantity: float, unit: str, **extra: object
    ) -> dict:
        price = prices.get(key)
        # An explanatory note applies only when the price was found; an absent price is always
        # recorded as "not retrieved" with a null subtotal, never defaulted.
        explanation = str(extra.pop("note", ""))
        item: dict[str, object] = {
            "label": label,
            "unit_usd": price,
            "quantity": quantity,
            "unit": unit,
            "hours": 0,
            # Six places: a few manual runs cost fractions of a cent, which two places would hide.
            "subtotal_usd": None if price is None else round(price * quantity, 6),
            "note": "not retrieved" if price is None else explanation,
        }
        item.update(extra)
        return item

    return [
        usage(
            f"AWS Lambda requests (ceiling, {invocations} manual invocations)",
            "lambda_request",
            invocations,
            "requests",
        ),
        usage(
            f"AWS Lambda duration (ceiling, {invocations} x {STAGE3_MEMORY_MB} MiB x "
            f"{STAGE3_TIMEOUT_S} s)",
            "lambda_gb_second",
            gb_seconds,
            "GB-seconds",
        ),
        usage(
            f"FSx for ONTAP S3 Access Points requests (ceiling, "
            f"{STAGE3_S3AP_REQUESTS_PER_INVOCATION} per invocation, assumed, at the "
            "PUT/LIST rate)",
            "fsx_s3ap_put_list_request",
            s3ap_requests,
            "requests",
            source=FSX_S3AP_REQUEST_SOURCE,
            note=S3AP_REQUEST_CEILING_NOTE,
        ),
        usage(
            "FSx for ONTAP S3 Access Points requests (GET rate, reference only, not in the total)",
            "fsx_s3ap_get_request",
            s3ap_requests,
            "requests",
            reference_usd=None
            if prices.get("fsx_s3ap_get_request") is None
            else round(prices["fsx_s3ap_get_request"] * s3ap_requests, 6),
            subtotal_usd=None,
            note="reference only: the same assumed requests at the GET rate; not added to the "
            "total",
        ),
        usage(
            f"Amazon CloudWatch Logs ingestion (ceiling, {STAGE3_LOG_MIB_PER_INVOCATION} MiB "
            "per invocation, assumed)",
            "cwl_ingest_gb",
            round(log_gb, 6),
            "GB",
        ),
        {
            "label": "Amazon EventBridge Scheduler (schedule created DISABLED)",
            "unit_usd": 0.0,
            "quantity": 0,
            "unit": "invocations",
            "hours": 0,
            "subtotal_usd": 0.0,
            "note": "the schedule is DISABLED, so it invokes nothing; manual runs are priced above",
        },
        {
            "label": "S3 data transfer through the S3 Access Point",
            "unit_usd": None,
            "quantity": None,
            "unit": "GB",
            "hours": 0,
            "subtotal_usd": None,
            "note": S3_DATA_TRANSFER_NOTE,
        },
    ]


def build_line_items(
    prices: dict[str, float],
    target: str,
    hours: int,
    egress_mode: str | None,
    transformation: str | None = None,
    limit_minutes: int | None = None,
    invocations: int = STAGE3_DEFAULT_INVOCATIONS,
) -> list[dict[str, object]]:
    """Return estimate line items. A missing price is recorded as not-retrieved, never guessed."""
    items: list[dict[str, object]] = []

    if target == "stage3":
        return stage3_line_items(prices, invocations)

    if target == "atx" and limit_minutes is not None:
        # The ceiling: atx stops (exit 2) once the run has used limit_minutes agent minutes.
        unit = prices.get("transform_agent_minute")
        items.append(
            {
                "label": f"AWS Transform custom agent minutes (ceiling, --limit {limit_minutes})",
                "transformation": transformation,
                "unit_usd": unit,
                "agent_minutes": limit_minutes,
                "hours": 0,
                "subtotal_usd": None
                if unit is None
                else round(unit * limit_minutes, 2),
                "note": "not retrieved" if unit is None else "",
            }
        )
        if transformation == ATX_DOTNET:
            # Expected charge inside the no-cost quota. subtotal_usd stays null so it never adds
            # to (or replaces) the ceiling in total_usd.
            items.append(
                {
                    "label": "AWS Transform custom agent minutes (expected, within the monthly quota)",
                    "transformation": transformation,
                    "unit_usd": 0.0,
                    "agent_minutes": limit_minutes,
                    "hours": 0,
                    "subtotal_usd": None,
                    "note": f"{DOTNET_EXPECTED_BASIS}; {DOTNET_EXPECTED_CAVEAT}",
                }
            )
        return items

    def monthly_to_hourly(key: str, qty: float) -> float | None:
        unit = prices.get(key)
        if unit is None:
            return None
        return unit * qty / HOURS_IN_MONTH

    def add(label: str, hourly: float | None, note: str = "") -> None:
        items.append(
            {
                "label": label,
                "hourly_usd": None if hourly is None else round(hourly, 4),
                "hours": hours,
                "subtotal_usd": None if hourly is None else round(hourly * hours, 2),
                "note": note or ("not retrieved" if hourly is None else ""),
            }
        )

    if target in ("base", "extend"):
        add(
            "FSx for ONTAP SSD (1,024 GiB)",
            monthly_to_hourly("fsx_ssd_gb_month", FIXED_STORAGE_GIB),
        )
        add(
            "FSx for ONTAP throughput (128 MBps)",
            monthly_to_hourly("fsx_throughput_mbps_month", FIXED_THROUGHPUT_MBPS),
        )
        ad_unit = prices.get("ad_standard_dc_hour")
        add(
            "AWS Managed Microsoft AD (Standard, 2 DC)",
            None if ad_unit is None else ad_unit * 2,
        )
        add("Windows EC2 (t3.large)", prices.get("ec2_t3_large_hour"))
        add("Linux EC2 (t3.medium)", prices.get("ec2_t3_medium_hour"))
        add("EBS gp3 (70 GiB)", monthly_to_hourly("ebs_gp3_gb_month", 70))
        if egress_mode == "endpoints":
            unit = prices.get("vpce_interface_az_hour")
            hourly = None if unit is None else unit * 6
            add("Interface VPC endpoints (6 services x 1 AZ)", hourly)
        add(
            "Secrets Manager (4 secrets)",
            monthly_to_hourly("secrets_manager_secret_month", 4),
        )
    return items


def write_estimate(
    target: str,
    hours: int,
    egress_mode: str | None,
    items: list[dict[str, object]],
    minimum_mismatch: dict[str, int] | None,
    parameters: list[dict[str, str]] | None,
) -> Path:
    ESTIMATES_DIR.mkdir(parents=True, exist_ok=True)
    created = _utc_now()
    stamp = created.strftime("%Y%m%dT%H%M%SZ")
    payload = {
        "target": target,
        "region": REGION,
        "created_at": created.isoformat().replace("+00:00", "Z"),
        "hours": hours,
        "egress_mode": egress_mode,
        "price_source": "AWS Price List API",
        "note": "Unit price x hours is arithmetic, not an invoice figure.",
        "line_items": items,
        "minimum_recheck": minimum_mismatch,
    }
    # The CloudFormation parameters the approved estimate authorizes. deploy.sh reads this object
    # and passes exactly these to create-stack; an estimate without it is refused rather than
    # deployed with template defaults.
    # For atx the same object carries Transformation and LimitMinutes, which run-atx.sh reads.
    # For stage3 it carries the stage-3 template parameters, which deploy.sh stage3 passes.
    if parameters is not None:
        payload["parameters"] = parameters
        pairs = {p["ParameterKey"]: p["ParameterValue"] for p in parameters}
        if target == "atx" and pairs.get("Transformation") == ATX_DOTNET:
            # total_usd below stays the ceiling; this records what is expected inside the quota.
            payload["expected"] = {
                "usd": 0.0,
                "basis": DOTNET_EXPECTED_BASIS,
                "caveat": DOTNET_EXPECTED_CAVEAT,
            }
    subtotals = [i["subtotal_usd"] for i in items if i["subtotal_usd"] is not None]
    payload["total_usd"] = round(sum(subtotals), 2) if subtotals else None
    json_path = ESTIMATES_DIR / f"{stamp}-{target}.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return json_path


def elapsed_fraction(approval_file: Path) -> float:
    """Return elapsed-time fraction against the base approval. Raises InputError (exit 2).

    The origin is the approval time of the most recent base approval (largest approved_at); older
    base approvals are superseded. extend hours add to the denominator. atx / stage3 /
    teardown / base-update approvals do not change the origin or the denominator.
    """
    if not approval_file.exists():
        raise InputError(
            f"{approval_file} not found; no base approval to measure against"
        )
    try:
        approvals = json.loads(approval_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise InputError(f"{approval_file} is not valid JSON: {exc}") from exc
    if not isinstance(approvals, list):
        raise InputError("approval.json must be a JSON array of approval records")

    base_records = [a for a in approvals if a.get("target") == "base"]
    if not base_records:
        raise InputError("approval.json has no 'base' approval")
    try:
        base = max(base_records, key=lambda a: _parse_iso(a["approved_at"]))
        origin = _parse_iso(base["approved_at"])
        denom = float(base["hours"])
    except (KeyError, ValueError) as exc:
        raise InputError(
            f"base approval is missing a valid approved_at/hours: {exc}"
        ) from exc

    for approval in approvals:
        if approval.get("target") == "extend":
            try:
                denom += float(approval["hours"])
            except (KeyError, ValueError) as exc:
                raise InputError(f"extend approval missing hours: {exc}") from exc

    if denom <= 0:
        raise InputError("approved hours sum to zero")
    elapsed = (_utc_now() - origin).total_seconds() / 3600.0
    return elapsed / denom


def cmd_elapsed(args: argparse.Namespace) -> int:
    approval_file = Path(args.approval_file) if args.approval_file else APPROVAL_FILE
    try:
        fraction = elapsed_fraction(approval_file)
    except InputError as exc:
        print(f"estimate: {exc}", file=sys.stderr)
        return 2
    percent = fraction * 100
    print(f"elapsed: {percent:.1f}% of approved base time")
    if fraction > 0.80:
        print(
            "estimate: over 80% of approved time; present an extend estimate before continuing",
            file=sys.stderr,
        )
        return 3
    return 0


def cmd_target(args: argparse.Namespace) -> int:
    try:
        validate_target_args(args)
        # For base, resolve the deployment configuration into CloudFormation parameters before any
        # price is fetched, so an invalid configuration fails (exit 2) without a billed lookup.
        # atx records its transformation and agent-minute limit the same way.
        # stage3 records its deploy parameters the same way, and deploy.sh stage3 passes them.
        parameters = None
        invocations = STAGE3_DEFAULT_INVOCATIONS
        if args.target == "base":
            parameters = build_base_parameters(args)
        elif args.target == "atx":
            parameters = build_atx_parameters(args)
        elif args.target == "stage3":
            parameters = build_stage3_parameters(args)
            invocations = stage3_invocations(args)
    except InputError as exc:
        print(f"estimate: {exc}", file=sys.stderr)
        return 2

    price_fixture = Path(args.price_fixture) if args.price_fixture else None
    prices = load_prices(price_fixture)
    if prices is None:
        print(
            "estimate: AWS Price List API is unreachable and no --price-fixture given; "
            "no estimate written",
            file=sys.stderr,
        )
        return 1

    hours = args.hours if args.hours is not None else 0

    minimum_mismatch = None
    exit_code = 0
    if args.target == "base":
        minimums = load_minimums(
            Path(args.minimums_fixture) if args.minimums_fixture else None
        )
        if minimums is None:
            # Could not re-fetch the minimum. Record that and let a human decide (not exit 4).
            minimum_mismatch = {
                "note": "minimum not retrieved; verify before deploying"
            }
        elif (
            minimums["storage_gib"] != FIXED_STORAGE_GIB
            or minimums["throughput_mbps"] != FIXED_THROUGHPUT_MBPS
        ):
            minimum_mismatch = {
                "fixed_storage_gib": FIXED_STORAGE_GIB,
                "current_storage_gib": minimums["storage_gib"],
                "fixed_throughput_mbps": FIXED_THROUGHPUT_MBPS,
                "current_throughput_mbps": minimums["throughput_mbps"],
            }
            exit_code = 4

    items = build_line_items(
        prices,
        args.target,
        hours,
        args.egress_mode,
        transformation=args.transformation,
        limit_minutes=int(args.limit_minutes) if args.limit_minutes else None,
        invocations=invocations,
    )
    path = write_estimate(
        args.target, hours, args.egress_mode, items, minimum_mismatch, parameters
    )
    try:
        shown = path.relative_to(ROOT)
    except ValueError:
        shown = path
    print(f"estimate written: {shown}")
    if exit_code == 4:
        print(
            "estimate: the fixed minimum no longer matches the current minimum; "
            "decide whether to adjust the template and cfn-guard before deploying",
            file=sys.stderr,
        )
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    # No argparse choices on --target / --egress-mode: validate_target_args returns exit 2 for a bad
    # value, giving one uniform invalid-input path rather than argparse's own SystemExit.
    parser.add_argument("--target")
    parser.add_argument("--hours", type=int)
    parser.add_argument("--egress-mode")
    parser.add_argument("--region")
    parser.add_argument("--elapsed", action="store_true")

    # --target base deployment configuration. The five Create<X> switches default to the all-new
    # dedicated environment; CIDRs default to the approved dedicated-VPC values. Existing* IDs are
    # read only when the matching Create<X> is false. Validated by build_base_parameters and
    # recorded in the estimate as the CloudFormation parameters deploy.sh will pass.
    base = parser.add_argument_group("base deployment configuration (--target base)")
    base.add_argument("--create-vpc", default="true")
    base.add_argument("--create-subnets", default="true")
    base.add_argument("--create-interface-endpoints", default="true")
    base.add_argument("--create-s3-gateway-endpoint", default="true")
    base.add_argument("--create-directory", default="true")
    # Approved dedicated-VPC CIDRs for this run (10.90.0.0/16 confirmed non-overlapping).
    base.add_argument("--vpc-cidr", default="10.90.0.0/16")
    base.add_argument("--primary-subnet-cidr", default="10.90.0.0/24")
    base.add_argument("--second-az-subnet-cidr", default="10.90.1.0/24")
    base.add_argument("--existing-vpc-id")
    base.add_argument("--existing-primary-subnet-id")
    base.add_argument("--existing-second-az-subnet-id")
    base.add_argument("--existing-directory-id")
    base.add_argument("--existing-directory-dns-ips")
    base.add_argument("--route-table-ids")
    base.add_argument("--name-prefix")

    # --target atx run configuration. Both are required for atx and refused for other targets.
    # --limit-minutes is parsed as a string and validated in validate_target_args, so a bad value
    # takes the same exit-2 path as every other invalid input.
    atx = parser.add_argument_group("atx run configuration (--target atx)")
    atx.add_argument("--transformation")
    atx.add_argument("--limit-minutes")

    # --target stage3 deployment configuration. Unset flags take STAGE3_DEFAULTS (the template's
    # Default values); every flag is refused on other targets. --invocations is parsed as a string
    # and validated, like --limit-minutes.
    stage3 = parser.add_argument_group(
        "stage3 deployment configuration (--target stage3)"
    )
    stage3.add_argument("--base-stack-name")
    stage3.add_argument("--lambda-package-key")
    stage3.add_argument("--lambda-handler")
    stage3.add_argument("--lambda-runtime")
    stage3.add_argument("--file-system-identity-user")
    stage3.add_argument("--artifacts-bucket-name")
    stage3.add_argument("--invocations")

    parser.add_argument(
        "--price-fixture", help="JSON of unit prices (tests only; no live call)"
    )
    parser.add_argument(
        "--minimums-fixture", help="JSON of current minimum storage/throughput"
    )
    parser.add_argument("--approval-file", help="override approval.json path (tests)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.elapsed:
        if args.target is not None:
            print("estimate: --elapsed is exclusive of --target", file=sys.stderr)
            return 2
        return cmd_elapsed(args)
    if args.target is None:
        print("estimate: one of --target or --elapsed is required", file=sys.stderr)
        return 2
    return cmd_target(args)


if __name__ == "__main__":
    sys.exit(main())
