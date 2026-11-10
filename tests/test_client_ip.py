from ipaddress import ip_network
from pathlib import Path

import pytest
from fastapi import Request
from pydantic import ValidationError

from council_meetings.config import Settings, settings
from council_meetings.main import _client_ip


@pytest.mark.parametrize(
    ("peer", "visitor", "expected"),
    [
        ("192.0.2.1", "203.0.113.10", "203.0.113.10"),
        ("192.0.2.1", " 2001:0db8:0:0::1 ", "2001:db8::1"),
        ("2001:db8:1::1", "203.0.113.11", "203.0.113.11"),
        ("192.0.2.2", "203.0.113.10", "192.0.2.2"),
        ("192.0.2.1", "bad", "192.0.2.1"),
        ("192.0.2.1", "", "192.0.2.1"),
        ("192.0.2.1", "203.0.113.10, 203.0.113.11", "192.0.2.1"),
        ("192.0.2.1", None, "192.0.2.1"),
        ("2001:0db8:2:0::1", None, "2001:db8:2::1"),
        ("not-an-ip", "203.0.113.10", "not-an-ip"),
        (None, "203.0.113.10", "unknown"),
    ],
)
def test_client_ip_trust_and_fallback(
    monkeypatch: pytest.MonkeyPatch, peer: str | None, visitor: str | None, expected: str
) -> None:
    monkeypatch.setattr(
        settings,
        "trusted_proxy_networks",
        (ip_network("192.0.2.1/32"), ip_network("2001:db8:1::/64")),
    )
    headers = [(b"cf-connecting-ip", visitor.encode())] if visitor is not None else []
    request = Request(
        {"type": "http", "client": (peer, 1234) if peer else None, "headers": headers}
    )
    assert _client_ip(request) == expected


def test_trusted_proxy_networks_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TRUSTED_PROXY_NETWORKS", raising=False)
    assert Settings(_env_file=None).trusted_proxy_networks == ()
    monkeypatch.setenv("TRUSTED_PROXY_NETWORKS", '["192.0.2.1/32", "2001:db8:1::/64"]')
    assert Settings(_env_file=None).trusted_proxy_networks == (
        ip_network("192.0.2.1/32"),
        ip_network("2001:db8:1::/64"),
    )
    monkeypatch.setenv("TRUSTED_PROXY_NETWORKS", '["not-a-network"]')
    with pytest.raises(ValidationError):
        Settings(_env_file=None)


def test_docker_preserves_socket_peer() -> None:
    dockerfile = Path(__file__).resolve().parents[1] / "Dockerfile"
    assert "--no-proxy-headers" in dockerfile.read_text()
