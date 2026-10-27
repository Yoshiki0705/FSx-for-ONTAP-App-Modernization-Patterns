"""Tier 1 (local/unit) tests for the Candidate C handler.

These use moto to fake S3 locally. They establish the handler's dispatch, mode separation, and
output shape only. They do NOT establish FSx for ONTAP S3 Access Point behavior, policy-denial
error classes, or CloudTrail evidence — those are Tier 2/3 real-environment concerns. With a plain
moto S3 bucket standing in for the access point, a "bucket" name is used where the real run passes
an access point ARN or alias.
"""

import json

import boto3
import pytest
from docintake_c import handler
from moto import mock_aws

REGION = "us-east-1"
AP = "candc-fake-ap"
ARTIFACTS = "candc-fake-artifacts"


@pytest.fixture
def s3_env(monkeypatch):
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(Bucket=AP)
        client.create_bucket(Bucket=ARTIFACTS)
        monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
        monkeypatch.setenv("ARTIFACTS_BUCKET", ARTIFACTS)
        yield client


def test_put_then_get_roundtrip(s3_env):
    put = handler.handle(
        {
            "mode": handler.MANAGED_MODE,
            "op": "put",
            "access_point": AP,
            "key": "seed/doc-001.txt",
            "content": "alpha",
            "scenario_id": "p1",
        }
    )
    assert put["status"] == "ok"
    assert put["op"] == "PutObject"
    assert put["postcondition"] == "object_written"

    get = handler.handle(
        {
            "mode": handler.MANAGED_MODE,
            "op": "get",
            "access_point": AP,
            "key": "seed/doc-001.txt",
            "scenario_id": "p2",
        }
    )
    assert get["status"] == "ok"
    assert get["op"] == "GetObject"
    assert get["length"] == len("alpha")
    assert get["postcondition"] == "object_read"


def test_list_returns_keys(s3_env):
    for i in range(3):
        handler.handle(
            {
                "mode": handler.MANAGED_MODE,
                "op": "put",
                "access_point": AP,
                "key": f"seed/doc-{i}.txt",
                "content": str(i),
                "scenario_id": f"s{i}",
            }
        )
    out = handler.handle(
        {
            "mode": handler.MANAGED_MODE,
            "op": "list",
            "access_point": AP,
            "prefix": "seed/",
            "scenario_id": "l1",
        }
    )
    assert out["status"] == "ok"
    assert out["op"] == "ListObjectsV2"
    assert out["key_count"] == 3
    assert out["postcondition"] == "listing_returned"


def test_get_missing_key_returns_error_class(s3_env):
    out = handler.handle(
        {
            "mode": handler.MANAGED_MODE,
            "op": "get",
            "access_point": AP,
            "key": "seed/missing.txt",
            "scenario_id": "n1",
        }
    )
    assert out["status"] == "error"
    # moto returns NoSuchKey; the real policy-denial error class (AccessDenied) is a Tier 2/3 case.
    assert out["error_class"] in ("NoSuchKey", "NoSuchBucket", "AccessDenied")


def test_witness_written_to_separate_bucket(s3_env):
    handler.handle(
        {
            "mode": handler.MANAGED_MODE,
            "op": "put",
            "access_point": AP,
            "key": "seed/w.txt",
            "content": "x",
            "scenario_id": "w1",
        }
    )
    objs = s3_env.list_objects_v2(Bucket=ARTIFACTS, Prefix="witness/")
    assert objs.get("KeyCount", 0) >= 1


def test_parser_mode_never_managed():
    out = handler.handle({"mode": handler.PARSER_MODE, "bytes": "{not json"})
    assert out["status"] == "local"
    assert out["mode"] == handler.PARSER_MODE
    assert out["classification"] == "invalid_parser_input"
    assert "byte_input_hash" in out
    # A parser record carries no managed invocation identity and is not managed evidence.
    assert "access_point_identity" not in out


def test_parser_mode_valid_json():
    out = handler.handle(
        {"mode": handler.PARSER_MODE, "bytes": json.dumps({"b": 1, "a": 2})}
    )
    assert out["classification"] == "parsed_ok"
    assert out["detail"]["keys"] == ["a", "b"]


def test_invalid_mode_and_op():
    assert handler.handle({"mode": "nope"})["error_class"] == "invalid_mode"
    assert (
        handler.handle({"mode": handler.MANAGED_MODE, "op": "delete"})["error_class"]
        == "invalid_op"
    )
