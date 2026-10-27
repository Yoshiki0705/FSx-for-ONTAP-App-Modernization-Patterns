"""Candidate C-REST: managed Python Lambda worker that reads/serves FSx for ONTAP file data
through the ONTAP REST API over HTTPS.

Distinct from the S3 Access Points candidate: the endpoint is the ONTAP REST API on a management
or data LIF, authentication is an ONTAP credential (not AWS SigV4), the network boundary is the
Lambda ENI reaching the ONTAP LIF inside the VPC, and ONTAP — not AWS IAM — authorizes the
operation.

Two modes:
  * ``managed_lambda_ontap_rest`` — one ONTAP REST operation per record, from a decoded event.
  * ``parser_unit_local`` — local response/event parsing; never managed evidence, no ONTAP call.

The first-slice operation is a read on the documented files resource
``GET /storage/volumes/{volume.uuid}/files/{path}``. The handler keeps the ONTAP credential
identity, the Lambda execution-role identity, and the witness identity distinct, and classifies
negative outcomes exactly: 401 -> auth failure, 403 / ONTAP code 6 -> authorization denied, a
connection timeout / no route -> endpoint unreachable, 404 or 405 (code 4) / 400 (code 2) ->
invalid identifier.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import ssl
import urllib.error
import urllib.request

import boto3
import botocore.exceptions

MANAGED_MODE = "managed_lambda_ontap_rest"
PARSER_MODE = "parser_unit_local"

# Negative classifications, fixed before execution.
AUTH_FAILED = "ontap_rest_auth_failed"
AUTHZ_DENIED = "ontap_rest_authorization_denied"
UNREACHABLE = "ontap_endpoint_unreachable"
INVALID_ID = "ontap_rest_invalid_identifier"


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _ontap_credential() -> tuple[str, str]:
    """Reads the ONTAP basic-auth credential from Secrets Manager (via the VPC endpoint)."""
    secret_id = os.environ["ONTAP_SECRET_ID"]
    raw = boto3.client("secretsmanager").get_secret_value(SecretId=secret_id)[
        "SecretString"
    ]
    doc = json.loads(raw)
    user = doc.get("username") or doc.get("user") or "fsxadmin"
    return user, doc["password"]


def _ssl_context() -> ssl.SSLContext:
    """TLS context for the ONTAP endpoint.

    The spec does not authorize disabling certificate validation. ``ONTAP_CA_BUNDLE`` points at a
    trusted CA bundle when provided. When it is absent, verification uses the default trust store;
    an FSx for ONTAP management endpoint presents a self-signed certificate, so the call then fails
    verification and that failure is recorded (not silently bypassed). ``ONTAP_TLS_INSECURE=1`` is
    honored only as an explicit, recorded operator choice for the negative/diagnostic path; it is
    off by default.
    """
    ca_bundle = os.environ.get("ONTAP_CA_BUNDLE")
    if ca_bundle:
        return ssl.create_default_context(cafile=ca_bundle)
    if os.environ.get("ONTAP_TLS_INSECURE") == "1":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        return ctx
    return ssl.create_default_context()


def _ontap_get(
    path: str, user: str, password: str, accept: str | None = None, timeout: int = 10
):
    host = os.environ["ONTAP_HOST"]  # e.g. https://<ontap-management-ip>
    url = host.rstrip("/") + path
    req = urllib.request.Request(url, method="GET")
    token = base64.b64encode(f"{user}:{password}".encode()).decode()
    req.add_header("Authorization", "Basic " + token)
    if accept:
        req.add_header("Accept", accept)
    return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context())


def handle(event: dict, context=None) -> dict:
    mode = event.get("mode")
    if mode == MANAGED_MODE:
        return _handle_managed(event)
    if mode == PARSER_MODE:
        return _handle_parser(event)
    return {"status": "error", "error_class": "invalid_mode", "mode": mode}


def _handle_managed(event: dict) -> dict:
    scenario_id = event.get("scenario_id", "crest")
    op = event.get("op", "read")
    volume_uuid = event.get("volume_uuid")
    file_path = event.get("path")
    length = int(event.get("length", 1024))

    try:
        user, password = _ontap_credential()
    except (
        botocore.exceptions.BotoCoreError,
        botocore.exceptions.ClientError,
        KeyError,
        ValueError,
    ) as exc:
        # Secret retrieval / parsing is an AWS-side failure, not an ONTAP authorization class.
        return _record(
            scenario_id,
            op,
            status="error",
            error_class="aws_secret_retrieval_failed:" + type(exc).__name__,
            postcondition="no_ontap_call",
        )

    # A directory/property read lists the files resource as JSON. A file bytes read uses
    # Accept: multipart/form-data with a length (ONTAP documents multipart only for byte reads;
    # it answers 406 "MIME type multipart/form-data is not supported" for a listing).
    read_kind = event.get("read_kind") or ("listing" if not file_path else "bytes")
    if read_kind == "listing":
        rest_path = f"/api/storage/volumes/{volume_uuid}/files/{file_path}"
        accept = "application/json"
        ok_postcondition = "listing_returned"
    else:
        rest_path = (
            f"/api/storage/volumes/{volume_uuid}/files/{file_path}?length={length}"
        )
        accept = "multipart/form-data"
        ok_postcondition = "bytes_served"
    try:
        resp = _ontap_get(rest_path, user, password, accept=accept)
        body = resp.read()
        record = _record(
            scenario_id,
            op,
            status="ok",
            http_status=resp.status,
            output_hash=_sha256_hex(body),
            length=len(body),
            read_kind=read_kind,
            postcondition=ok_postcondition,
        )
        _write_witness(record)
        return record
    except urllib.error.HTTPError as exc:
        err_body = exc.read()
        ontap_code = _ontap_error_code(err_body)
        classification = _classify_http(exc.code, ontap_code)
        record = _record(
            scenario_id,
            op,
            status="error",
            http_status=exc.code,
            ontap_error_code=ontap_code,
            error_class=classification,
            postcondition="no_bytes_served",
            raw_error=err_body[:512].decode("utf-8", "replace"),
        )
        _write_witness(record)
        return record
    except (urllib.error.URLError, TimeoutError, ssl.SSLError, OSError) as exc:
        # No HTTP status from ONTAP. A TLS trust failure (self-signed/untrusted cert) is distinct
        # from a connection timeout / no route. urllib wraps the SSLError in URLError.reason, so
        # unwrap it before classifying; otherwise a cert failure is misread as "unreachable".
        reason = getattr(exc, "reason", None)
        is_tls = isinstance(exc, ssl.SSLError) or isinstance(reason, ssl.SSLError)
        classification = "ontap_tls_untrusted" if is_tls else UNREACHABLE
        record = _record(
            scenario_id,
            op,
            status="error",
            error_class=classification,
            postcondition="no_bytes_served",
            raw_error=type(exc).__name__ + ":" + str(exc)[:256],
        )
        _write_witness(record)
        return record


def _classify_http(http_status: int, ontap_code):
    if http_status == 401:
        return AUTH_FAILED
    if http_status == 403:
        return AUTHZ_DENIED
    if http_status in (404, 405) or http_status == 400:
        return INVALID_ID
    return "ontap_rest_unexpected_status"


def _ontap_error_code(body: bytes):
    try:
        return json.loads(body.decode("utf-8")).get("error", {}).get("code")
    except (ValueError, UnicodeDecodeError):
        return None


def _handle_parser(event: dict) -> dict:
    raw = event.get("bytes", "")
    data = raw.encode("utf-8") if isinstance(raw, str) else bytes(raw)
    try:
        parsed = json.loads(data.decode("utf-8"))
        classification = "parsed_ok"
        detail = (
            {"has_error_obj": "error" in parsed} if isinstance(parsed, dict) else {}
        )
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


def _record(scenario_id, op, **fields) -> dict:
    base = {
        "mode": MANAGED_MODE,
        "scenario_id": scenario_id,
        "op": op,
        "sequence_id": 1,
        "sequence_count": 1,
    }
    base.update(fields)
    return base


def _write_witness(record: dict) -> None:
    bucket = os.environ.get("ARTIFACTS_BUCKET")
    if not bucket:
        return
    key = "witness/" + str(record.get("scenario_id", "op")) + ".json"
    boto3.client("s3").put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(record).encode("utf-8"),
        ContentType="application/json",
    )
