#!/usr/bin/env python3
"""Pure helpers shared by the scripts under scripts/measure (stdlib only, no network at import).

Nothing here reads, prints or writes a secret. The validators exist so that a live identifier
never has to be written into a script: every ID, bucket, path and region reaches the drivers as an
argument or an APPMOD_* environment variable, and is checked by shape before it is used.
"""

from __future__ import annotations

import re
import shlex
import subprocess
from collections.abc import Callable, Mapping

DEFAULT_REGION = "ap-northeast-1"

INSTANCE_ID_RE = re.compile(r"^i-[0-9a-f]{8,17}$")
# s0-, s2m-, s2n-, s3m-, s4c-, s0w- followed by a UTC timestamp.
RUN_ID_RE = re.compile(r"^s[0-9][a-z]{0,2}-[0-9]{8}T[0-9]{6}Z$")
# A stage directory name under out/. The Worker's own default is s0, so this is also the guard
# that keeps a typo from landing in a recorded stage.
STAGE_NAME_RE = re.compile(r"^s[0-9][a-z]{0,2}$")
REGION_RE = re.compile(r"^[a-z]{2}(-[a-z]+)+-[0-9]$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
COMPONENT_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}$")
ENV_NAME_RE = re.compile(r"^APPMOD_[A-Z0-9_]+$")
# Deliberately narrow: no whitespace, quotes, $, backticks or ;. A password-shaped value is
# refused by shape. Never pass a secret through --env.
ENV_VALUE_RE = re.compile(r"^[A-Za-z0-9._/:@,=+-]*$")
DEST_DIR_RE = re.compile(r"^/opt/appmod/[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")
RELPATH_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)*$")

EXIT_OK = 0
EXIT_REMOTE_FAILED = 1
EXIT_BAD_ARGUMENT = 2
EXIT_SEND_FAILED = 10
EXIT_GET_FAILED = 11
EXIT_NO_INVOCATION = 12
EXIT_EXPIRED_TOKEN = 20

EXPIRED_MESSAGE = (
    "AWS credentials have expired: run `aws sso login` yourself; "
    "this script does not try another credential path"
)


class ExpiredTokenError(Exception):
    """The AWS CLI reported an expired or invalid SSO/STS token."""


Runner = Callable[..., "subprocess.CompletedProcess[str]"]


def _check(pattern: re.Pattern[str], value: str, what: str, hint: str) -> str:
    if not isinstance(value, str) or not pattern.match(value):
        raise ValueError(f"invalid {what}: {value!r} ({hint})")
    return value


def validate_instance_id(value: str) -> str:
    return _check(
        INSTANCE_ID_RE, value, "instance id", "expected i- followed by 8-17 hex digits"
    )


def validate_run_id(value: str) -> str:
    return _check(
        RUN_ID_RE, value, "run id", "expected s<digit>[a-z]{0,2}-YYYYMMDDTHHMMSSZ"
    )


def validate_stage_name(value: str) -> str:
    return _check(STAGE_NAME_RE, value, "stage name", "expected s<digit>[a-z]{0,2}")


def validate_region(value: str) -> str:
    return _check(REGION_RE, value, "region", "expected a form like ap-northeast-1")


def validate_bucket(value: str) -> str:
    return _check(BUCKET_RE, value, "bucket name", "3-63 chars of a-z 0-9 . -")


def validate_component(value: str) -> str:
    return _check(
        COMPONENT_RE, value, "component name", "expected [a-z][a-z0-9-]{0,30}"
    )


def validate_relpath(value: str) -> str:
    _check(
        RELPATH_RE, value, "relative path", "expected path segments of [A-Za-z0-9._-]"
    )
    if any(seg in ("", ".", "..") for seg in value.split("/")):
        raise ValueError(
            f"invalid relative path: {value!r} (no empty, . or .. segment)"
        )
    return value


def validate_dest_dir(value: str) -> str:
    """An absolute destination under /opt/appmod/ with no .. segment."""
    _check(
        DEST_DIR_RE,
        value,
        "destination directory",
        "expected /opt/appmod/<name>[/<name>...]",
    )
    if any(seg in (".", "..") for seg in value.split("/")):
        raise ValueError(
            f"invalid destination directory: {value!r} (no . or .. segment)"
        )
    return value


def validate_env_item(item: str) -> tuple[str, str]:
    """Split NAME=VALUE; the name must be APPMOD_* and the value must match the narrow shape."""
    if "=" not in item:
        raise ValueError(f"invalid --env {item!r}: expected NAME=VALUE")
    name, value = item.split("=", 1)
    _check(
        ENV_NAME_RE,
        name,
        "environment variable name",
        "only APPMOD_[A-Z0-9_]+ is accepted",
    )
    _check(
        ENV_VALUE_RE,
        value,
        f"value for {name}",
        "characters outside [A-Za-z0-9._/:@,=+-]",
    )
    return name, value


def parse_env_items(items: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in items:
        name, value = validate_env_item(item)
        out[name] = value
    return out


def env_preamble(platform: str, mapping: Mapping[str, str]) -> list[str]:
    """Command lines that set the variables for the script run next.

    bash: export K='V' (shlex.quote). PowerShell: $env:K='V' with single quotes doubled.
    """
    lines: list[str] = []
    for name in sorted(mapping):
        value = mapping[name]
        _check(
            ENV_NAME_RE, name, "environment variable name", "only APPMOD_* is accepted"
        )
        if platform == "linux":
            lines.append(f"export {name}={shlex.quote(value)}")
        elif platform == "windows":
            escaped = value.replace("'", "''")
            lines.append(f"$env:{name}='{escaped}'")
        else:
            raise ValueError(f"invalid platform: {platform!r}")
    return lines


_IP_HOSTNAME_RE = re.compile(
    r"\bip-\d{1,3}-\d{1,3}-\d{1,3}-\d{1,3}(?:\.[A-Za-z0-9.-]+)?"
)
_IPV4_RE = re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b")


def scrub(text: str, bucket: str | None = None) -> str:
    """Replace the bucket name, EC2 internal hostnames and IPv4 addresses in text to be saved."""
    if bucket:
        text = text.replace(bucket, "ARTIFACTS_BUCKET")
    text = _IP_HOSTNAME_RE.sub("linux-host", text)
    return _IPV4_RE.sub("x.x.x.x", text)


def is_expired_token(stderr: str) -> bool:
    return "Token has expired" in stderr or "ExpiredToken" in stderr


def run_aws(args: list[str], runner: Runner = subprocess.run) -> tuple[int, str, str]:
    """Run `aws <args>` as an argv list (never a shell string).

    Raises ExpiredTokenError when the CLI reports an expired token, so a caller stops instead of
    retrying or looking for another credential path.
    """
    proc = runner(["aws", *args], capture_output=True, text=True)
    if proc.returncode != 0 and is_expired_token(proc.stderr or ""):
        raise ExpiredTokenError(EXPIRED_MESSAGE)
    return proc.returncode, proc.stdout, proc.stderr
