#!/usr/bin/env python3
"""Regenerate the unit-price fixture that scripts/estimate.py reads (stdlib + the aws CLI).

estimate.py never calls the AWS Price List API; it reads a JSON fixture passed with
--price-fixture. This script produces that fixture from the Price List API so the prices behind an
estimate can be re-read instead of remembered. Every price key carries the SKU, effective date,
unit and read date it came from in "_sources".

Read-only: the only AWS call is `aws pricing get-products`. The Price List endpoint is in
us-east-1; the usagetypes below are for ap-northeast-1 (APN1-...), selected with the regionCode
filter. Credentials come from the caller's own session. When the session has expired the script
stops with exit 20; run `aws sso login` yourself, it does not look for another credential path.

A key is written only when exactly one price remains after the filters and the tier rule. Zero or
several candidates stop the run (exit 3) and every candidate is printed; the script never takes
"the first". A live value more than 10x away from the reference value recorded in KEYS (the value
read on 2026-10-12) stops the run (exit 4) because that usually means a unit mismatch, for
example per 1,000 requests against per request. A smaller change is printed and recorded.

The minimum storage and throughput file for `estimate.py --minimums-fixture` is not a Price List
value (it comes from the FSx for ONTAP quotas page), so this script does not write it.

Exit codes: 0 success; 2 bad argument or existing output file; 3 no unique price for a key;
4 live value out of the reference range; 10 the aws call failed or returned an unexpected shape;
20 expired credentials.

Examples:
  python3 scripts/fetch-prices.py --set all --dry-run
  python3 scripts/fetch-prices.py --set all
  python3 scripts/fetch-prices.py --explain ec2_t3_large_hour
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

PRICE_LIST_REGION = "us-east-1"
DEFAULT_REGION_CODE = "ap-northeast-1"
GENERATED_BY = "scripts/fetch-prices.py"
SETS = ("base", "atx", "stage3")
REGION_CODE_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
DEFAULT_OUT_DIR = Path(".private/estimates/fixtures")
DESCRIPTION_LIMIT = 120
DRIFT_FACTOR = Decimal(10)
AWS_TIMEOUT_SECONDS = 120

EXIT_OK = 0
EXIT_BAD_ARGUMENT = 2
EXIT_NO_UNIQUE_PRICE = 3
EXIT_OUT_OF_RANGE = 4
EXIT_AWS_FAILED = 10
EXIT_EXPIRED_TOKEN = 20

EXPIRED_MARKERS = (
    "Token has expired",
    "ExpiredToken",
    "SSO session associated with this profile has expired",
    "Error loading SSO Token",
)
EXPIRED_MESSAGE = (
    "error: AWS credentials have expired. Run `aws sso login` yourself and run this again; "
    "this script does not try another credential path."
)


@dataclass(frozen=True)
class KeySpec:
    """One estimate.py price key and how to find its single Price List entry."""

    key: str
    set_name: str
    service_code: str
    filters: tuple[tuple[str, str], ...]
    # "single": exactly one price dimension must remain. "first_tier": keep the dimension whose
    # beginRange is 0 (the first pricing tier) and require exactly one.
    tier: str
    # The value read on 2026-10-12 (see scripts/tests/fixtures/prices.json). Range check only.
    reference: Decimal
    # The SKU read earlier, a cross-check only: a different live SKU is a warning, not an error.
    expected_skus: tuple[str, ...] = ()


# Order follows scripts/tests/fixtures/prices.json so the output reads like the fixture.
KEYS: tuple[KeySpec, ...] = (
    KeySpec(
        "fsx_ssd_gb_month",
        "base",
        "AmazonFSx",
        (
            ("usagetype", "APN1-Storage.SAZ_2N:SSD"),
            ("operation", "CreateFileSystem:ONTAP"),
        ),
        "single",
        Decimal("0.150"),
        ("4M9T5U5EGURPEF6X",),
    ),
    KeySpec(
        "fsx_throughput_mbps_month",
        "base",
        "AmazonFSx",
        (
            ("usagetype", "APN1-ThroughputCapacity.SAZ_2N"),
            ("operation", "CreateFileSystem:ONTAP"),
        ),
        "single",
        Decimal("0.906"),
        ("79GNR3MAFQUJHR6Q",),
    ),
    KeySpec(
        "ad_standard_dc_hour",
        "base",
        "AWSDirectoryService",
        (("usagetype", "APN1-Std-MicrosoftAD-DC-Usage"),),
        "single",
        Decimal("0.073"),
        ("77GMZ4UGPKPPTTKH",),
    ),
    # The licenseModel value below was not re-read against the live Price List. If it is wrong the
    # filters match nothing or several SKUs and the run stops with exit 3; use
    # `--explain ec2_t3_large_hour` to see the candidates and correct the value here.
    KeySpec(
        "ec2_t3_large_hour",
        "base",
        "AmazonEC2",
        (
            ("instanceType", "t3.large"),
            ("operatingSystem", "Windows"),
            ("tenancy", "Shared"),
            ("preInstalledSw", "NA"),
            ("capacitystatus", "Used"),
            ("licenseModel", "No License required"),
        ),
        "single",
        Decimal("0.1364"),
        ("3AHMWXGBEX6HSCMQ",),
    ),
    KeySpec(
        "ec2_t3_medium_hour",
        "base",
        "AmazonEC2",
        (
            ("instanceType", "t3.medium"),
            ("operatingSystem", "Linux"),
            ("tenancy", "Shared"),
            ("preInstalledSw", "NA"),
            ("capacitystatus", "Used"),
        ),
        "single",
        Decimal("0.0544"),
        ("77PTRYZ5MAUP8HU6",),
    ),
    KeySpec(
        "ebs_gp3_gb_month",
        "base",
        "AmazonEC2",
        (("usagetype", "APN1-EBS:VolumeUsage.gp3"),),
        "single",
        Decimal("0.096"),
        ("C8Y3GJZBQTH8T5JV",),
    ),
    KeySpec(
        "vpce_interface_az_hour",
        "base",
        "AmazonVPC",
        (("usagetype", "APN1-VpcEndpoint-Hours"),),
        "single",
        Decimal("0.014"),
        ("W5KUR855UCRX4QZ8",),
    ),
    KeySpec(
        "secrets_manager_secret_month",
        "base",
        "AWSSecretsManager",
        (("usagetype", "APN1-AWSSecretsManager-Secrets"),),
        "single",
        Decimal("0.40"),
        ("GVA2ZKT9KD5RYX6P",),
    ),
    KeySpec(
        "transform_agent_minute",
        "atx",
        "AWSTransform",
        (("usagetype", "APN1-AgentMinute"),),
        "single",
        Decimal("0.035"),
    ),
    KeySpec(
        "lambda_request",
        "stage3",
        "AWSLambda",
        (("usagetype", "APN1-Request"),),
        "single",
        Decimal("0.0000002"),
    ),
    KeySpec(
        "lambda_gb_second",
        "stage3",
        "AWSLambda",
        (("usagetype", "APN1-Lambda-GB-Second"),),
        "first_tier",
        Decimal("0.0000166667"),
    ),
    KeySpec(
        "cwl_ingest_gb",
        "stage3",
        "AmazonCloudWatch",
        (("usagetype", "APN1-DataProcessing-Bytes"),),
        "first_tier",
        Decimal("0.76"),
    ),
    # S3 Access Points request prices for FSx for ONTAP are listed under service code AmazonS3,
    # not AmazonFSx. The OpenZFS usagetypes (APN1-Requests-FSXOPENZFS-...) are a different product.
    KeySpec(
        "fsx_s3ap_put_list_request",
        "stage3",
        "AmazonS3",
        (("usagetype", "APN1-Requests-FSXONTAP-Tier1"),),
        "single",
        Decimal("0.00000108"),
        ("ZE45RAAH9EX5HNT4",),
    ),
    KeySpec(
        "fsx_s3ap_get_request",
        "stage3",
        "AmazonS3",
        (("usagetype", "APN1-Requests-FSXONTAP-Tier2"),),
        "single",
        Decimal("0.000000029"),
        ("W647VRU94Q4WFUS7",),
    ),
)
KEYS_BY_NAME = {spec.key: spec for spec in KEYS}


class PriceListError(Exception):
    """The aws call failed or returned a shape this script does not understand (exit 10)."""


class ExpiredTokenError(Exception):
    """The caller's AWS session has expired (exit 20)."""


