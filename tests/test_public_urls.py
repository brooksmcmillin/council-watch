import pytest

from council_meetings.config import resolve_public_url


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        (None, ""),
        ("", ""),
        ("/doc", "https://city.example/doc"),
        ("relative/doc", "https://city.example/relative/doc"),
        ("//video.example/watch", "https://video.example/watch"),
        ("HTTPS://VIDEO.EXAMPLE/watch", "https://video.example/watch"),
        ("https://other.example/doc", "https://other.example/doc"),
        ("/javascript:alert(1)", "https://city.example/javascript:alert(1)"),
        ("javascript:alert(1)", ""),
        (" JaVaScRiPt:alert(1)", ""),
        ("java\tscript:alert(1)", ""),
        ("data:text/html,<svg/onload=alert(1)>", ""),
        ("vbscript:msgbox(1)", ""),
        ("file:///etc/passwd", ""),
        ("https://[", ""),
        ("https://user:password@city.example/doc", ""),  # pragma: allowlist secret
    ],
)
def test_public_url_boundary_preserves_ordinary_links(source: str | None, expected: str) -> None:
    assert resolve_public_url("https://city.example", source) == expected
