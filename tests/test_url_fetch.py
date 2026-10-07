import pytest

from build_a_hia.services.inspection import SourceError
from build_a_hia.services.url_fetch import (
    HttpResponse,
    SafeFetcher,
    UrlTarget,
    is_public_address,
    parse_url,
)


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",
        "10.0.0.5",
        "172.16.0.1",
        "192.168.1.1",
        "169.254.169.254",
        "100.64.0.1",
        "0.0.0.0",  # noqa: S104 - must be blocked, not bound
        "::1",
        "fe80::1",
        "fd00::1",
        "::ffff:127.0.0.1",
        "168.63.129.16",
        "224.0.0.1",
    ],
)
def test_private_and_infrastructure_addresses_are_blocked(address):
    assert not is_public_address(address)


@pytest.mark.parametrize("address", ["8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_are_allowed(address):
    assert is_public_address(address)


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("ftp://example.org/file.pdf", "invalid_url"),
        ("https://user:pass@example.org/", "invalid_url"),
        ("https://example.org:8443/", "invalid_url"),
        ("http://localhost/", "blocked_address"),
        ("http://127.0.0.1/", "blocked_address"),
        ("http://[::1]/", "blocked_address"),
        ("https:///path", "invalid_url"),
        ("https://" + "a" * 2050 + ".org", "invalid_url"),
    ],
)
def test_unsafe_urls_are_rejected(url, code):
    with pytest.raises(SourceError) as error:
        parse_url(url)

    assert error.value.code == code


def test_url_is_normalized():
    target = parse_url("  HTTPS://Example.ORG/a b?x=1#frag ")

    assert target.url == "https://example.org/a b?x=1"
    assert target.host == "example.org"
    assert target.port == 443


def test_international_domain_is_encoded():
    assert parse_url("https://bücher.example/").host == "xn--bcher-kva.example"


class FakeWeb:
    def __init__(self, routes, addresses=None):
        self.routes = routes
        self.addresses = addresses or {}
        self.requests: list[tuple[str, str]] = []

    def resolve(self, host, _port):
        return self.addresses.get(host, ["93.184.216.34"])

    def connect(self, target: UrlTarget, address: str, _timeout: float) -> HttpResponse:
        self.requests.append((target.url, address))
        status, headers, body = self.routes[target.url]
        return HttpResponse(status=status, headers=headers, body=iter(body), close=lambda: None)


def _fetcher(web, max_bytes=1000, max_redirects=2):
    return SafeFetcher(
        max_bytes=max_bytes,
        max_redirects=max_redirects,
        timeout_seconds=5,
        resolver=web.resolve,
        connector=web.connect,
    )


def test_fetch_follows_validated_redirects():
    web = FakeWeb(
        {
            "https://example.org/": (302, {"location": "/en/help"}, []),
            "https://example.org/en/help": (
                200,
                {"content-type": "text/html", "last-modified": "Wed, 01 Oct 2026 10:00:00 GMT"},
                [b"<p>", b"Help</p>"],
            ),
        }
    )

    result = _fetcher(web).fetch("https://example.org/")

    assert result.final_url == "https://example.org/en/help"
    assert result.data == b"<p>Help</p>"
    assert result.last_modified == "2026-10-01"
    assert all(address == "93.184.216.34" for _url, address in web.requests)


def test_redirect_to_private_network_is_blocked():
    web = FakeWeb(
        {"https://example.org/": (302, {"location": "http://internal.example/"}, [])},
        addresses={"internal.example": ["10.1.2.3"]},
    )

    with pytest.raises(SourceError) as error:
        _fetcher(web).fetch("https://example.org/")

    assert error.value.code == "blocked_address"
    assert len(web.requests) == 1


def test_any_private_dns_answer_blocks_the_host():
    web = FakeWeb({}, addresses={"mixed.example": ["93.184.216.34", "127.0.0.1"]})

    with pytest.raises(SourceError) as error:
        _fetcher(web).fetch("https://mixed.example/")

    assert error.value.code == "blocked_address"
    assert web.requests == []


def test_redirect_loops_are_bounded():
    web = FakeWeb({"https://example.org/": (301, {"location": "https://example.org/"}, [])})

    with pytest.raises(SourceError) as error:
        _fetcher(web).fetch("https://example.org/")

    assert error.value.code == "too_many_redirects"
    assert len(web.requests) == 3


def test_declared_size_limit_is_enforced():
    web = FakeWeb({"https://example.org/": (200, {"content-length": "5000"}, [b"x"])})

    with pytest.raises(SourceError) as error:
        _fetcher(web).fetch("https://example.org/")

    assert error.value.code == "too_large"


def test_streamed_size_limit_is_enforced():
    web = FakeWeb({"https://example.org/": (200, {}, [b"x" * 600, b"x" * 600])})

    with pytest.raises(SourceError) as error:
        _fetcher(web).fetch("https://example.org/")

    assert error.value.code == "too_large"


@pytest.mark.parametrize(
    ("status", "code"),
    [
        (403, "access_denied"),
        (401, "access_denied"),
        (429, "rate_limited"),
        (404, "not_found"),
        (500, "http_error"),
    ],
)
def test_http_errors_are_reported(status, code):
    web = FakeWeb({"https://example.org/": (status, {}, [])})

    with pytest.raises(SourceError) as error:
        _fetcher(web).fetch("https://example.org/")

    assert error.value.code == code


def test_connection_failures_are_reported():
    def refuse(_target, _address, _timeout):
        raise ConnectionRefusedError

    fetcher = SafeFetcher(
        max_bytes=10,
        max_redirects=1,
        timeout_seconds=1,
        resolver=lambda _host, _port: ["93.184.216.34"],
        connector=refuse,
    )

    with pytest.raises(SourceError) as error:
        fetcher.fetch("https://example.org/")

    assert error.value.code == "unreachable"
