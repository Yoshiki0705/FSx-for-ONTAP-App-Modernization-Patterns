#!/usr/bin/env python3
"""Reject a CloudFormation change set that would replace a resource (stdlib only).

A replacing update on the appmod-base stack would destroy the target volume. This reads a change set
(the JSON from `aws cloudformation describe-change-set`) and fails (exit 1) when any change has
Replacement True, unless its logical ID is in --allow-replacement.

The base stack is not meant to be updated at all; --allow-replacement exists only for the
deliberate, pre-b0 case the design keeps as a general mechanism. The SVM-join-failure recovery does
NOT use this path (it rebuilds the environment instead).

  check-changeset.py --change-set changeset.json [--allow-replacement appmodsvm,appdata]

Reads a saved change set only; it makes no AWS call.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load(path: str) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def replacing_changes(change_set: dict) -> list[tuple[str, str]]:
    """Return (logical_id, replacement) for every resource change that replaces."""
    found: list[tuple[str, str]] = []
    for change in change_set.get("Changes", []):
        resource = change.get("ResourceChange", {})
        replacement = resource.get("Replacement")
        logical_id = resource.get("LogicalResourceId", "<unknown>")
        # Replacement is "True", "Conditional" or "False". Treat True and Conditional as replacing.
        if replacement in ("True", "Conditional"):
            found.append((logical_id, replacement))
    return found


def disallowed(change_set: dict, allowed: set[str]) -> list[tuple[str, str]]:
    return [
        (lid, rep) for lid, rep in replacing_changes(change_set) if lid not in allowed
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--change-set", required=True, help="describe-change-set JSON")
    parser.add_argument(
        "--allow-replacement",
        default="",
        help="comma-separated logical IDs permitted to replace (default: none)",
    )
    args = parser.parse_args(argv)

    allowed = {
        item.strip() for item in args.allow_replacement.split(",") if item.strip()
    }
    change_set = load(args.change_set)
    bad = disallowed(change_set, allowed)
    if bad:
        print("check-changeset: change set would replace a resource:", file=sys.stderr)
        for logical_id, replacement in bad:
            print(f"  - {logical_id}: Replacement={replacement}", file=sys.stderr)
        return 1
    allowed_replacing = [c for c in replacing_changes(change_set) if c[0] in allowed]
    if allowed_replacing:
        print(
            "check-changeset: only allow-listed replacements present: "
            + ", ".join(lid for lid, _ in allowed_replacing)
        )
    else:
        print("check-changeset: no replacing changes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
