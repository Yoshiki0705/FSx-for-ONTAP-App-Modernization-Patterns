#!/usr/bin/env python3
"""Static checks for scripts/measure/windows/*.ps1.

PowerShell is not linted or executed by any gate in this repository, so these checks read the
text: the documented parameters exist, the out\\<Stage> guard is in front of the Worker start, the
SMB mapping is removed in a finally block, no secret is printed, nothing is deleted or moved, and
no live value from the original one-off scripts is left in. They do not prove the scripts run.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

WINDOWS = Path(__file__).resolve().parent.parent / "measure" / "windows"
PREP = WINDOWS / "prep-worker.ps1"
RUN = WINDOWS / "run-worker.ps1"


def code_of(path: Path) -> str:
    """The script without its comment-based help block and # comment lines."""
    text = re.sub(r"<#.*?#>", "", path.read_text(), flags=re.DOTALL)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def param_names(path: Path) -> list[str]:
    block = path.read_text().split("param(", 1)[1]
    return re.findall(r"\[(?:string|int)\]\$([A-Za-z]+)", block.split("\n)\n", 1)[0])


class CommonTests(unittest.TestCase):
    def test_both_scripts_exist_with_help_and_strict_errors(self) -> None:
        for path in (PREP, RUN):
            text = path.read_text()
            self.assertTrue(text.startswith("<#"), msg=path.name)
            self.assertIn(".SYNOPSIS", text)
            self.assertIn("[CmdletBinding()]", text)
            self.assertIn('$ErrorActionPreference = "Stop"', text)

    def test_no_live_values_from_the_one_off_scripts(self) -> None:
        for path in (PREP, RUN):
            text = path.read_text()
            for literal in (
                "DocIntake-case2",
                "src-case2",
                "C:\\appmod\\DocIntake-worker\\DocIntake.Worker.exe",
            ):
                self.assertNotIn(literal, text, msg=f"{path.name}: {literal}")
            self.assertIsNone(re.search(r"\bi-[0-9a-f]{8,17}\b", text))
            self.assertIsNone(re.search(r"\b\d{12}\b", text))
            self.assertIsNone(re.search(r"\b\d{1,3}(\.\d{1,3}){3}\b", text))

    def test_nothing_is_deleted_or_moved(self) -> None:
        for path in (PREP, RUN):
            text = code_of(path)
            for cmd in (
                "Remove-Item",
                "Move-Item",
                "Rename-Item",
                "Clear-Content",
                "Set-Content",
                "Out-File",
                "rmdir",
                "del ",
            ):
                self.assertNotIn(cmd, text, msg=f"{path.name}: {cmd}")


class PrepTests(unittest.TestCase):
    def test_parameters(self) -> None:
        names = param_names(PREP)
        self.assertEqual(names, ["SourceDir", "DestDir", "ExeName"])
        self.assertIn("Mandatory = $true)][string]$SourceDir", PREP.read_text())
        self.assertIn("Mandatory = $true)][string]$DestDir", PREP.read_text())

    def test_existing_destination_is_refused_before_anything_is_created(self) -> None:
        text = code_of(PREP)
        guard = text.index("Test-Path -LiteralPath $DestDir")
        self.assertIn("already exists", text[guard : guard + 120])
        self.assertLess(guard, text.index("New-Item"))
        self.assertLess(guard, text.index("Copy-Item"))


class RunTests(unittest.TestCase):
    def test_parameters_and_defaults(self) -> None:
        text = RUN.read_text()
        self.assertEqual(
            param_names(RUN),
            [
                "Stage",
                "ExeDir",
                "ExeName",
                "SvmNetbios",
                "Domain",
                "SecretId",
                "Region",
            ],
        )
        self.assertIn(
            "[ValidatePattern('^s[0-9][a-z]{0,2}$')][string]$Stage = \"s0\"", text
        )
        self.assertIn('$SvmNetbios = "APPMODSVM01"', text)
        self.assertIn('$Domain = "APPMOD"', text)
        self.assertIn('$SecretId = "appmod/app-users"', text)
        self.assertIn('$Region = "ap-northeast-1"', text)
        self.assertIn("Mandatory = $true)][string]$ExeDir", text)

    def test_stage_guard_precedes_the_worker_start(self) -> None:
        text = code_of(RUN)
        guard = text.index('throw "GUARD: out\\$Stage already exists; not running"')
        self.assertLess(guard, text.index("Start-Process"))
        self.assertIn('Test-Path -LiteralPath "$remote\\out\\$Stage"', text)

    def test_config_must_match_stage_and_share_before_any_mapping(self) -> None:
        text = code_of(RUN)
        stage = text.index("config Stage is")
        share = text.index("config SmbRoot is")
        mapping = text.index("New-SmbMapping")
        self.assertLess(stage, mapping)
        self.assertLess(share, mapping)
        self.assertLess(
            text.index("not found; the Worker's stage cannot be checked"), mapping
        )

    def test_mapping_is_removed_in_finally(self) -> None:
        text = code_of(RUN)
        m = re.search(r"finally\s*\{(.*)\}\s*$", text, flags=re.DOTALL)
        self.assertIsNotNone(m)
        self.assertIn("Remove-SmbMapping -RemotePath $remote", m.group(1))
        # The Worker start is inside the try block that this finally belongs to.
        self.assertLess(text.index("try {"), text.index("Start-Process"))
        self.assertLess(text.index("Start-Process"), text.index("finally"))

    def test_secret_is_used_once_cleared_and_never_printed(self) -> None:
        text = code_of(RUN)
        self.assertEqual(text.count("$appUsers.appsvc"), 1)
        self.assertLess(text.index("New-SmbMapping"), text.index("$appUsers = $null"))
        self.assertLess(text.index("$appUsers = $null"), text.index("try {"))
        for line in text.splitlines():
            if re.search(r"Write-Host|Write-Output|Write-Verbose|echo", line):
                self.assertNotRegex(
                    line, r"\$(appUsers|password|secret|SecretString)", msg=line
                )
        # Output lines are bare strings; none of them mentions the secret variable.
        for line in text.splitlines():
            if line.lstrip().startswith('"'):
                self.assertNotIn("$appUsers", line)

    def test_reports_utc_times_exit_code_and_smb_listing(self) -> None:
        text = code_of(RUN)
        for needle in (
            "run-start-utc",
            "run-end-utc",
            "WORKER-EXIT=$exit",
            "SMB listing of out\\$Stage",
        ):
            self.assertIn(needle, text)


if __name__ == "__main__":
    unittest.main()
