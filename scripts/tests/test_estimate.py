#!/usr/bin/env python3
"""Unit tests for scripts/estimate.py (stdlib unittest, no AWS calls).

Covers the design's test-plan rows for estimate.py: the arithmetic from a price fixture, input
validation exit codes, the --elapsed 79% / 81% / no-record cases including "atx after base does not
move the origin", and the --target base minimum recheck (exit 4 on mismatch, exit 0 on match).
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
