"""Public-address-only article reads, pinned DNS, bounded redirects/body."""
import http.client
import ipaddress
import socket
from urllib.parse import urlsplit, urljoin


def read_html(url, *, timeout=8, max_bytes=1000000, resolver=socket.getaddrinfo):
    for _ in range(4):
        parsed = urlsplit(url)
        if parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username or parsed.password:
            raise ValueError('article_url_not_public')
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
        if port not in (80, 443):
            raise ValueError('article_port_not_allowed')
        addresses = {r[4][0] for r in resolver(parsed.hostname, port, type=socket.SOCK_STREAM)}
        if not addresses or any(not ipaddress.ip_address(a).is_global for a in addresses):
            raise ValueError('article_dns_not_public')
        address = sorted(addresses)[0]
        cls = http.client.HTTPSConnection if parsed.scheme == 'https' else http.client.HTTPConnection
        conn = cls(parsed.hostname, port, timeout=timeout)
        # Keep hostname for Host and TLS verification, pin the actual TCP peer.
        conn._create_connection = lambda endpoint, timeout, *args: socket.create_connection((address, port), timeout)
        try:
            conn.request('GET', (parsed.path or '/') + ('?' + parsed.query if parsed.query else ''),
                         headers={'User-Agent':'politics-narrative/1.0','Accept-Encoding':'identity'})
            response = conn.getresponse()
            if response.status in (301,302,303,307,308):
                target = response.getheader('Location')
                if not target:
                    raise ValueError('article_redirect_missing')
                url = urljoin(url, target)
                continue
            if response.status != 200 or 'html' not in (response.getheader('Content-Type') or '').lower():
                raise ValueError('article_response_not_html')
            if response.getheader('Content-Encoding', 'identity') != 'identity':
                raise ValueError('article_encoding_not_allowed')
            size = response.getheader('Content-Length')
            if size and int(size) > max_bytes:
                raise ValueError('article_body_too_large')
            data = response.read(max_bytes + 1)
            if len(data) > max_bytes:
                raise ValueError('article_body_too_large')
            return data.decode(response.headers.get_content_charset() or 'utf-8', errors='replace')
        finally:
            conn.close()
    raise ValueError('article_redirect_limit')
