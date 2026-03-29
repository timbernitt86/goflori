from __future__ import annotations

from app.services.project_state_engine import _humanize_healthcheck_failure


def test_humanize_tls_hostname_mismatch_error():
    raw = """HTTPSConnectionPool(host='www.goflori.de', port=443): Max retries exceeded with url: /
    (Caused by SSLError(SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed:
    Hostname mismatch, certificate is not valid for 'www.goflori.de'. (_ssl.c:1032)")))"""

    message, code = _humanize_healthcheck_failure(raw, "https://www.goflori.de")

    assert code == "tls_hostname_mismatch"
    assert "Hostname-Mismatch" in message
    assert "Zertifikat" in message


def test_humanize_timeout_error():
    message, code = _humanize_healthcheck_failure(
        "HTTPSConnectionPool(host='x', port=443): Read timed out.",
        "https://example.com",
    )

    assert code == "timeout"
    assert "Timeout" in message