@dataclass(frozen=True)
class Candidate:
    """One on-demand price dimension of one product."""

    sku: str
    effective_date: str
    unit: str
    description: str
    begin_range: str
    end_range: str
    usd: Decimal
    attributes: dict[str, str] = field(default_factory=dict, compare=False, hash=False)


class SelectionError(Exception):
    """No unique price for a key (exit 3). The message lists every candidate."""


Runner = Callable[[list[str]], "subprocess.CompletedProcess[str]"]


def run_aws(argv: list[str]) -> subprocess.CompletedProcess[str]:
    """Run an aws CLI argv list (never a shell string)."""
    return subprocess.run(
        argv,
        capture_output=True,
        text=True,
        check=False,
        timeout=AWS_TIMEOUT_SECONDS,
    )


def build_argv(spec: KeySpec, region_code: str) -> list[str]:
    """The one aws call this script makes. Only `pricing get-products` is ever built."""
    argv = [
        "aws",
        "pricing",
        "get-products",
        "--region",
        PRICE_LIST_REGION,
        "--service-code",
        spec.service_code,
        "--filters",
    ]
    for name, value in (*spec.filters, ("regionCode", region_code)):
        argv.append(f"Type=TERM_MATCH,Field={name},Value={value}")
    argv += ["--output", "json"]
    return argv


