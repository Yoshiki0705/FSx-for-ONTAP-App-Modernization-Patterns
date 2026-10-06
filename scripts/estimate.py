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
  --elapsed      Compute the elapsed fraction of the approved base time from approval.json and
                 return exit 3 when it exceeds 80%. The origin is always the base approval time; atx
                 / stage3 / teardown / base-update approvals do not move it, extend hours add to the
                 denominator.

Exit codes: 0 ok; 1 Price List API unreachable (no estimate written); 2 invalid input; 3 elapsed
fraction over 80%; 4 (--target base only) the fixed minimum no longer matches the current minimum.

Prices and minimums reach this script through the AWS Price List API in production. In tests they
are supplied as JSON fixtures via --price-fixture and --minimums-fixture, so no test makes a live
call. When neither a fixture nor a reachable API is available, --target returns exit 1 rather than
inventing numbers.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
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


def load_prices(fixture: Path | None) -> dict[str, float] | None:
    """Return a mapping of price keys to unit prices, or None if unavailable.

    In production this would call the AWS Price List API (the pricing endpoint is us-east-1). Here it
    is read from a fixture in tests. Returning None signals 'API unreachable' and causes exit 1; it
    never falls back to remembered values.
    """
    if fixture is not None:
        data = json.loads(fixture.read_text(encoding="utf-8"))
        return {str(k): float(v) if v is not None else None for k, v in data.items()}
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


def build_line_items(
    prices: dict[str, float], target: str, hours: int, egress_mode: str | None
) -> list[dict[str, object]]:
    """Return estimate line items. A missing price is recorded as not-retrieved, never guessed."""
    items: list[dict[str, object]] = []

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

    if target in ("base", "stage3", "extend"):
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
    subtotals = [i["subtotal_usd"] for i in items if i["subtotal_usd"] is not None]
    payload["total_usd"] = round(sum(subtotals), 2) if subtotals else None
    json_path = ESTIMATES_DIR / f"{stamp}-{target}.json"
    json_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return json_path


def elapsed_fraction(approval_file: Path) -> float:
    """Return elapsed-time fraction against the base approval. Raises InputError (exit 2).

    The origin is the base approval time. extend hours add to the denominator. atx / stage3 /
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

    base = next((a for a in approvals if a.get("target") == "base"), None)
    if base is None:
        raise InputError("approval.json has no 'base' approval")
    try:
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

    items = build_line_items(prices, args.target, hours, args.egress_mode)
    path = write_estimate(args.target, hours, args.egress_mode, items, minimum_mismatch)
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
