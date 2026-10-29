"""Tier 1 (local/unit) tests for the Candidate C-REST handler.

These exercise the parser mode and the negative-classification mapping with no real ONTAP endpoint.
They establish local algorithmic behavior only; the real ONTAP REST read/serve, the actual
401/403/404 responses, and endpoint reachability are Tier 2/3 real-environment concerns.
"""

import json

from docintake_crest import handler


def test_classify_http_maps_exact_classes():
    assert handler._classify_http(401, None) == handler.AUTH_FAILED
    assert handler._classify_http(403, 6) == handler.AUTHZ_DENIED
    assert handler._classify_http(404, 4) == handler.INVALID_ID
    assert handler._classify_http(405, 4) == handler.INVALID_ID
    assert handler._classify_http(400, 2) == handler.INVALID_ID
    assert handler._classify_http(500, None) == "ontap_rest_unexpected_status"


def test_ontap_error_code_parsing():
    assert handler._ontap_error_code(b'{"error":{"code":6,"message":"denied"}}') == 6
    assert handler._ontap_error_code(b"not json") is None


def test_parser_mode_invalid_bytes():
    out = handler.handle({"mode": handler.PARSER_MODE, "bytes": "{bad json"})
    assert out["status"] == "local"
    assert out["mode"] == handler.PARSER_MODE
    assert out["classification"] == "invalid_parser_input"
    assert "byte_input_hash" in out
    assert "http_status" not in out  # parser mode makes no ONTAP call


def test_parser_mode_valid_error_object():
    out = handler.handle({"mode": handler.PARSER_MODE, "bytes": '{"error":{"code":4}}'})
    assert out["classification"] == "parsed_ok"
    assert out["detail"]["has_error_obj"] is True


def test_invalid_mode():
    assert handler.handle({"mode": "nope"})["error_class"] == "invalid_mode"


def test_tls_failure_classified_distinct_from_unreachable():
    """A urllib URLError wrapping an ssl.SSLError is ontap_tls_untrusted, not unreachable.

    Regression for a real-environment finding: the FSx for ONTAP management endpoint presents a
    self-signed certificate, so a verified call raises URLError(reason=SSLError). That must map to
    ontap_tls_untrusted, not ontap_endpoint_unreachable.
    """
    import ssl
    import urllib.error

    tls_wrapped = urllib.error.URLError(ssl.SSLError("CERTIFICATE_VERIFY_FAILED"))
    reason = getattr(tls_wrapped, "reason", None)
    is_tls = isinstance(tls_wrapped, ssl.SSLError) or isinstance(reason, ssl.SSLError)
    assert is_tls is True

    plain = urllib.error.URLError("timed out")
    reason2 = getattr(plain, "reason", None)
    is_tls2 = isinstance(plain, ssl.SSLError) or isinstance(reason2, ssl.SSLError)
    assert is_tls2 is False


def _fake_response(body: bytes, content_type: str = "application/json"):
    class _Resp:
        status = 200
        headers = {"Content-Type": content_type}

        def read(self):
            return body

    return _Resp()


def test_multipart_roundtrip():
    data = bytes(range(256)) * 4
    body, ctype = handler._multipart_body(data)
    assert handler._multipart_payload(ctype, body) == data


def test_payload_bytes_is_deterministic_and_exact():
    a = handler._payload_bytes({"generate_bytes": 2_500_000, "seed": "x"})
    b = handler._payload_bytes({"generate_bytes": 2_500_000, "seed": "x"})
    assert len(a) == 2_500_000 and a == b
    assert a != handler._payload_bytes({"generate_bytes": 2_500_000, "seed": "y"})


def test_write_file_creates_then_chunks_below_the_one_megabyte_limit(monkeypatch):
    calls = []

    def fake_request(method, path, user, password, **kwargs):
        calls.append((method, path, len(kwargs.get("body") or b"")))
        return _fake_response(b"{}")

    monkeypatch.setattr(handler, "_ontap_request", fake_request)
    event = {"generate_bytes": 2_500_000}
    rec = handler._write_file("w", "write", "uuid", "big.bin", event, "u", "p")
    assert [c[0] for c in calls] == ["POST", "PATCH", "PATCH", "PATCH"]
    assert [c[1].split("byte_offset=")[-1] for c in calls[1:]] == [
        "0",
        "1000000",
        "2000000",
    ]
    assert rec["ontap_calls"] == 4 and rec["written_length"] == 2_500_000
    assert rec["written_hash"] == handler._sha256_hex(handler._payload_bytes(event))


def test_read_all_issues_ranged_reads_and_hashes_the_whole_file(monkeypatch):
    data = handler._payload_bytes({"generate_bytes": 2_500_000})
    seen = []

    def fake_get(path, user, password, accept=None, timeout=10):
        if accept == "application/json":
            return _fake_response(json.dumps({"records": [{"size": len(data)}]}).encode())
        q = dict(p.split("=") for p in path.split("?")[1].split("&"))
        off, ln = int(q["byte_offset"]), int(q["length"])
        seen.append((off, ln))
        body, ctype = handler._multipart_body(data[off : off + ln])
        return _fake_response(body, ctype)

    monkeypatch.setattr(handler, "_ontap_get", fake_get)
    rec = handler._read_all("r", "read", "uuid", "big.bin", {}, "u", "p")
    assert seen == [(0, 1_000_000), (1_000_000, 1_000_000), (2_000_000, 500_000)]
    assert rec["output_hash"] == handler._sha256_hex(data)
    assert rec["ontap_calls"] == 4 and rec["file_size"] == len(data)
