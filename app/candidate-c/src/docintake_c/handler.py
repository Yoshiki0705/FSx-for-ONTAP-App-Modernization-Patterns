"""Candidate C: managed Python Lambda worker/probe for FSx for ONTAP S3 Access Points.

Two explicitly separate execution modes, fixed per the Candidate C spec:

  * ``managed_lambda_decoded_event`` — the managed path. Execution begins at a decoded event
    handed to this handler. It performs exactly one S3 object operation (GetObject, PutObject, or
    ListObjectsV2) against the FSx for ONTAP S3 Access Point, writes a witness record to a separate
    ordinary S3 artifacts bucket, and returns an immutable single-operation result. Only this mode
    can produce managed-Lambda evidence.
  * ``parser_unit_byte_ingress`` — local parser/unit only. It classifies raw bytes with no managed
    invocation identity and no S3 Access Point call, and can never produce managed-Lambda evidence.

The handler keeps the Access Point identity and the ordinary artifacts-bucket witness identity
distinct. It asserts no runtime result on its own; a run is evidence only when executed and recorded
in the Candidate C ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
from typing import Any

import boto3

MANAGED_MODE = "managed_lambda_decoded_event"
PARSER_MODE = "parser_unit_byte_ingress"

# The closed first-slice operation set. No operation outside this set is implicitly included.
MANAGED_OPS = ("get", "put", "list")


def _s3_client() -> Any:
    return boto3.client("s3")


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def handle(event: dict, context: Any = None) -> dict:
    """Lambda entry point. Dispatches on the decoded event's ``mode``.

    The event is already decoded (the managed path does not start at raw byte ingress). Required
    fields: ``mode`` and, for the managed mode, ``op`` plus the Access Point target. The ordinary
    artifacts bucket is read from ``ARTIFACTS_BUCKET``; the Access Point ARN or alias from the event
    (``access_point``) or ``S3_ACCESS_POINT`` for defaults.
    """
    mode = event.get("mode")
    if mode == MANAGED_MODE:
        return _handle_managed(event)
    if mode == PARSER_MODE:
        return _handle_parser(event)
    return {"status": "error", "error_class": "invalid_mode", "mode": mode}


def _handle_managed(event: dict) -> dict:
    op = event.get("op")
    if op not in MANAGED_OPS:
        return {
            "status": "error",
            "mode": MANAGED_MODE,
            "error_class": "invalid_op",
            "op": op,
        }

    access_point = event.get("access_point") or os.environ.get("S3_ACCESS_POINT")
    if not access_point:
        return {
            "status": "error",
            "mode": MANAGED_MODE,
            "error_class": "missing_access_point",
        }

    s3 = _s3_client()
    scenario_id = event.get("scenario_id", op)
    key = event.get("key")

    try:
        if op == "get":
            resp = s3.get_object(Bucket=access_point, Key=key)
            body = resp["Body"].read()
            result = {
                "op": "GetObject",
                "key": key,
                "output_hash": _sha256_hex(body),
                "length": len(body),
                "postcondition": "object_read",
            }
        elif op == "put":
            content = event.get("content", "").encode("utf-8")
            s3.put_object(Bucket=access_point, Key=key, Body=content)
            result = {
                "op": "PutObject",
                "key": key,
                "input_hash": _sha256_hex(content),
                "postcondition": "object_written",
            }
        else:  # list
            prefix = event.get("prefix", "")
            resp = s3.list_objects_v2(Bucket=access_point, Prefix=prefix)
            keys = [o["Key"] for o in resp.get("Contents", [])]
            result = {
                "op": "ListObjectsV2",
                "prefix": prefix,
                "key_count": len(keys),
                "keys": keys,
                "postcondition": "listing_returned",
            }
    except s3.exceptions.ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        http_status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        record = {
            "status": "error",
            "mode": MANAGED_MODE,
            "scenario_id": scenario_id,
            "op": op,
            "error_class": error_code,
            "http_status": http_status,
            "postcondition": "operation_denied_or_failed",
        }
        _write_witness(record)
        return record

    record = {
        "status": "ok",
        "mode": MANAGED_MODE,
        "scenario_id": scenario_id,
        "sequence_id": event.get("sequence_id", 1),
        "sequence_count": event.get("sequence_count", 1),
        "access_point_identity": access_point,
        **result,
    }
    _write_witness(record)
    return record


def _handle_parser(event: dict) -> dict:
    """Local parser/unit path. Classifies raw bytes; never a managed result, never an S3 AP call."""
    raw = event.get("bytes", "")
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    try:
        parsed = json.loads(data.decode("utf-8"))
        classification = "parsed_ok"
        detail = {"keys": sorted(parsed.keys())} if isinstance(parsed, dict) else {}
    except (ValueError, UnicodeDecodeError) as exc:
        classification = "invalid_parser_input"
        detail = {"error": type(exc).__name__}
    return {
        "status": "local",
        "mode": PARSER_MODE,
        "byte_input_hash": _sha256_hex(data),
        "classification": classification,
        "detail": detail,
        "note": "parser/unit result; not managed-Lambda evidence",
    }


def _write_witness(record: dict) -> None:
    """Writes a witness record to the ordinary artifacts bucket (identity separate from the AP)."""
    bucket = os.environ.get("ARTIFACTS_BUCKET")
    if not bucket:
        return
    key = (
        "witness/"
        + str(record.get("scenario_id", "op"))
        + "-"
        + str(record.get("op", ""))
        + ".json"
    )
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
        ContentType="application/json",
    )