def fetch_price_list(spec: KeySpec, region_code: str, runner: Runner) -> list[dict]:
    argv = build_argv(spec, region_code)
    if argv[:3] != ["aws", "pricing", "get-products"]:
        raise PriceListError("refusing to run anything but `aws pricing get-products`")
    try:
        proc = runner(argv)
    except FileNotFoundError as exc:
        raise PriceListError("the aws CLI was not found on PATH") from exc
    except subprocess.TimeoutExpired as exc:
        raise PriceListError(
            f"{spec.key}: aws call timed out after {AWS_TIMEOUT_SECONDS}s"
        ) from exc
    stderr = proc.stderr or ""
    if proc.returncode != 0:
        if any(marker in stderr for marker in EXPIRED_MARKERS):
            raise ExpiredTokenError(EXPIRED_MESSAGE)
        raise PriceListError(
            f"{spec.key}: aws exited {proc.returncode}: {stderr.strip()[:300]}"
        )
    try:
        body = json.loads(proc.stdout)
        items = body["PriceList"]
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise PriceListError(
            f"{spec.key}: unexpected get-products output ({exc.__class__.__name__})"
        ) from exc
    products: list[dict] = []
    for item in items:
        try:
            products.append(json.loads(item) if isinstance(item, str) else item)
        except json.JSONDecodeError as exc:
            raise PriceListError(f"{spec.key}: PriceList entry is not JSON") from exc
    return products


def parse_candidates(products: list[dict]) -> list[Candidate]:
    """Flatten products to their on-demand price dimensions."""
    found: list[Candidate] = []
    try:
        for product in products:
            sku = str(product["product"]["sku"])
            attributes = {
                str(k): str(v)
                for k, v in product["product"].get("attributes", {}).items()
            }
            for term in product.get("terms", {}).get("OnDemand", {}).values():
                for dim in term["priceDimensions"].values():
                    found.append(
                        Candidate(
                            sku=sku,
                            effective_date=str(term.get("effectiveDate", ""))[:10],
                            unit=str(dim.get("unit", "")),
                            description=str(dim.get("description", "")),
                            begin_range=str(dim.get("beginRange", "")),
                            end_range=str(dim.get("endRange", "")),
                            usd=Decimal(str(dim["pricePerUnit"]["USD"])),
                            attributes=attributes,
                        )
                    )
    except (KeyError, TypeError, AttributeError, InvalidOperation) as exc:
        raise PriceListError(
            f"unexpected Price List entry shape ({exc.__class__.__name__}: {exc})"
        ) from exc
    return found


def _is_zero(text: str) -> bool:
    try:
        return Decimal(text) == 0
    except InvalidOperation:
        return False


def format_candidates(candidates: list[Candidate]) -> str:
    if not candidates:
        return "  (no candidates)"
    lines = []
    for c in candidates:
        attrs = ", ".join(
            f"{name}={c.attributes[name]}"
            for name in ("usagetype", "operation", "instanceType", "licenseModel")
            if name in c.attributes
        )
        lines.append(
            f"  SKU {c.sku}  {c.usd} USD/{c.unit}  range {c.begin_range or '-'}.."
            f"{c.end_range or '-'}  effective {c.effective_date or '-'}\n"
            f"    {attrs}\n"
            f"    {c.description[:DESCRIPTION_LIMIT]}"
        )
    return "\n".join(lines)


def select_candidate(spec: KeySpec, candidates: list[Candidate]) -> Candidate:
    """Return the single candidate for spec, or raise SelectionError listing all of them."""
    pool = candidates
    if spec.tier == "first_tier":
        pool = [c for c in candidates if _is_zero(c.begin_range)]
    if len(pool) != 1:
        raise SelectionError(
            f"{spec.key}: expected exactly one price after the filters"
            f"{' and the first-tier rule' if spec.tier == 'first_tier' else ''}, "
            f"found {len(pool)} (service {spec.service_code}). Candidates:\n"
            f"{format_candidates(candidates)}"
        )
    return pool[0]


