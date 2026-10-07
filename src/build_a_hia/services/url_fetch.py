"""Fetch a single user-submitted URL without exposing private or infrastructure networks.

Every hop is validated: the host is resolved once, all resolved addresses must be public,
and the connection goes to the validated address so DNS cannot be rebound in between.
"""

import ipaddress
import socket
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from email.utils import parsedate_to_datetime
from urllib.parse import urljoin, urlsplit

from .inspection import SourceError

ALLOWED_SCHEMES = {"http": 80, "https": 443}
MAX_URL_LENGTH = 2048
REDIRECT_STATUSES = {301, 302, 303, 307, 308}
USER_AGENT = "build-a-hia/0.1 (Red Cross Helpful Information App source fetcher)"
ACCEPT = (
    "text/html,application/xhtml+xml,application/pdf,"
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document,"
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet,"
    "image/*;q=0.8,*/*;q=0.1"
)
# Public address used by the Azure platform for host services.
_BLOCKED_ADDRESSES = {ipaddress.ip_address("168.63.129.16")}


@dataclass(frozen=True)
class UrlTarget:
    """A validated, normalized http(s) address split into its connection parts.

    Attributes:
        url: Normalized address without credentials or fragment.
        scheme: ``http`` or ``https``.
        host: Lower-case IDNA host name, or an IP literal without brackets.
        port: 80 or 443.
        path: Path including the query string.
    """

    url: str
    scheme: str
    host: str
    port: int
    path: str

    @property
    def host_header(self) -> str:
        """Host header value; includes the port only when it is not the scheme's default."""
        host = f"[{self.host}]" if ":" in self.host else self.host
        if self.port == ALLOWED_SCHEMES[self.scheme]:
            return host
        return f"{host}:{self.port}"


@dataclass
class HttpResponse:
    """Streaming HTTP response returned by a connector.

    Attributes:
        status: HTTP status code.
        headers: Response headers with lower-case names.
        body: Decoded body in chunks.
        close: Releases the connection; must be called once the response is handled.
    """

    status: int
    headers: Mapping[str, str]
    body: Iterator[bytes]
    close: Callable[[], None]


@dataclass(frozen=True)
class FetchResult:
    """A completed download.

    Attributes:
        final_url: Normalized address after redirects.
        content_type: Content-Type header value, or empty.
        data: Downloaded body.
        last_modified: ISO date from the Last-Modified header, or empty.
    """

    final_url: str
    content_type: str
    data: bytes
    last_modified: str


Resolver = Callable[[str, int], list[str]]
Connector = Callable[[UrlTarget, str, float], HttpResponse]


def _invalid(message: str) -> SourceError:
    return SourceError("invalid_url", message)


def parse_url(url: str) -> UrlTarget:
    """Validate and normalize a user-submitted web address without resolving it.

    Only http and https on ports 80 and 443 are allowed, without credentials. Host names are
    IDNA-encoded; ``localhost`` and non-public IP literals are rejected. Fragments are dropped.

    Args:
        url: Address entered by the user or taken from a redirect.

    Returns:
        The validated target.

    Raises:
        SourceError: ``invalid_url`` for a malformed or disallowed address, or
            ``blocked_address`` for ``localhost`` or a non-public IP literal.
    """
    url = url.strip()
    if not url or len(url) > MAX_URL_LENGTH:
        raise _invalid("Enter a web address of at most 2048 characters.")
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as error:
        raise _invalid("This web address is not valid.") from error
    scheme = parts.scheme.lower()
    if scheme not in ALLOWED_SCHEMES:
        raise _invalid("Only http and https web addresses are supported.")
    if parts.username or parts.password:
        raise _invalid("Web addresses with a user name or password are not allowed.")
    host = (parts.hostname or "").rstrip(".")
    if not host:
        raise _invalid("This web address has no host name.")
    port = port or ALLOWED_SCHEMES[scheme]
    if port not in ALLOWED_SCHEMES.values():
        raise _invalid("Only the standard web ports 80 and 443 are allowed.")

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is None:
        try:
            host = host.encode("idna").decode("ascii").lower()
        except UnicodeError as error:
            raise _invalid("This web address has an invalid host name.") from error
        if host == "localhost" or host.endswith(".localhost"):
            raise SourceError("blocked_address", "Only public websites can be added.")
    elif not is_public_address(str(literal)):
        raise SourceError("blocked_address", "Only public websites can be added.")

    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    target = UrlTarget(url="", scheme=scheme, host=host, port=port, path=path)
    return UrlTarget(
        url=f"{scheme}://{target.host_header}{path}",
        scheme=scheme,
        host=host,
        port=port,
        path=path,
    )


