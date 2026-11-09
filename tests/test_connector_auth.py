"""AroFlo HMAC-SHA512 request signing."""
import hashlib
import hmac

from aroflo_connector import AroFloConnector


def test_signature_matches_postman_payload_order():
    conn = AroFloConnector("org", "user", "pass", "secret")
    headers = conn._generate_auth("zone=tasks&page=1")

    auth = "uencoded=user&pencoded=pass&orgEncoded=org"
    to_sign = "+".join(["GET", "", "text/json", auth, headers["afdatetimeutc"], "zone=tasks&page=1"])
    expected = hmac.new(b"secret", to_sign.encode(), hashlib.sha512).hexdigest()

    assert headers["Authorization"] == auth
    assert headers["Authentication"] == f"HMAC {expected}"
    assert "HostIP" not in headers


def test_host_ip_is_signed_and_sent_when_set():
    conn = AroFloConnector("org", "user", "pass", "secret", host_ip="203.0.113.5")
    headers = conn._generate_auth("zone=tasks")
    assert headers["HostIP"] == "203.0.113.5"


def test_credentials_are_url_encoded():
    conn = AroFloConnector("o+g", "u/s", "p=s", "secret")
    headers = conn._generate_auth("zone=tasks")
    assert headers["Authorization"] == "uencoded=u%2Fs&pencoded=p%3Ds&orgEncoded=o%2Bg"