def check_reference(spec: KeySpec, value: Decimal) -> tuple[str, str]:
    """Return ("ok"|"changed"|"out_of_range", message) against the recorded reference."""
    ref = spec.reference
    if value <= 0 or value > ref * DRIFT_FACTOR or value < ref / DRIFT_FACTOR:
        return (
            "out_of_range",
            (
                f"{spec.key}: live value {value} is more than {DRIFT_FACTOR}x away from the "
                f"reference {ref} (likely a unit mismatch)"
            ),
        )
    if value != ref:
        return "changed", f"{spec.key}: changed from reference {ref} to {value}"
    return "ok", ""


def source_text(
    spec: KeySpec, chosen: Candidate, region_code: str, read_date: str
) -> str:
    usagetypes = [v for n, v in spec.filters if n == "usagetype"]
    if usagetypes:
        what = f"usagetype {usagetypes[0]}"
        extra = [f"{n}={v}" for n, v in spec.filters if n != "usagetype"]
    else:
        what = "attributes"
        extra = [f"{n}={v}" for n, v in spec.filters]
    parts = [
        "AWS Price List API",
        f"service code {spec.service_code}",
        what,
    ]
    if extra:
        parts.append(", ".join(extra))
    parts += [
        f"regionCode {region_code}",
        f"SKU {chosen.sku}",
        f"effectiveDate {chosen.effective_date}",
        f"unit {chosen.unit}",
        f"description {chosen.description[:DESCRIPTION_LIMIT]!r}",
        f"read {read_date}",
    ]
    return ", ".join(parts)


def build_fixture(
    specs: list[KeySpec],
    region_code: str,
    read_date: str,
    runner: Runner,
    allow_drift: bool,
    log: Callable[[str], None],
) -> tuple[int, dict | None]:
    """Fetch every spec. Return (exit code, fixture). The fixture is None unless exit is 0."""
    prices: dict[str, float] = {}
    sources: dict[str, str] = {}
    changed: dict[str, dict[str, float]] = {}
    warnings: list[str] = []
    selection_failed = False
    out_of_range = False
    for spec in specs:
        products = fetch_price_list(spec, region_code, runner)
        candidates = parse_candidates(products)
        try:
            chosen = select_candidate(spec, candidates)
        except SelectionError as exc:
            log(f"error: {exc}")
            selection_failed = True
            continue
        if spec.expected_skus and chosen.sku not in spec.expected_skus:
            message = (
                f"{spec.key}: live SKU {chosen.sku} differs from the previously read "
                f"{'/'.join(spec.expected_skus)}"
            )
            log(f"warning: {message}")
            warnings.append(message)
        status, message = check_reference(spec, chosen.usd)
        if status == "out_of_range":
            if allow_drift:
                log(f"warning: {message}")
                warnings.append(message)
            else:
                log(f"error: {message}")
                out_of_range = True
                continue
        elif status == "changed":
            log(message)
            changed[spec.key] = {
                "reference": float(spec.reference),
                "read": float(chosen.usd),
            }
        prices[spec.key] = float(chosen.usd)
        sources[spec.key] = source_text(spec, chosen, region_code, read_date)
        log(f"{spec.key}: {chosen.usd} USD/{chosen.unit} (SKU {chosen.sku})")
    if selection_failed:
        return EXIT_NO_UNIQUE_PRICE, None
    if out_of_range:
        return EXIT_OUT_OF_RANGE, None
    fixture: dict = dict(prices)
    fixture["_read_date"] = read_date
    fixture["_region"] = region_code
    fixture["_price_list_region"] = PRICE_LIST_REGION
    fixture["_generated_by"] = GENERATED_BY
    fixture["_sources"] = sources
    fixture["_changed_from_reference"] = changed
    fixture["_warnings"] = warnings
    return EXIT_OK, fixture


