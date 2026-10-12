#!/usr/bin/env python3
"""Unit tests for scripts/fetch-prices.py (stdlib unittest, no AWS calls, no network).

The aws CLI is replaced by a runner that returns canned `get-products` output in the shape the
Price List API uses: PriceList is a list of JSON strings, each with product.sku,
product.attributes and terms.OnDemand.<id>.priceDimensions.<id>.{unit, beginRange, endRange,
description, pricePerUnit.USD}, plus terms.OnDemand.<id>.effectiveDate. Covered: argument
assembly (read-only subcommand only), unique match, zero and several candidates (exit 3 with every
candidate listed), tier selection, the reference range (exit 4, --allow-drift, changed value), the
previously-read-SKU warning, expired credentials (exit 20), other aws failures (exit 10), that the
output is accepted by estimate.load_prices and by estimate.main --target stage3, that the key set
equals the non-underscore keys of tests/fixtures/prices.json, and that --dry-run and --explain
never write a file.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import subprocess
import sys
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def load_module(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


fetch = load_module("fetch_prices", "fetch-prices.py")
estimate = load_module("estimate", "estimate.py")

READ_DATE = "2026-10-12"


def product(
    sku: str,
    usd: str,
    *,
    unit: str = "Quantity",
    description: str = "canned price",
    dims: list[tuple[str, str, str]] | None = None,
    attributes: dict[str, str] | None = None,
    as_object: bool = False,
):
    """One PriceList element. dims is a list of (beginRange, endRange, USD) overriding usd."""
    dims = dims or [("0", "Inf", usd)]
    price_dimensions = {
        f"{sku}.dim{i}": {
            "unit": unit,
            "beginRange": begin,
            "endRange": end,
            "description": description,
            "pricePerUnit": {"USD": price},
        }
        for i, (begin, end, price) in enumerate(dims)
    }
    body = {
        "product": {"sku": sku, "attributes": attributes or {}},
        "terms": {
            "OnDemand": {
                f"{sku}.JRTCKXETXF": {
                    "effectiveDate": "2026-10-01T00:00:00Z",
                    "priceDimensions": price_dimensions,
                }
            }
        },
    }
    return body if as_object else json.dumps(body)


def reference_response(spec) -> list:
    """The canned answer for a spec: one product at the reference value and expected SKU."""
    sku = spec.expected_skus[0] if spec.expected_skus else f"SKU{spec.key[:8].upper()}"
    return [product(sku, format(spec.reference, "f"), unit="Unit")]


class FakeAws:
    """Records every argv and answers from a per-key table of PriceList lists."""

    def __init__(self, overrides: dict[str, list] | None = None) -> None:
        self.calls: list[list[str]] = []
        self.responses = {spec.key: reference_response(spec) for spec in fetch.KEYS} | (
            overrides or {}
        )
        self.failure: subprocess.CompletedProcess | None = None

    def __call__(self, argv: list[str]):
        self.calls.append(list(argv))
        if self.failure is not None:
            return self.failure
        filters = {
            part.split(",")[1][len("Field=") :]: part.split(",", 2)[2][len("Value=") :]
            for part in argv[argv.index("--filters") + 1 : argv.index("--output")]
        }
        service = argv[argv.index("--service-code") + 1]
        for spec in fetch.KEYS:
            if spec.service_code != service:
                continue
            if all(filters.get(n) == v for n, v in spec.filters):
                return subprocess.CompletedProcess(
                    argv,
                    0,
                    json.dumps({"PriceList": self.responses[spec.key]}),
                    "",
                )
        raise AssertionError(f"no canned response for {service} {filters}")


def run_main(argv: list[str], runner) -> tuple[int, str, str]:
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = fetch.main(argv, runner=runner)
    return code, out.getvalue(), err.getvalue()


class ArgvTests(unittest.TestCase):
    def test_argv_is_the_read_only_price_list_call(self) -> None:
        spec = fetch.KEYS_BY_NAME["fsx_ssd_gb_month"]
        argv = fetch.build_argv(spec, "ap-northeast-1")
        self.assertEqual(argv[:3], ["aws", "pricing", "get-products"])
        self.assertEqual(argv[argv.index("--region") + 1], "us-east-1")
        self.assertEqual(argv[argv.index("--service-code") + 1], "AmazonFSx")
        triples = argv[argv.index("--filters") + 1 : argv.index("--output")]
        self.assertEqual(
            triples,
            [
                "Type=TERM_MATCH,Field=usagetype,Value=APN1-Storage.SAZ_2N:SSD",
                "Type=TERM_MATCH,Field=operation,Value=CreateFileSystem:ONTAP",
                "Type=TERM_MATCH,Field=regionCode,Value=ap-northeast-1",
            ],
        )

    def test_every_spec_builds_only_get_products(self) -> None:
        for spec in fetch.KEYS:
            with self.subTest(key=spec.key):
                argv = fetch.build_argv(spec, "ap-northeast-1")
                self.assertEqual(argv[:3], ["aws", "pricing", "get-products"])
                self.assertEqual(argv.count("--region"), 1)
                self.assertNotIn("ap-northeast-1", argv[argv.index("--region") + 1])

    def test_s3_access_points_prices_use_the_amazon_s3_service_code(self) -> None:
        put = fetch.KEYS_BY_NAME["fsx_s3ap_put_list_request"]
        get = fetch.KEYS_BY_NAME["fsx_s3ap_get_request"]
        self.assertEqual((put.service_code, get.service_code), ("AmazonS3", "AmazonS3"))
        self.assertEqual(put.filters, (("usagetype", "APN1-Requests-FSXONTAP-Tier1"),))
        self.assertEqual(get.filters, (("usagetype", "APN1-Requests-FSXONTAP-Tier2"),))

    def test_lambda_and_logs_usagetypes(self) -> None:
        by = fetch.KEYS_BY_NAME
        self.assertEqual(by["lambda_request"].filters, (("usagetype", "APN1-Request"),))
        self.assertEqual(
            by["lambda_gb_second"].filters, (("usagetype", "APN1-Lambda-GB-Second"),)
        )
        self.assertEqual(
            by["cwl_ingest_gb"].filters, (("usagetype", "APN1-DataProcessing-Bytes"),)
        )

    def test_dry_run_never_calls_the_runner_or_writes(self) -> None:
        fake = FakeAws()
        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "p.json"
            code, stdout, _ = run_main(
                ["--set", "all", "--dry-run", "--out", str(out)], fake
            )
            self.assertFalse(out.exists())
        self.assertEqual(code, 0)
        self.assertEqual(fake.calls, [])
        for spec in fetch.KEYS:
            self.assertIn(spec.key, stdout)
        self.assertEqual(stdout.count("aws pricing get-products"), len(fetch.KEYS))


class SelectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "prices.json"

    def run_set(self, set_name: str, fake: FakeAws, *extra: str):
        return run_main(
            [
                "--set",
                set_name,
                "--out",
                str(self.out),
                "--read-date",
                READ_DATE,
                *extra,
            ],
            fake,
        )

    def test_unique_match_is_written_with_sku_and_read_date(self) -> None:
        code, _, _ = self.run_set("atx", FakeAws())
        self.assertEqual(code, 0)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["transform_agent_minute"], 0.035)
        self.assertEqual(data["_read_date"], READ_DATE)
        self.assertEqual(data["_region"], "ap-northeast-1")
        self.assertEqual(data["_price_list_region"], "us-east-1")
        self.assertEqual(data["_generated_by"], "scripts/fetch-prices.py")
        text = data["_sources"]["transform_agent_minute"]
        self.assertIn("AWS Price List API, service code AWSTransform", text)
        self.assertIn("usagetype APN1-AgentMinute", text)
        self.assertIn("SKU SKUTRANSFO", text)
        self.assertIn("effectiveDate 2026-10-01", text)
        self.assertIn("unit Unit", text)
        self.assertTrue(text.endswith(f"read {READ_DATE}"))

    def test_every_source_is_a_string_with_sku_and_read_date(self) -> None:
        code, _, _ = self.run_set("all", FakeAws())
        self.assertEqual(code, 0)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        for spec in fetch.KEYS:
            with self.subTest(key=spec.key):
                source = data["_sources"][spec.key]
                self.assertIsInstance(source, str)
                self.assertIn("SKU ", source)
                self.assertIn(f"read {READ_DATE}", source)

    def test_attribute_only_keys_describe_their_filters(self) -> None:
        code, _, _ = self.run_set("base", FakeAws())
        self.assertEqual(code, 0)
        source = json.loads(self.out.read_text(encoding="utf-8"))["_sources"][
            "ec2_t3_large_hour"
        ]
        self.assertIn("instanceType=t3.large", source)
        self.assertIn("operatingSystem=Windows", source)

    def test_two_skus_stop_with_exit_3_and_list_both(self) -> None:
        fake = FakeAws(
            {
                "ec2_t3_large_hour": [
                    product("AAAAAAAAAAAAAAAA", "0.1364"),
                    product("BBBBBBBBBBBBBBBB", "0.1088"),
                ]
            }
        )
        code, _, err = self.run_set("base", fake)
        self.assertEqual(code, 3)
        self.assertIn("AAAAAAAAAAAAAAAA", err)
        self.assertIn("BBBBBBBBBBBBBBBB", err)
        self.assertIn("0.1088", err)
        self.assertFalse(self.out.exists())

    def test_zero_candidates_stop_with_exit_3(self) -> None:
        code, _, err = self.run_set("base", FakeAws({"ebs_gp3_gb_month": []}))
        self.assertEqual(code, 3)
        self.assertIn("ebs_gp3_gb_month", err)
        self.assertIn("(no candidates)", err)
        self.assertFalse(self.out.exists())

    def test_every_failing_key_is_reported_in_one_run(self) -> None:
        fake = FakeAws({"ebs_gp3_gb_month": [], "vpce_interface_az_hour": []})
        code, _, err = self.run_set("base", fake)
        self.assertEqual(code, 3)
        self.assertIn("ebs_gp3_gb_month", err)
        self.assertIn("vpce_interface_az_hour", err)

    def test_single_key_with_tiered_dimensions_is_ambiguous(self) -> None:
        tiered = product(
            "TIEREDSKU",
            "0",
            dims=[("0", "100", "0.0000002"), ("100", "Inf", "0.0000001")],
        )
        code, _, err = self.run_set("stage3", FakeAws({"lambda_request": [tiered]}))
        self.assertEqual(code, 3)
        self.assertIn("lambda_request", err)

    def test_first_tier_picks_the_begin_range_zero_dimension(self) -> None:
        tiered = product(
            "TIEREDSKU",
            "0",
            dims=[
                ("0", "6000000000", "0.0000166667"),
                ("6000000000", "15000000000", "0.0000150000"),
                ("15000000000", "Inf", "0.0000133334"),
            ],
        )
        fake = FakeAws({"lambda_gb_second": [tiered]})
        code, _, _ = self.run_set("stage3", fake)
        self.assertEqual(code, 0)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["lambda_gb_second"], 0.0000166667)

    def test_first_tier_with_two_zero_start_dimensions_is_ambiguous(self) -> None:
        both = [product("S1", "0.76"), product("S2", "0.50")]
        code, _, err = self.run_set("stage3", FakeAws({"cwl_ingest_gb": both}))
        self.assertEqual(code, 3)
        self.assertIn("S1", err)
        self.assertIn("S2", err)

    def test_price_list_entries_may_be_objects_instead_of_strings(self) -> None:
        obj = product("SKUOBJECT", "0.035", as_object=True)
        code, _, _ = self.run_set("atx", FakeAws({"transform_agent_minute": [obj]}))
        self.assertEqual(code, 0)


class ReferenceRangeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "prices.json"

    def run_stage3(self, fake: FakeAws, *extra: str):
        return run_main(
            [
                "--set",
                "stage3",
                "--out",
                str(self.out),
                "--read-date",
                READ_DATE,
                *extra,
            ],
            fake,
        )

    def test_per_thousand_price_for_a_per_request_key_is_exit_4(self) -> None:
        # 0.0002 per 1,000 requests read as if it were per request: 1000x the reference.
        fake = FakeAws({"lambda_request": [product("SKUREQ", "0.0002")]})
        code, _, err = self.run_stage3(fake)
        self.assertEqual(code, 4)
        self.assertIn("lambda_request", err)
        self.assertIn("unit mismatch", err)
        self.assertFalse(self.out.exists())

    def test_allow_drift_records_a_warning_instead(self) -> None:
        fake = FakeAws({"lambda_request": [product("SKUREQ", "0.0002")]})
        code, _, _ = self.run_stage3(fake, "--allow-drift")
        self.assertEqual(code, 0)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["lambda_request"], 0.0002)
        self.assertTrue(any("lambda_request" in w for w in data["_warnings"]))

    def test_zero_price_is_out_of_range(self) -> None:
        fake = FakeAws({"cwl_ingest_gb": [product("SKUCWL", "0.0000000000")]})
        code, _, _ = self.run_stage3(fake)
        self.assertEqual(code, 4)

    def test_small_change_is_recorded_not_rejected(self) -> None:
        fake = FakeAws({"cwl_ingest_gb": [product("SKUCWL", "0.80")]})
        code, _, err = self.run_stage3(fake)
        self.assertEqual(code, 0)
        self.assertIn("changed from reference", err)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["cwl_ingest_gb"], 0.80)
        self.assertEqual(
            data["_changed_from_reference"]["cwl_ingest_gb"],
            {"reference": 0.76, "read": 0.80},
        )

    def test_unchanged_values_record_no_change(self) -> None:
        code, _, _ = self.run_stage3(FakeAws())
        self.assertEqual(code, 0)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertEqual(data["_changed_from_reference"], {})
        self.assertEqual(data["_warnings"], [])

    def test_a_different_live_sku_is_a_warning_and_is_recorded(self) -> None:
        fake = FakeAws({"fsx_s3ap_get_request": [product("NEWSKU000", "0.000000029")]})
        code, _, err = self.run_stage3(fake)
        self.assertEqual(code, 0)
        self.assertIn("NEWSKU000", err)
        data = json.loads(self.out.read_text(encoding="utf-8"))
        self.assertTrue(any("NEWSKU000" in w for w in data["_warnings"]))
        self.assertIn("SKU NEWSKU000", data["_sources"]["fsx_s3ap_get_request"])

    def test_check_reference_boundaries(self) -> None:
        spec = fetch.KEYS_BY_NAME["cwl_ingest_gb"]
        self.assertEqual(fetch.check_reference(spec, Decimal("0.76"))[0], "ok")
        self.assertEqual(fetch.check_reference(spec, Decimal("7.6"))[0], "changed")
        self.assertEqual(
            fetch.check_reference(spec, Decimal("7.61"))[0], "out_of_range"
        )
        self.assertEqual(fetch.check_reference(spec, Decimal("0.076"))[0], "changed")
        self.assertEqual(
            fetch.check_reference(spec, Decimal("0.0759"))[0], "out_of_range"
        )


class FailureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "prices.json"

    def run_all(self, fake):
        return run_main(["--set", "all", "--out", str(self.out)], fake)

    def test_expired_token_is_exit_20_after_one_call(self) -> None:
        for stderr in (
            "An error occurred (ExpiredToken) when calling the GetProducts operation",
            "Error when retrieving token from sso: Token has expired and refresh failed",
            "The SSO session associated with this profile has expired or is otherwise invalid.",
        ):
            with self.subTest(stderr=stderr):
                fake = FakeAws()
                fake.failure = subprocess.CompletedProcess([], 255, "", stderr)
                code, _, err = self.run_all(fake)
                self.assertEqual(code, 20)
                self.assertEqual(len(fake.calls), 1)
                self.assertIn("aws sso login", err)
                self.assertFalse(self.out.exists())

    def test_other_aws_failure_is_exit_10(self) -> None:
        fake = FakeAws()
        fake.failure = subprocess.CompletedProcess([], 254, "", "AccessDenied: nope")
        code, _, err = self.run_all(fake)
        self.assertEqual(code, 10)
        self.assertIn("AccessDenied", err)
        self.assertFalse(self.out.exists())

    def test_unparseable_output_is_exit_10(self) -> None:
        fake = FakeAws()
        fake.failure = subprocess.CompletedProcess([], 0, "not json", "")
        code, _, _ = self.run_all(fake)
        self.assertEqual(code, 10)
        fake.failure = subprocess.CompletedProcess([], 0, json.dumps({"x": 1}), "")
        code, _, _ = self.run_all(fake)
        self.assertEqual(code, 10)

    def test_unexpected_entry_shape_is_exit_10(self) -> None:
        bad = json.dumps({"product": {"sku": "X"}, "terms": {"OnDemand": {"t": {}}}})
        code, _, _ = self.run_all(FakeAws({"fsx_ssd_gb_month": [bad]}))
        self.assertEqual(code, 10)

    def test_missing_cli_is_exit_10(self) -> None:
        def missing(argv):
            raise FileNotFoundError("aws")

        code, _, err = self.run_all(missing)
        self.assertEqual(code, 10)
        self.assertIn("aws CLI", err)

    def test_existing_output_is_not_overwritten_without_force(self) -> None:
        self.out.write_text('{"keep": 1}\n', encoding="utf-8")
        fake = FakeAws()
        code, _, err = self.run_all(fake)
        self.assertEqual(code, 2)
        self.assertEqual(fake.calls, [])
        self.assertIn("--force", err)
        self.assertEqual(json.loads(self.out.read_text("utf-8")), {"keep": 1})
        code, _, _ = run_main(
            ["--set", "all", "--out", str(self.out), "--force"], FakeAws()
        )
        self.assertEqual(code, 0)
        self.assertIn("fsx_ssd_gb_month", json.loads(self.out.read_text("utf-8")))

    def test_bad_arguments_are_exit_2(self) -> None:
        cases = [
            [],
            ["--set", "all", "--region-code", "us-east-1"],
            ["--set", "all", "--region-code", "tokyo"],
            ["--set", "all", "--read-date", "12-10-2026"],
            ["--set", "all", "--read-date", "2026-13-40"],
        ]
        for argv in cases:
            with self.subTest(argv=argv):
                fake = FakeAws()
                code, _, _ = run_main([*argv, "--out", str(self.out)], fake)
                self.assertEqual(code, 2)
                self.assertEqual(fake.calls, [])
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            fetch.main(["--set", "nope"])
        self.assertEqual(raised.exception.code, 2)

    def test_help_documents_the_flags_and_exit_codes(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as raised:
            fetch.main(["--help"])
        self.assertEqual(raised.exception.code, 0)
        text = " ".join(out.getvalue().split())
        for flag in (
            "--set",
            "--region-code",
            "--out",
            "--read-date",
            "--dry-run",
            "--explain",
            "--allow-drift",
            "--force",
        ):
            self.assertIn(flag, text)
        self.assertIn("aws sso login", text)


class ExplainTests(unittest.TestCase):
    def test_explain_prints_every_candidate(self) -> None:
        fake = FakeAws(
            {
                "ec2_t3_large_hour": [
                    product("AAAAAAAAAAAAAAAA", "0.1364", description="Windows LI"),
                    product("BBBBBBBBBBBBBBBB", "0.1088", description="Windows BYOL"),
                ]
            }
        )
        code, stdout, _ = run_main(["--explain", "ec2_t3_large_hour"], fake)
        self.assertEqual(code, 0)
        self.assertEqual(len(fake.calls), 1)
        for needle in (
            "AAAAAAAAAAAAAAAA",
            "BBBBBBBBBBBBBBBB",
            "Windows BYOL",
            "0.1088",
        ):
            self.assertIn(needle, stdout)

    def test_explain_of_an_unknown_key_is_refused(self) -> None:
        with (
            contextlib.redirect_stderr(io.StringIO()),
            self.assertRaises(SystemExit) as raised,
        ):
            fetch.main(["--explain", "no_such_key"])
        self.assertEqual(raised.exception.code, 2)

    def test_explain_with_expired_token_is_exit_20(self) -> None:
        fake = FakeAws()
        fake.failure = subprocess.CompletedProcess([], 255, "", "Token has expired")
        code, _, _ = run_main(["--explain", "cwl_ingest_gb"], fake)
        self.assertEqual(code, 20)


class EstimateCompatibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name) / "prices.json"
        code, _, _ = run_main(
            ["--set", "all", "--out", str(self.out), "--read-date", READ_DATE],
            FakeAws(),
        )
        self.assertEqual(code, 0)

    def test_key_set_equals_the_checked_in_fixture(self) -> None:
        # A key added to estimate.py and the fixture without a spec here fails this test.
        fixture = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))
        expected = {k for k in fixture if not k.startswith("_")}
        data = json.loads(self.out.read_text(encoding="utf-8"))
        produced = {k for k in data if not k.startswith("_")}
        self.assertEqual(produced, expected)
        self.assertEqual({spec.key for spec in fetch.KEYS}, expected)

    def test_references_equal_the_checked_in_fixture_values(self) -> None:
        fixture = json.loads((FIXTURES / "prices.json").read_text(encoding="utf-8"))
        for spec in fetch.KEYS:
            with self.subTest(key=spec.key):
                self.assertEqual(float(spec.reference), fixture[spec.key])

    def test_load_prices_accepts_the_output(self) -> None:
        prices = estimate.load_prices(self.out)
        self.assertEqual(set(prices), {spec.key for spec in fetch.KEYS})
        self.assertAlmostEqual(prices["fsx_ssd_gb_month"], 0.150)
        self.assertTrue(all(isinstance(v, float) for v in prices.values()))

    def test_estimate_stage3_accepts_the_output(self) -> None:
        orig = estimate.ESTIMATES_DIR
        estimate.ESTIMATES_DIR = Path(self.tmp.name) / "estimates"
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                code = estimate.main(
                    [
                        "--target",
                        "stage3",
                        "--hours",
                        "4",
                        "--price-fixture",
                        str(self.out),
                    ]
                )
            written = sorted(estimate.ESTIMATES_DIR.glob("*.json"))
        finally:
            estimate.ESTIMATES_DIR = orig
        self.assertEqual(code, 0)
        self.assertEqual(len(written), 1)

    def test_set_selection_limits_the_keys(self) -> None:
        for set_name in ("base", "atx", "stage3"):
            with self.subTest(set=set_name):
                out = Path(self.tmp.name) / f"{set_name}.json"
                code, _, _ = run_main(["--set", set_name, "--out", str(out)], FakeAws())
                self.assertEqual(code, 0)
                keys = {
                    k
                    for k in json.loads(out.read_text(encoding="utf-8"))
                    if not k.startswith("_")
                }
                self.assertEqual(
                    keys,
                    {s.key for s in fetch.KEYS if s.set_name == set_name},
                )

    def test_output_holds_no_local_path_or_account_id(self) -> None:
        text = self.out.read_text(encoding="utf-8")
        self.assertNotIn("/Users/", text)
        self.assertNotRegex(text, r"\b[0-9]{12}\b")


if __name__ == "__main__":
    unittest.main()
