"""
SSRF guard for outbound requests whose URL traces back to crawled page content
(an <img src>, a redirect Location header) rather than a literal constant —
CodeQL flags these as partial/full SSRF since a malicious/compromised page
could point one at an internal address.

Not airtight DNS-rebinding defense: the IP is checked here, not re-checked at
the moment `requests` actually connects a beat later. Sufficient for the
CodeQL-taint-shaped risk this addresses.
"""
import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    pass


def assert_safe_url(url):
    """Raise UnsafeURLError if url isn't safe to fetch server-side: wrong scheme,
    or resolves to a private/loopback/link-local/reserved address (blocks SSRF
    pivots to internal services or cloud metadata endpoints like 169.254.169.254)."""
    parsed = urlparse(url)
    if parsed.scheme not in ('http', 'https'):
        raise UnsafeURLError(f'refusing to fetch non-http(s) URL: {url!r}')
    if not parsed.hostname:
        raise UnsafeURLError(f'refusing to fetch URL with no host: {url!r}')
    try:
        addr = socket.gethostbyname(parsed.hostname)
        ip = ipaddress.ip_address(addr)
    except (socket.gaierror, ValueError) as e:
        raise UnsafeURLError(f'could not resolve host {parsed.hostname!r}: {e}')
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast:
        raise UnsafeURLError(f'refusing to fetch URL resolving to non-public address: {url!r} -> {ip}')