def specs_for(set_name: str) -> list[KeySpec]:
    names = SETS if set_name == "all" else (set_name,)
    return [spec for spec in KEYS if spec.set_name in names]


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".fetch-prices-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="fetch-prices.py",
        description=(
            "Regenerate the unit-price fixture for scripts/estimate.py --price-fixture from the "
            "AWS Price List API (read-only). Prices change: every key records its SKU, effective "
            "date and read date, and a fixture should be fetched again before each approval."
        ),
        epilog=(
            "Exit codes: 0 ok; 2 bad argument or output exists; 3 no unique price for a key; "
            "4 live value more than 10x from the reference; 10 aws call failed; "
            "20 credentials expired (run `aws sso login` yourself)."
        ),
    )
    parser.add_argument(
        "--set",
        dest="set_name",
        choices=(*SETS, "all"),
        help="keys to fetch: base (FSx for ONTAP, AD, EC2, EBS, VPC endpoints, Secrets Manager), "
        "atx, stage3 (Lambda, CloudWatch Logs, S3 Access Points requests), or all",
    )
    parser.add_argument(
        "--region-code",
        default=DEFAULT_REGION_CODE,
        help=f"Price List regionCode filter (default {DEFAULT_REGION_CODE}; the usagetypes "
        "in this script carry the APN1 prefix, so no other value is accepted yet)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        help=f"output file (default {DEFAULT_OUT_DIR}/prices-<UTC date>-<set>.json); an "
        "existing file is not overwritten unless --force is given",
    )
    parser.add_argument(
        "--force", action="store_true", help="overwrite an existing --out file"
    )
    parser.add_argument(
        "--read-date",
        help="date recorded as the read date, YYYY-MM-DD (default today, UTC)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the aws command for every key and exit; no AWS call, no file",
    )
    parser.add_argument(
        "--explain",
        metavar="KEY",
        choices=sorted(KEYS_BY_NAME),
        help="print every candidate price for one key and exit; writes no file",
    )
    parser.add_argument(
        "--allow-drift",
        action="store_true",
        help="record a value more than 10x from the reference as a warning instead of exit 4",
    )
    return parser.parse_args(argv)


def main(
    argv: list[str] | None = None,
    runner: Runner = run_aws,
    today: Callable[[], dt.date] | None = None,
) -> int:
    args = parse_args(argv)

    def log(message: str) -> None:
        print(message, file=sys.stderr)

    if args.explain is None and args.set_name is None:
        log("error: --set is required unless --explain is given")
        return EXIT_BAD_ARGUMENT
    if not REGION_CODE_RE.match(args.region_code):
        log(f"error: --region-code {args.region_code!r} is not a region code")
        return EXIT_BAD_ARGUMENT
    if args.region_code != DEFAULT_REGION_CODE:
        log(
            f"error: the usagetypes in this script are for {DEFAULT_REGION_CODE} (APN1-...); "
            f"{args.region_code} would match nothing"
        )
        return EXIT_BAD_ARGUMENT
    now = (today or (lambda: dt.datetime.now(dt.timezone.utc).date()))()
    read_date = args.read_date or now.isoformat()
    if not DATE_RE.match(read_date):
        log(f"error: --read-date {read_date!r} is not YYYY-MM-DD")
        return EXIT_BAD_ARGUMENT
    try:
        dt.date.fromisoformat(read_date)
    except ValueError:
        log(f"error: --read-date {read_date!r} is not a calendar date")
        return EXIT_BAD_ARGUMENT

    if args.explain is not None:
        spec = KEYS_BY_NAME[args.explain]
        if args.dry_run:
            print(" ".join(build_argv(spec, args.region_code)))
            return EXIT_OK
        try:
            candidates = parse_candidates(
                fetch_price_list(spec, args.region_code, runner)
            )
        except ExpiredTokenError as exc:
            log(str(exc))
            return EXIT_EXPIRED_TOKEN
        except PriceListError as exc:
            log(f"error: {exc}")
            return EXIT_AWS_FAILED
        print(f"{spec.key} ({spec.service_code}, tier rule: {spec.tier})")
        print(format_candidates(candidates))
        return EXIT_OK

    specs = specs_for(args.set_name)
    if args.dry_run:
        for spec in specs:
            print(f"{spec.key}: {' '.join(build_argv(spec, args.region_code))}")
        return EXIT_OK

    out = args.out or DEFAULT_OUT_DIR / f"prices-{now.isoformat()}-{args.set_name}.json"
    if out.exists() and not args.force:
        log(f"error: {out} already exists; choose another --out or pass --force")
        return EXIT_BAD_ARGUMENT

    try:
        code, fixture = build_fixture(
            specs, args.region_code, read_date, runner, args.allow_drift, log
        )
    except ExpiredTokenError as exc:
        log(str(exc))
        return EXIT_EXPIRED_TOKEN
    except PriceListError as exc:
        log(f"error: {exc}")
        return EXIT_AWS_FAILED
    if code != EXIT_OK or fixture is None:
        log("no file written")
        return code
    write_atomic(out, json.dumps(fixture, indent=2, ensure_ascii=False) + "\n")
    print(f"wrote {out} ({len(specs)} keys, read {read_date})")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
