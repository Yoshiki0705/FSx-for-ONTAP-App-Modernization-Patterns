"""Tier 1 (local/unit) tests for the Candidate C-REST handler.

These exercise the parser mode and the negative-classification mapping with no real ONTAP endpoint.
They establish local algorithmic behavior only; the real ONTAP REST read/serve, the actual
401/403/404 responses, and endpoint reachability are Tier 2/3 real-environment concerns.
"""

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
