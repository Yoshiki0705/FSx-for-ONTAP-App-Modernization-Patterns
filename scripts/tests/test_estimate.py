#!/usr/bin/env python3
"""Unit tests for scripts/estimate.py (stdlib unittest, no AWS calls).

Covers the design's test-plan rows for estimate.py: the arithmetic from a price fixture, input
validation exit codes, the --elapsed 79% / 81% / no-record cases including "atx after base does not
move the origin" and the latest base approval being the origin when several exist, the --target base minimum recheck (exit 4 on mismatch, exit 0 on match), and
--target atx: --transformation / --limit-minutes required and allow-listed (exit 2 otherwise),
refused on other targets, recorded as parameters, and priced as the ceiling N x unit price, with
an expected $0 inside the AWS/dotnet-modernization quota. --target stage3: add-on line items only,
the S3 Access Points request charge priced at the FSx for ONTAP PUT/LIST rate as an upper bound (GET rate
shown for reference only, a missing S3 request price recorded as "not retrieved"), stage-3
parameters recorded with the template's defaults, and bad stage-3 inputs refused.
"""

from __future__ import annotations

import datetime as dt
import importlib.util
import json
import re
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


class Stage3EstimateTests(unittest.TestCase):
    """--target stage3 prices only the add-on and records the stage-3 deploy parameters."""

    TEMPLATE = SCRIPTS.parent / "templates" / "stage3-serverless.yaml"

    def _run_stage3(
        self, extra: list[str], price_fixture: Path | None = FIXTURES / "prices.json"
    ) -> tuple[int, dict | None]:
        with tempfile.TemporaryDirectory() as d:
            orig = estimate.ESTIMATES_DIR
            estimate.ESTIMATES_DIR = Path(d) / "estimates"
            argv = ["--target", "stage3", "--hours", "4", *extra]
            if price_fixture is not None:
                argv += ["--price-fixture", str(price_fixture)]
            try:
                code = estimate.main(argv)
                written = sorted(estimate.ESTIMATES_DIR.glob("*.json"))
                payload = (
                    json.loads(written[0].read_text(encoding="utf-8"))
                    if written
                    else None
                )
            finally:
                estimate.ESTIMATES_DIR = orig
            return code, payload

    @staticmethod
    def _item(payload: dict, prefix: str) -> dict:
        return next(i for i in payload["line_items"] if i["label"].startswith(prefix))

    def test_line_items_are_the_add_on_only(self) -> None:
        code, payload = self._run_stage3([])
        self.assertEqual(code, 0)
        labels = [i["label"] for i in payload["line_items"]]
        for base_label in (
            "FSx for ONTAP SSD",
            "FSx for ONTAP throughput",
            "AWS Managed Microsoft AD",
            "Windows EC2",
            "Linux EC2",
        ):
            self.assertFalse(
                any(label.startswith(base_label) for label in labels),
                f"stage3 must not re-price the base environment ({base_label})",
            )
        for add_on in (
            "AWS Lambda requests",
            "AWS Lambda duration",
            "FSx for ONTAP S3 Access Points requests",
            "Amazon CloudWatch Logs ingestion",
            "Amazon EventBridge Scheduler",
        ):
            self.assertTrue(any(label.startswith(add_on) for label in labels), add_on)

    def test_lambda_subtotals_compute_from_the_fixture(self) -> None:
        code, payload = self._run_stage3(["--invocations", "20"])
        self.assertEqual(code, 0)
        requests = self._item(payload, "AWS Lambda requests")
        self.assertEqual(requests["quantity"], 20)
        self.assertAlmostEqual(requests["subtotal_usd"], 20 * 0.0000002, places=9)
        duration = self._item(payload, "AWS Lambda duration")
        # 20 invocations x 0.5 GB (512 MiB) x 120 s = 1,200 GB-seconds.
        self.assertAlmostEqual(duration["quantity"], 1200.0)
        self.assertAlmostEqual(duration["subtotal_usd"], 1200 * 0.0000166667, places=6)
        scheduler = self._item(payload, "Amazon EventBridge Scheduler")
        self.assertEqual(scheduler["subtotal_usd"], 0.0)
        self.assertIn("DISABLED", scheduler["label"])

    def test_s3_access_point_requests_are_priced_at_the_put_list_rate_as_an_upper_bound(
        self,
    ) -> None:
        code, payload = self._run_stage3(["--invocations", "20"])
        self.assertEqual(code, 0)
        item = self._item(payload, "FSx for ONTAP S3 Access Points requests (ceiling")
        self.assertEqual(
            item["quantity"], 20 * estimate.STAGE3_S3AP_REQUESTS_PER_INVOCATION
        )
        self.assertEqual(item["quantity"], 2000)
        self.assertEqual(item["unit_usd"], 0.00000108)
        self.assertAlmostEqual(item["subtotal_usd"], 2000 * 0.00000108, places=9)
        self.assertIn("upper bound", item["note"])
        self.assertIn("not confirmed billing", item["note"])
        self.assertNotIn("not retrieved", item["note"])
        # The basis: two routes, the AmazonS3 Price List SKUs and the English pricing page tab.
        for text in (
            "service code AmazonS3",
            "APN1-Requests-FSXONTAP-Tier1/Tier2",
            "ZE45RAAH9EX5HNT4",
            "W647VRU94Q4WFUS7",
            "read 2026-10-12",
            "S3 Access Points for FSx",
            "https://aws.amazon.com/fsx/netapp-ontap/pricing/",
            "ap-northeast-1",
            "$0.00108",
            "$0.000029",
            "not visible to a plain HTTP fetch",
        ):
            self.assertIn(text, item["source"])
        # A regression to the earlier "not checked" wording or to the wrong service code fails here.
        for text in (
            "not checked",
            "not verified",
            "AmazonFSx",
            "SSO",
            "FSXOPENZFS",
        ):
            self.assertNotIn(text, item["source"])
        # The Amazon S3 list-price SKUs are not the unit-price source.
        for text in ("XAKHUSC46NWC9PPM", "9C7873KU3ZQBD6BJ", "0.0047", "0.0037"):
            self.assertNotIn(text, item["source"])
        # The note does not claim the invoice will match.
        self.assertNotIn("not retrieved", item["note"])
        self.assertIn("not confirmed billing", item["note"])

    def test_source_constant_and_fixtures_name_the_amazons3_skus(self) -> None:
        self.assertIn("service code AmazonS3", estimate.FSX_S3AP_REQUEST_SOURCE)
        self.assertNotIn("AmazonFSx", estimate.FSX_S3AP_REQUEST_SOURCE)
        raw = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))
        for key in ("fsx_s3ap_put_list_request", "fsx_s3ap_get_request"):
            recorded = raw["_sources"][key]
            for text in (
                "service code AmazonS3",
                "APN1-Requests-FSXONTAP-Tier1/Tier2",
                "ZE45RAAH9EX5HNT4/W647VRU94Q4WFUS7",
                "read 2026-10-12",
                "S3 Access Points for FSx",
            ):
                self.assertIn(text, recorded, key)
            self.assertNotIn("AmazonFSx", recorded)
            self.assertNotIn("not checked", recorded)
        # Source metadata is not a price: the loader keeps it out of the price map.
        prices = estimate.load_prices(FIXTURES / "prices.json")
        self.assertNotIn("_sources", prices)
        self.assertEqual(prices["fsx_s3ap_put_list_request"], 0.00000108)
        self.assertEqual(prices["fsx_s3ap_get_request"], 0.000000029)

    def test_get_rate_is_a_reference_line_outside_the_total(self) -> None:
        code, payload = self._run_stage3(["--invocations", "20"])
        self.assertEqual(code, 0)
        ref = self._item(payload, "FSx for ONTAP S3 Access Points requests (GET")
        self.assertEqual(ref["unit_usd"], 0.000000029)
        self.assertIsNone(ref["subtotal_usd"])
        self.assertAlmostEqual(ref["reference_usd"], 2000 * 0.000000029, places=9)
        self.assertLess(ref["unit_usd"], 0.00000108)
        self.assertIn("reference only", ref["note"])

    def test_stage3_total_includes_the_request_line(self) -> None:
        code, payload = self._run_stage3(["--invocations", "20"])
        self.assertEqual(code, 0)
        expected = (
            20 * 0.0000002
            + 1200 * 0.0000166667
            + 2000 * 0.00000108
            + round(20 / 1024, 6) * 0.76
        )
        self.assertAlmostEqual(payload["total_usd"], expected, places=2)
        # The request line is a real contribution: dropping it changes the rounded total.
        without = expected - 2000 * 0.00000108
        self.assertNotEqual(round(without, 2), round(expected, 2))
        self.assertNotIn(
            "not retrieved",
            json.dumps(
                self._item(payload, "FSx for ONTAP S3 Access Points requests (ceiling")
            ),
        )

    def test_data_transfer_stays_outside_the_total_and_says_why(self) -> None:
        code, payload = self._run_stage3([])
        self.assertEqual(code, 0)
        item = self._item(payload, "S3 data transfer")
        self.assertIsNone(item["subtotal_usd"])
        self.assertIn("not included", item["note"])
        self.assertIn("billed as for an S3 bucket", item["note"])

    def test_missing_s3_request_price_is_recorded_not_guessed(self) -> None:
        for missing, label, other in (
            (
                "fsx_s3ap_put_list_request",
                "FSx for ONTAP S3 Access Points requests (ceiling",
                "fsx_s3ap_get_request",
            ),
            (
                "fsx_s3ap_get_request",
                "FSx for ONTAP S3 Access Points requests (GET",
                "fsx_s3ap_put_list_request",
            ),
        ):
            with self.subTest(missing=missing), tempfile.TemporaryDirectory() as d:
                fixture = Path(d) / "prices.json"
                prices = json.loads(
                    (FIXTURES / "prices.json").read_text(encoding="utf-8")
                )
                del prices[missing]
                fixture.write_text(json.dumps(prices), encoding="utf-8")
                code, payload = self._run_stage3([], price_fixture=fixture)
                self.assertEqual(code, 0)
                item = self._item(payload, label)
                self.assertIsNone(item["unit_usd"])
                self.assertIsNone(item["subtotal_usd"])
                self.assertEqual(item["note"], "not retrieved")
                # The other S3 request price is untouched: no default is borrowed from it.
                present = next(
                    i
                    for i in payload["line_items"]
                    if i["label"].startswith("FSx for ONTAP S3 Access Points requests")
                    and i is not item
                )
                self.assertEqual(present["unit_usd"], prices[other])

    def test_retired_request_key_is_not_read(self) -> None:
        # fsx_s3ap_request is retired: a stale fixture key neither prices a line nor is an error.
        with tempfile.TemporaryDirectory() as d:
            fixture = Path(d) / "prices.json"
            prices = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))
            prices["fsx_s3ap_request"] = 0.5
            fixture.write_text(json.dumps(prices), encoding="utf-8")
            code, payload = self._run_stage3(
                ["--invocations", "10"], price_fixture=fixture
            )
        self.assertEqual(code, 0)
        item = self._item(payload, "FSx for ONTAP S3 Access Points requests (ceiling")
        self.assertEqual(item["unit_usd"], 0.00000108)

    def test_missing_lambda_price_is_recorded_not_guessed(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            fixture = Path(d) / "prices.json"
            fixture.write_text(
                json.dumps({"lambda_request": 0.0000002}), encoding="utf-8"
            )
            code, payload = self._run_stage3([], price_fixture=fixture)
        self.assertEqual(code, 0)
        duration = self._item(payload, "AWS Lambda duration")
        self.assertIsNone(duration["subtotal_usd"])
        self.assertEqual(duration["note"], "not retrieved")

    def test_parameters_are_recorded_with_template_defaults(self) -> None:
        code, payload = self._run_stage3([])
        self.assertEqual(code, 0)
        pairs = {p["ParameterKey"]: p["ParameterValue"] for p in payload["parameters"]}
        self.assertEqual(
            pairs,
            {
                "BaseStackName": "appmod-base",
                "LambdaPackageKey": "lambda/worker.zip",
                "LambdaHandler": "DocIntake.Lambda::DocIntake.Lambda.Function::Handler",
                "LambdaRuntime": "dotnet10",
                "FileSystemIdentityUser": "APPMOD\\appsvc",
            },
        )
        for removed in ("VpcId", "PrimarySubnetId", "LambdaSecurityGroupId"):
            self.assertNotIn(removed, pairs)

    def test_overrides_are_recorded(self) -> None:
        code, payload = self._run_stage3(
            [
                "--lambda-runtime",
                "provided.al2023",
                "--artifacts-bucket-name",
                "appmod-example-artifacts",
            ]
        )
        self.assertEqual(code, 0)
        pairs = {p["ParameterKey"]: p["ParameterValue"] for p in payload["parameters"]}
        self.assertEqual(pairs["LambdaRuntime"], "provided.al2023")
        self.assertEqual(pairs["ArtifactsBucketName"], "appmod-example-artifacts")

    def test_bad_stage3_inputs_exit_2_without_writing(self) -> None:
        for extra in (
            ["--lambda-runtime", "dotnet8"],
            ["--lambda-handler", "a,b"],
            ["--lambda-package-key", "../worker.zip"],
            ["--lambda-package-key", "lambda/worker.tar"],
            ["--base-stack-name", "1base"],
            ["--file-system-identity-user", "APPMOD\\app,svc"],
            ["--artifacts-bucket-name", "Bad_Bucket"],
            ["--invocations", "0"],
            ["--invocations", "10001"],
            ["--invocations", "abc"],
        ):
            with self.subTest(extra=extra):
                code, payload = self._run_stage3(extra)
                self.assertEqual(code, 2)
                self.assertIsNone(payload)

    def test_stage3_flags_on_another_target_are_refused(self) -> None:
        with tempfile.TemporaryDirectory() as d:
            orig = estimate.ESTIMATES_DIR
            estimate.ESTIMATES_DIR = Path(d) / "estimates"
            try:
                code = estimate.main(
                    [
                        "--target",
                        "teardown",
                        "--invocations",
                        "5",
                        "--price-fixture",
                        str(FIXTURES / "prices.json"),
                    ]
                )
                self.assertEqual(code, 2)
                self.assertEqual(sorted(estimate.ESTIMATES_DIR.glob("*.json")), [])
            finally:
                estimate.ESTIMATES_DIR = orig

    def test_defaults_match_the_template(self) -> None:
        # Narrow stdlib scan of the template, like the deploy.sh secret-defaults parity check.
        text = self.TEMPLATE.read_text(encoding="utf-8")

        def default_of(key: str) -> str | None:
            m = re.search(rf"(?m)^  {re.escape(key)}:\s*$", text)
            if not m:
                return None
            d = re.search(r"(?m)^    Default:\s*(\S+)\s*$", text[m.end() :])
            return d.group(1) if d else None

        for key, value in estimate.STAGE3_DEFAULTS.items():
            self.assertEqual(default_of(key), value, key)
        allowed = re.search(
            r"(?m)^    AllowedValues:\s*\[([^\]]*)\]",
            text[text.index("  LambdaRuntime:") :],
        )
        self.assertIsNotNone(allowed)
        self.assertEqual(
            tuple(v.strip() for v in allowed.group(1).split(",")),
            estimate.STAGE3_RUNTIMES,
        )
        self.assertRegex(text, rf"(?m)^      MemorySize: {estimate.STAGE3_MEMORY_MB}$")
        self.assertRegex(text, rf"(?m)^      Timeout: {estimate.STAGE3_TIMEOUT_S}$")


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

    def test_latest_base_approval_is_the_origin_regardless_of_list_order(self) -> None:
        # Three base records, not in chronological order. The latest approved_at is the middle
        # one (10h ago, 100h) -> 10%. The first record (500/24) or the last (300/24) would be far off.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "base", "approved_at": self._ago(500), "hours": 24},
                    {"target": "base", "approved_at": self._ago(10), "hours": 100},
                    {"target": "base", "approved_at": self._ago(300), "hours": 24},
                ],
                tmp,
            )
            self.assertAlmostEqual(estimate.elapsed_fraction(path), 0.10, delta=0.01)
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 0
            )

    def test_stale_first_base_does_not_trigger_false_alarm(self) -> None:
        # A rebuilt environment adds a newer base record. The stale first record (120/24 = 500%)
        # must not decide the result; the latest one is 24/72 = 33% -> exit 0.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "base", "approved_at": self._ago(120), "hours": 24},
                    {"target": "base", "approved_at": self._ago(24), "hours": 72},
                ],
                tmp,
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 0
            )

    def test_single_base_approval_still_works(self) -> None:
        # One base record, 10h ago over 100h -> 10%.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [{"target": "base", "approved_at": self._ago(10), "hours": 100}], tmp
            )
            self.assertAlmostEqual(estimate.elapsed_fraction(path), 0.10, delta=0.01)
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 0
            )

    def test_no_base_with_other_approvals_still_exits_2(self) -> None:
        # atx / stage3 / extend records alone are not a base approval -> InputError, exit 2.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "atx", "approved_at": self._ago(3)},
                    {"target": "stage3", "approved_at": self._ago(2)},
                    {"target": "extend", "approved_at": self._ago(1), "hours": 50},
                ],
                tmp,
            )
            with self.assertRaises(estimate.InputError):
                estimate.elapsed_fraction(path)
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 2
            )

    def test_80_percent_boundary_with_multiple_bases(self) -> None:
        # The comparison is fraction > 0.80. 81% exits 3 and 79% exits 0 with a stale base first.
        # Exactly 80% is not tested: _utc_now() moves between the write and the call.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "base", "approved_at": self._ago(900), "hours": 24},
                    {"target": "base", "approved_at": self._ago(81), "hours": 100},
                ],
                tmp,
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 3
            )
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "base", "approved_at": self._ago(900), "hours": 24},
                    {"target": "base", "approved_at": self._ago(79), "hours": 100},
                ],
                tmp,
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 0
            )

    def test_malformed_approved_at_on_a_base_record_exits_2(self) -> None:
        # Fails closed: an unparseable approved_at on any base record is an input error,
        # even when a valid, newer base record exists.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "base", "approved_at": "not-a-date", "hours": 24},
                    {"target": "base", "approved_at": self._ago(10), "hours": 100},
                ],
                tmp,
            )
            self.assertEqual(
                estimate.main(["--elapsed", "--approval-file", str(path)]), 2
            )

    def test_extend_hours_still_add_with_latest_base(self) -> None:
        # Latest base 60h ago over 100h, extend +50h -> 60/150 = 40%. The stale base is ignored.
        with tempfile.TemporaryDirectory() as d:
            tmp = Path(d)
            path = self._write_approval(
                [
                    {"target": "base", "approved_at": self._ago(500), "hours": 24},
                    {"target": "base", "approved_at": self._ago(60), "hours": 100},
                    {"target": "extend", "approved_at": self._ago(5), "hours": 50},
                ],
                tmp,
            )
            self.assertAlmostEqual(estimate.elapsed_fraction(path), 0.40, delta=0.01)


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
