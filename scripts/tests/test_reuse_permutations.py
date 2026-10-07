#!/usr/bin/env python3
"""Static checks for the base.yaml create-or-reuse permutations (stdlib only).

The base stack makes five resource kinds switchable between "create new" (the default) and "reuse
an existing one by ID": VPC, subnets, the S3 gateway endpoint, the interface endpoints, and the
AWS Managed Microsoft AD directory. Only the all-new combination is deployed; the reuse
combinations are checked statically here and never deployed.

cfn-guard sees every resource in a template regardless of its Condition, so `make cfn` already
runs the fixed-value rules against the conditional resources. What cfn-guard does NOT do is resolve
Conditions, so it cannot tell whether a given reuse combination still produces a valid template.
cfn-lint DOES evaluate a Condition when the controlling parameter has a fixed Default, pruning the
inactive branch and checking the references that remain. So this test derives one fixture per
permutation from templates/base.yaml by overriding the Create<X> parameter Defaults (and filling
the matching Existing<X> IDs with valid placeholders), then runs BOTH cfn-lint and cfn-guard on
each fixture. A future edit that breaks a reuse path — a dangling !Ref on the reuse branch, or a
guard-held value (SINGLE_AZ_1 / 1024 / 128 / NTFS / Retain / region pin) accidentally made
conditional — fails this test.

The fixtures are generated into a temporary directory on every run, not committed, so they cannot
go stale against base.yaml.

Requires the cfn-lint and cfn-guard binaries, the same ones `make cfn` uses. Run from the
repository root:  python3 -m unittest scripts.tests.test_reuse_permutations
"""

from __future__ import annotations

import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
BASE_TEMPLATE = ROOT / "templates" / "base.yaml"
GUARD_RULES = ROOT / "guard"

# Valid placeholder IDs for the reuse branches. Never real resources; the final digits are only
# format-valid so cfn-lint accepts them.
PLACEHOLDERS = {
    "ExistingVpcId": "vpc-0123456789abcdef0",
    "ExistingPrimarySubnetId": "subnet-0123456789abcdef0",
    "ExistingSecondAzSubnetId": "subnet-0123456789abcdef0",
    "ExistingDirectoryId": "d-0123456789",
    # RFC 5737 TEST-NET-1 documentation addresses: valid-format, not internal (so the gitleaks
    # internal-ip rule does not fire), and never routed to a real host.
    "ExistingDirectoryDnsIps": "192.0.2.10,192.0.2.11",
}

# One permutation per required static-check combination. Each maps a Create<X> parameter to the
# default it should carry in the fixture; parameters not listed keep base.yaml's default (true).
# The Create<X> flags are String parameters with AllowedValues ['true', 'false'], so the override
# value is quoted ('false') to stay a string rather than a YAML boolean.
PERMUTATIONS: dict[str, dict[str, str]] = {
    # The deploy target. All five kinds created new.
    "all-new": {},
    "reuse-vpc": {"CreateVpc": "'false'"},
    "reuse-subnets": {"CreateSubnets": "'false'"},
    "reuse-endpoints": {"CreateInterfaceEndpoints": "'false'"},
    "reuse-ad": {"CreateDirectory": "'false'"},
}


def _cfn_lint() -> str:
    venv = ROOT / ".venv" / "bin" / "cfn-lint"
    return str(venv) if venv.exists() else "cfn-lint"


def _set_default(template: str, parameter: str, value: str) -> str:
    """Set the Default of a top-level parameter. Matches the first `Default:` line that follows the
    parameter's declaration, before the next parameter or the Rules/Conditions/Resources block."""
    # Find the parameter block start.
    start = re.search(rf"^  {re.escape(parameter)}:\s*$", template, re.MULTILINE)
    if not start:
        raise AssertionError(f"parameter {parameter} not found in base.yaml")
    # The block ends at the next 2-space-indented key or a top-level section.
    rest = template[start.end() :]
    end_match = re.search(r"^  \S|^[A-Za-z]", rest, re.MULTILINE)
    end = start.end() + (end_match.start() if end_match else len(rest))
    block = template[start.end() : end]
    new_block, count = re.subn(
        r"^(    Default:)(?:[^\n]*)$",
        rf"\g<1> {value}",
        block,
        count=1,
        flags=re.MULTILINE,
    )
    if count != 1:
        raise AssertionError(f"no Default line to override for parameter {parameter}")
    return template[: start.end()] + new_block + template[end:]


def render(name: str, overrides: dict[str, str]) -> str:
    """Produce a fixture template for a permutation by overriding Create<X> and, for every kind set
    to reuse, filling the matching Existing<X> IDs so the reuse branch resolves to a real value."""
    template = BASE_TEMPLATE.read_text(encoding="utf-8")
    for parameter, value in overrides.items():
        template = _set_default(template, parameter, value)
    # When a kind is reused, its Existing<X> IDs must be non-empty for cfn-lint to resolve the
    # reuse branch; the all-new fixture leaves them empty (the create branch is active).
    reuse_fills: dict[str, list[str]] = {
        "CreateVpc": ["ExistingVpcId"],
        "CreateSubnets": ["ExistingPrimarySubnetId", "ExistingSecondAzSubnetId"],
        "CreateDirectory": ["ExistingDirectoryId", "ExistingDirectoryDnsIps"],
    }
    for parameter, value in overrides.items():
        if value.strip("'") == "false":
            for existing in reuse_fills.get(parameter, []):
                template = _set_default(template, existing, PLACEHOLDERS[existing])
    # Reusing a VPC means its route tables are supplied, not created.
    if overrides.get("CreateVpc", "").strip("'") == "false":
        template = _set_default(template, "RouteTableIds", "rtb-0123456789abcdef0")
    return template


class ReusePermutationStaticChecks(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if shutil.which(_cfn_lint()) is None and not Path(_cfn_lint()).exists():
            raise unittest.SkipTest("cfn-lint not installed")
        if shutil.which("cfn-guard") is None:
            raise unittest.SkipTest("cfn-guard not installed")
        cls.tmp = Path(tempfile.mkdtemp(prefix="reuse-perms-"))

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def _fixture(self, name: str, overrides: dict[str, str]) -> Path:
        path = self.tmp / f"{name}.yaml"
        path.write_text(render(name, overrides), encoding="utf-8")
        return path

    def test_permutations_pass_cfn_lint_and_cfn_guard(self) -> None:
        for name, overrides in PERMUTATIONS.items():
            with self.subTest(permutation=name):
                fixture = self._fixture(name, overrides)
                lint = subprocess.run(
                    [_cfn_lint(), str(fixture)],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    lint.returncode,
                    0,
                    msg=f"cfn-lint failed on {name}:\n{lint.stdout}\n{lint.stderr}",
                )
                guard = subprocess.run(
                    [
                        "cfn-guard",
                        "validate",
                        "--data",
                        str(fixture),
                        "--rules",
                        str(GUARD_RULES),
                        "--show-summary",
                        "fail",
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(
                    guard.returncode,
                    0,
                    msg=f"cfn-guard failed on {name}:\n{guard.stdout}\n{guard.stderr}",
                )


if __name__ == "__main__":
    unittest.main()