def is_public_address(address: str) -> bool:
    """Return whether an IP address is globally routable and allowed to be contacted.

    IPv4-mapped IPv6 addresses are checked as IPv4. Multicast addresses, the Azure platform
    address and unparsable values are not public.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast and ip not in _BLOCKED_ADDRESSES


def resolve_host(host: str, port: int) -> list[str]:
    """Resolve a host name to its unique addresses, in resolver order.

    Raises:
        OSError: If the name cannot be resolved.
    """
    infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return list(dict.fromkeys(str(info[4][0]) for info in infos))


def urllib3_connector(target: UrlTarget, address: str, timeout: float) -> HttpResponse:
    """Send a GET request to an already validated address with urllib3.

    The connection goes to `address`, while the target host is used for the Host header,
    TLS SNI and certificate verification. Redirects are not followed and nothing is retried.

    Args:
        target: Validated target to request.
        address: Validated public IP address of the target host.
        timeout: Connect and read timeout in seconds.

    Returns:
        The streaming response; the caller must call its `close`.
    """
    import urllib3

    pool_timeout = urllib3.Timeout(connect=timeout, read=timeout)
    pool: urllib3.HTTPConnectionPool
    if target.scheme == "https":
        pool = urllib3.HTTPSConnectionPool(
            address,
            target.port,
            timeout=pool_timeout,
            retries=False,
            maxsize=1,
            cert_reqs="CERT_REQUIRED",
            server_hostname=target.host,
            assert_hostname=target.host,
        )
    else:
        pool = urllib3.HTTPConnectionPool(
            address, target.port, timeout=pool_timeout, retries=False, maxsize=1
        )
    try:
        response = pool.urlopen(
            "GET",
            target.path,
            headers={
                "Host": target.host_header,
                "User-Agent": USER_AGENT,
                "Accept": ACCEPT,
                "Accept-Encoding": "gzip, deflate",
            },
            redirect=False,
            assert_same_host=False,
            preload_content=False,
            decode_content=True,
        )
    except BaseException:
        pool.close()
        raise

    def close() -> None:
        response.release_conn()
        pool.close()

    return HttpResponse(
        status=response.status,
        headers={key.lower(): value for key, value in response.headers.items()},
        body=response.stream(64 * 1024, decode_content=True),
        close=close,
    )


class SafeFetcher:
    """Download web addresses with SSRF protection and size, redirect and time limits.

    Args:
        max_bytes: Maximum download size.
        max_redirects: Maximum number of redirects to follow.
        timeout_seconds: Connect and read timeout per request; the whole fetch must finish
            reading within three times this value.
        resolver: Resolves a host name and port to IP addresses.
        connector: Sends the request to a validated address.
        clock: Monotonic clock in seconds.
    """

    def __init__(
        self,
        *,
        max_bytes: int,
        max_redirects: int,
        timeout_seconds: float,
        resolver: Resolver = resolve_host,
        connector: Connector = urllib3_connector,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._max_bytes = max_bytes
        self._max_redirects = max_redirects
        self._timeout = timeout_seconds
        self._resolver = resolver
        self._connector = connector
        self._clock = clock

    def fetch(self, url: str) -> FetchResult:
        """Download a web address, following redirects and validating every hop.

        Each hop is parsed, resolved once and only contacted if all resolved addresses are
        public; the connection goes to the validated address.

        Args:
            url: Address to download.

        Returns:
            The downloaded content and response metadata.

        Raises:
            SourceError: ``invalid_url`` or ``blocked_address`` for a disallowed address or
                redirect target, ``unreachable``, ``timeout``, ``too_large``,
                ``too_many_redirects``, ``access_denied``, ``rate_limited``, ``not_found`` or
                ``http_error``.
        """
        current = url
        deadline = self._clock() + self._timeout * 3
        for _ in range(self._max_redirects + 1):
            target = parse_url(current)
            address = self._resolve_public(target)
            try:
                response = self._connector(target, address, self._timeout)
            except SourceError:
                raise
            except Exception as error:
                raise SourceError("unreachable", "The website could not be reached.") from error
            try:
                if response.status in REDIRECT_STATUSES:
                    location = response.headers.get("location")
                    if not location:
                        raise SourceError("http_error", "The website returned a broken redirect.")
                    current = urljoin(target.url, location)
                    continue
                _check_status(response.status)
                data = self._read(response, deadline)
            finally:
                response.close()
            return FetchResult(
                final_url=target.url,
                content_type=response.headers.get("content-type", ""),
                data=data,
                last_modified=_http_date(response.headers.get("last-modified", "")),
            )
        raise SourceError("too_many_redirects", "The web address redirects too many times.")

    def _resolve_public(self, target: UrlTarget) -> str:
        try:
            addresses = self._resolver(target.host, target.port)
        except OSError as error:
            raise SourceError("unreachable", "The website address could not be found.") from error
        if not addresses:
            raise SourceError("unreachable", "The website address could not be found.")
        if not all(is_public_address(address) for address in addresses):
            raise SourceError("blocked_address", "Only public websites can be added.")
        return addresses[0]

    def _read(self, response: HttpResponse, deadline: float) -> bytes:
        too_large = SourceError(
            "too_large",
            f"The download is larger than the {self._max_bytes // (1024 * 1024)} MB limit.",
        )
        declared = response.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > self._max_bytes:
            raise too_large
        chunks: list[bytes] = []
        total = 0
        try:
            for chunk in response.body:
                total += len(chunk)
                if total > self._max_bytes:
                    raise too_large
                if self._clock() > deadline:
                    raise SourceError("timeout", "The website took too long to respond.")
                chunks.append(chunk)
        except SourceError:
            raise
        except Exception as error:
            raise SourceError("unreachable", "The download was interrupted.") from error
        return b"".join(chunks)


def _check_status(status: int) -> None:
    if 200 <= status < 300:
        return
    if status in (401, 403, 407):
        raise SourceError(
            "access_denied",
            "This page needs a login or blocks automated access. "
            "Upload the document or a saved PDF instead.",
        )
    if status == 429:
        raise SourceError(
            "rate_limited",
            "The website is limiting requests. Try again later or upload the document instead.",
        )
    if status in (404, 410):
        raise SourceError("not_found", f"The page was not found (HTTP {status}).")
    raise SourceError("http_error", f"The website returned an error (HTTP {status}).")


def _http_date(value: str) -> str:
    if not value:
        return ""
    try:
        return parsedate_to_datetime(value).date().isoformat()
    except (TypeError, ValueError):
        return ""
