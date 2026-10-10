#!/usr/bin/env python3
"""Unit tests for scripts/estimate.py (stdlib unittest, no AWS calls).

Covers the design's test-plan rows for estimate.py: the arithmetic from a price fixture, input
validation exit codes, the --elapsed 79% / 81% / no-record cases including "atx after base does not
move the origin", the --target base minimum recheck (exit 4 on mismatch, exit 0 on match), and
--target atx: --transformation / --limit-minutes required and allow-listed (exit 2 otherwise),
refused on other targets, recorded as parameters, and priced as the ceiling N x unit price, with
an expected $0 inside the AWS/dotnet-modernization quota.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"

spec = importlib.util.spec_from_file_location("estimate", SCRIPTS / "estimate.py")
estimate = importlib.util.module_from_spec(spec)
sys.modules["estimate"] = estimate
spec.loader.exec_module(estimate)


class ArithmeticTests(unittest.TestCase):
    def test_base_line_items_match_draft(self) -> None:
        prices = estimate.load_prices(FIXTURES / "prices.json")
        items = estimate.build_line_items(
            prices, "base", hours=72, egress_mode="endpoints"
        )
        by_label = {i["label"]: i for i in items}

        # FSx SSD + throughput hourly should sum to the draft's $0.3693 / hour.
        ssd = by_label["FSx for ONTAP SSD (1,024 GiB)"]["hourly_usd"]
        thr = by_label["FSx for ONTAP throughput (128 MBps)"]["hourly_usd"]
        self.assertAlmostEqual(ssd + thr, 0.3693, places=4)

        # AD Standard, 2 DC = $0.146 / hour.
        self.assertAlmostEqual(
            by_label["AWS Managed Microsoft AD (Standard, 2 DC)"]["hourly_usd"],
            0.146,
            places=4,
        )
        # Interface endpoints row appears only for endpoints mode: 6 x $0.014 = $0.084.
        self.assertAlmostEqual(
            by_label["Interface VPC endpoints (6 services x 1 AZ)"]["hourly_usd"],
            0.084,
            places=4,
        )

    def test_nat_mode_has_no_endpoint_line(self) -> None:
        prices = estimate.load_prices(FIXTURES / "prices.json")
        items = estimate.build_line_items(prices, "base", hours=72, egress_mode="nat")
        self.assertFalse(
            any("Interface VPC endpoints" in i["label"] for i in items),
            "nat mode must not carry an interface-endpoint line",
        )

    def test_missing_price_is_recorded_not_guessed(self) -> None:
        prices = {"fsx_ssd_gb_month": 0.150}  # throughput and others absent
        items = estimate.build_line_items(prices, "base", hours=10, egress_mode="nat")
        throughput = next(i for i in items if "throughput" in i["label"])
        self.assertIsNone(throughput["hourly_usd"])
        self.assertEqual(throughput["note"], "not retrieved")


class BaseParameterTests(unittest.TestCase):
    """The base estimate records the exact CloudFormation parameters deploy.sh will pass."""

    def _run_base(self, extra: list[str]) -> tuple[int, dict | None]:
        """Run --target base into a temp estimates dir; return (exit_code, parameters-or-None)."""
        with tempfile.TemporaryDirectory() as d:
            orig = estimate.ESTIMATES_DIR
            estimate.ESTIMATES_DIR = Path(d) / "estimates"
            try:
                code = estimate.main(
                    [
                        "--target",
                        "base",
                        "--hours",
                        "72",
                        "--egress-mode",
                        "endpoints",
                        "--price-fixture",
                        str(FIXTURES / "prices.json"),
                        "--minimums-fixture",
                        str(FIXTURES / "minimums_match.json"),
                        *extra,
                    ]
                )
                written = (
                    sorted(estimate.ESTIMATES_DIR.glob("*.json"))
                    if code in (0, 4)
                    else []
                )
                payload = (
                    json.loads(written[0].read_text(encoding="utf-8"))
                    if written
                    else None
                )
            finally:
                estimate.ESTIMATES_DIR = orig
            return code, payload

    def test_all_new_records_five_switches_and_three_cidrs(self) -> None:
        # Default all-new config: the 5 Create<X>=true plus the 3 approved CIDRs, as
        # ParameterKey/ParameterValue pairs.
        code, payload = self._run_base([])
        self.assertEqual(code, 0)
        self.assertIsNotNone(payload)
        pairs = {p["ParameterKey"]: p["ParameterValue"] for p in payload["parameters"]}
        for key in (
            "CreateVpc",
            "CreateSubnets",
            "CreateInterfaceEndpoints",
            "CreateS3GatewayEndpoint",
            "CreateDirectory",
        ):
            self.assertEqual(
                pairs[key], "true", f"{key} should be true in the all-new config"
            )
        self.assertEqual(pairs["VpcCidr"], "10.90.0.0/16")
        self.assertEqual(pairs["PrimarySubnetCidr"], "10.90.0.0/24")
        self.assertEqual(pairs["SecondAzSubnetCidr"], "10.90.1.0/24")

    def test_subnet_cidr_outside_vpc_cidr_exits_2(self) -> None:
        # A /16 "subnet" is not inside a /24 VPC, so containment fails. Both values stay within the
        # approved dedicated-VPC block so no new address literal is introduced.
        code, _ = self._run_base(
            [
                "--vpc-cidr",
                "10.90.0.0/24",
                "--primary-subnet-cidr",
                "10.90.0.0/16",  # wider than the VPC: not contained
            ]
        )
        self.assertEqual(code, 2)

    def test_create_vpc_false_without_existing_id_exits_2(self) -> None:
        code, _ = self._run_base(
            [
                "--create-vpc",
                "false",
                "--create-subnets",
                "false",
                # ExistingVpcId deliberately omitted.
                "--existing-primary-subnet-id",
                "subnet-0123456789abcdef0",
                "--existing-second-az-subnet-id",
                "subnet-0123456789abcdef0",
            ]
        )
        self.assertEqual(code, 2)

    def test_create_directory_false_without_existing_id_exits_2(self) -> None:
        code, _ = self._run_base(
            [
                "--create-directory",
                "false",
                # ExistingDirectoryId deliberately omitted.
                "--existing-directory-dns-ips",
                "192.0.2.10,192.0.2.11",
            ]
        )
        self.assertEqual(code, 2)

    def test_reuse_config_records_existing_ids(self) -> None:
        # A reuse config records the Existing* IDs instead of the CIDRs; valid IDs -> exit 0.
        code, payload = self._run_base(
            [
                "--create-directory",
                "false",
                "--existing-directory-id",
                "d-0123456789",
                "--existing-directory-dns-ips",
                "192.0.2.10,192.0.2.11",
            ]
        )
        self.assertEqual(code, 0)
        pairs = {p["ParameterKey"]: p["ParameterValue"] for p in payload["parameters"]}
        self.assertEqual(pairs["CreateDirectory"], "false")
        self.assertEqual(pairs["ExistingDirectoryId"], "d-0123456789")
        self.assertEqual(pairs["ExistingDirectoryDnsIps"], "192.0.2.10,192.0.2.11")


class AtxEstimateTests(unittest.TestCase):
    """--target atx records the transformation and agent-minute limit, priced as a ceiling."""

    def _run_atx(
        self, extra: list[str], price_fixture: Path | None = FIXTURES / "prices.json"
    ) -> tuple[int, dict | None]:
        with tempfile.TemporaryDirectory() as d:
            orig = estimate.ESTIMATES_DIR
            estimate.ESTIMATES_DIR = Path(d) / "estimates"
            argv = ["--target", "atx", *extra]
            if price_fixture is not None:
                argv += ["--price-fixture", str(price_fixture)]
            try:
                code = estimate.main(argv)
                written = (
                    sorted(estimate.ESTIMATES_DIR.glob("*.json")) if code == 0 else []
                )
                payload = (
                    json.loads(written[0].read_text(encoding="utf-8"))
                    if written
                    else None
                )
            finally:
                estimate.ESTIMATES_DIR = orig
            return code, payload

    ANALYSIS = ("--transformation", "AWS/comprehensive-codebase-analysis")
    DOTNET = ("--transformation", "AWS/dotnet-modernization")

    def test_without_limit_minutes_is_refused(self) -> None:
        code, payload = self._run_atx(self.ANALYSIS)
        self.assertEqual(code, 2)
        self.assertIsNone(payload)

    def test_without_transformation_is_refused(self) -> None:
        self.assertEqual(self._run_atx(["--limit-minutes", "120"])[0], 2)

    def test_unknown_transformation_is_refused(self) -> None:
        code, _ = self._run_atx(
            ["--transformation", "AWS/java-version-upgrade", "--limit-minutes", "120"]
        )
        self.assertEqual(code, 2)

    def test_non_positive_or_non_integer_limit_is_refused(self) -> None:
        for bad in ("0", "-5", "abc", "1.5", "012", ""):
            with self.subTest(limit=bad):
                code, _ = self._run_atx([*self.ANALYSIS, "--limit-minutes", bad])
                self.assertEqual(code, 2)

    def test_atx_flags_on_another_target_are_refused(self) -> None:
        # Route through a temp ESTIMATES_DIR so a validation regression cannot write a teardown
        # estimate into the real .private/estimates relative to the cwd.
        price = ["--price-fixture", str(FIXTURES / "prices.json")]
        with tempfile.TemporaryDirectory() as d:
            orig = estimate.ESTIMATES_DIR
            estimate.ESTIMATES_DIR = Path(d) / "estimates"
            try:
                self.assertEqual(
                    estimate.main(
                        ["--target", "teardown", "--limit-minutes", "120", *price]
                    ),
                    2,
                )
                self.assertEqual(
                    estimate.main(["--target", "teardown", *self.ANALYSIS, *price]), 2
                )
                self.assertEqual(sorted(estimate.ESTIMATES_DIR.glob("*.json")), [])
            finally:
                estimate.ESTIMATES_DIR = orig

    def test_analysis_records_parameters_and_ceiling(self) -> None:
        code, payload = self._run_atx([*self.ANALYSIS, "--limit-minutes", "120"])
        self.assertEqual(code, 0)
        self.assertEqual(
            payload["parameters"],
            [
                {
                    "ParameterKey": "Transformation",
                    "ParameterValue": "AWS/comprehensive-codebase-analysis",
                },
                {"ParameterKey": "LimitMinutes", "ParameterValue": "120"},
            ],
        )
        self.assertAlmostEqual(payload["total_usd"], 4.20, places=2)
        self.assertNotIn("expected", payload)
        self.assertEqual(len(payload["line_items"]), 1)
        self.assertEqual(payload["line_items"][0]["agent_minutes"], 120)

    def test_dotnet_records_ceiling_and_expected_zero_within_quota(self) -> None:
        code, payload = self._run_atx([*self.DOTNET, "--limit-minutes", "300"])
        self.assertEqual(code, 0)
        pairs = {p["ParameterKey"]: p["ParameterValue"] for p in payload["parameters"]}
        self.assertEqual(pairs["Transformation"], "AWS/dotnet-modernization")
        self.assertEqual(pairs["LimitMinutes"], "300")
        self.assertAlmostEqual(payload["total_usd"], 10.50, places=2)
        self.assertEqual(payload["expected"]["usd"], 0.0)
        self.assertIn("not observable", payload["expected"]["caveat"])
        self.assertIn("50,000", payload["expected"]["basis"])
        expected_item = payload["line_items"][1]
        self.assertIsNone(expected_item["subtotal_usd"])
        self.assertIn("not observable", expected_item["note"])

    def test_missing_unit_price_is_recorded_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fixture = Path(d) / "prices.json"
            fixture.write_text(
                json.dumps({"fsx_ssd_gb_month": 0.150}), encoding="utf-8"
            )
            code, payload = self._run_atx(
                [*self.ANALYSIS, "--limit-minutes", "120"], price_fixture=fixture
            )
        self.assertEqual(code, 0)
        item = payload["line_items"][0]
        self.assertIsNone(item["subtotal_usd"])
        self.assertEqual(item["note"], "not retrieved")
        self.assertIsNone(payload["total_usd"])

    def test_no_price_fixture_exits_1(self) -> None:
        code, _ = self._run_atx(
            [*self.ANALYSIS, "--limit-minutes", "120"], price_fixture=None
        )
        self.assertEqual(code, 1)


class ValidationTests(unittest.TestCase):
    def _args(self, **kw):
        import argparse

        base = {
            "target": None,
            "hours": None,
            "egress_mode": None,
            "region": None,
            "elapsed": False,
            "price_fixture": None,
            "minimums_fixture": None,
            "approval_file": None,
        }
        base.update(kw)
        return argparse.Namespace(**base)

    def test_unknown_target_exits_2(self) -> None:
        self.assertEqual(estimate.main(["--target", "bogus"]), 2)

    def test_base_requires_hours_and_egress(self) -> None:
        self.assertEqual(estimate.main(["--target", "base"]), 2)
        self.assertEqual(estimate.main(["--target", "base", "--hours", "72"]), 2)

    def test_hours_out_of_range_exits_2(self) -> None:
        with self.assertRaises(estimate.InputError):
            estimate.validate_target_args(
                self._args(target="base", hours=0, egress_mode="nat")
            )
        with self.assertRaises(estimate.InputError):
            estimate.validate_target_args(
                self._args(target="base", hours=241, egress_mode="nat")
            )

    def test_region_other_than_tokyo_exits_2(self) -> None:
        with self.assertRaises(estimate.InputError):
            estimate.validate_target_args(
                self._args(
                    target="base", hours=72, egress_mode="nat", region="us-east-1"
                )
            )

    def test_elapsed_and_target_are_exclusive(self) -> None:
        self.assertEqual(estimate.main(["--elapsed", "--target", "base"]), 2)

    def test_no_mode_exits_2(self) -> None:
        self.assertEqual(estimate.main([]), 2)

    def test_target_without_price_fixture_exits_1(self) -> None:
        # No live call is made; absence of a fixture is treated as "API unreachable" -> exit 1.
        self.assertEqual(estimate.main(["--target", "teardown"]), 1)


class ElapsedTests(unittest.TestCase):
    def _write_approval(self, approvals: list[dict], tmp: Path) -> Path:
        path = tmp / "approval.json"
        path.write_text(json.dumps(approvals), encoding="utf-8")
        return path

    def _ago(self, hours: float) -> str:
        when = dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)
        return when.isoformat().replace("+00:00", "Z")

    def test_79_percent_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [{"target": "base", "approved_at": self._ago(79), "hours": 100}], tmp
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 0
            )

    def test_81_percent_exits_3(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [{"target": "base", "approved_at": self._ago(81), "hours": 100}], tmp
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 3
            )

    def test_missing_approval_file_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "nope.json"
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(missing)]), 2
            )

    def test_no_base_record_exits_2(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [{"target": "atx", "approved_at": self._ago(1)}], tmp
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 2
            )

    def test_atx_after_base_does_not_move_origin(self) -> None:
        # base approved 79h ago over 100h -> 79%. An atx approval 1h ago must not change the origin.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "base", "approved_at": self._ago(79), "hours": 100},
                    {"target": "atx", "approved_at": self._ago(1)},
                ],
                tmp,
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 0
            )

    def test_extend_hours_add_to_denominator(self) -> None:
        # base 100h approved 110h ago -> 110% alone. extend +50h -> 110/150 = 73% -> exit 0.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "base", "approved_at": self._ago(110), "hours": 100},
                    {"target": "extend", "approved_at": self._ago(5), "hours": 50},
                ],
                tmp,
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 0
            )


class MinimumRecheckTests(unittest.TestCase):
    def test_match_exits_0(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            # Redirect the estimates dir so the test does not write into the repo.
            orig = estimate.ESTIMATES_DIR
            estimate.ESTIMATES_DIR = Path(d) / "estimates"
            try:
                code = estimate.main(
                    [
                        "--target",
                        "base",
                        "--hours",
                        "72",
                        "--egress-mode",
                        "nat",
                        "--price-fixture",
                        str(FIXTURES / "prices.json"),
                        "--minimums-fixture",
                        str(FIXTURES / "minimums_match.json"),
                    ]
                )
            finally:
                estimate.ESTIMATES_DIR = orig
            self.assertEqual(code, 0)

    def test_mismatch_exits_4(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            orig = estimate.ESTIMATES_DIR
            estimate.ESTIMATES_DIR = Path(d) / "estimates"
            try:
                code = estimate.main(
                    [
                        "--target",
                        "base",
                        "--hours",
                        "72",
                        "--egress-mode",
                        "nat",
                        "--price-fixture",
                        str(FIXTURES / "prices.json"),
                        "--minimums-fixture",
                        str(FIXTURES / "minimums_mismatch.json"),
                    ]
                )
            finally:
                estimate.ESTIMATES_DIR = orig
            self.assertEqual(code, 4)


if __name__ == "__main__":
    unittest.main()
