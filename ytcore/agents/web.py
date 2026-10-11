"""Read-only web fetcher for the research pool (stdlib only).

GET over https only, no cookies or credentials, bounded size and time, redirects only to https,
and private, loopback and link-local addresses are refused (no SSRF into the platform). The
page text is untrusted data: it goes to the model only inside data blocks.
"""

from __future__ import annotations

import http.client
import ipaddress
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlparse

from ytcore.agents.providers import FetchedPage
from ytcore.approvals.hashing import file_digest

MAX_BYTES = 2_000_000


class _TextExtractor(HTMLParser):
    """Linear-time visible-text and title extraction (no backtracking regexes on hostile HTML)."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self._skip = 0
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: Any) -> None:
        if tag in {"script", "style"}:
            self._skip += 1
        elif tag == "title":
            self._in_title = True

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self._skip:
            self._skip -= 1
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        if not self._skip:
            self.parts.append(data)


def extract_text(page: str) -> tuple[str, str]:
    parser = _TextExtractor()
    parser.feed(page)
    parser.close()
    text = re.sub(r"\s+", " ", " ".join(parser.parts)).strip()
    return " ".join("".join(parser.title).split()), text


MAX_REDIRECTS = 5
TOTAL_SECONDS = 20.0  # the whole fetch, however slowly the server drips bytes


class FetchRefused(ValueError):
    pass


def resolve_public(host: str, port: int) -> str:
    """Resolve once and return a PUBLIC address. The caller connects to exactly this address (a
    second lookup could return a private one: DNS rebinding)."""
    addresses = []
    for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise FetchRefused("refusing to fetch a non-public address")
        addresses.append(str(ip))
    if not addresses:
        raise FetchRefused("host did not resolve")
    return addresses[0]


def _parse(url: str) -> tuple[str, int, str]:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise FetchRefused("only https URLs can be fetched")
    path = (parsed.path or "/") + (f"?{parsed.query}" if parsed.query else "")
    return parsed.hostname, parsed.port or 443, path


class _PinnedHttpsConnection(http.client.HTTPSConnection):
    """Connects to a pre-resolved address, but verifies the certificate for the hostname."""

    def __init__(self, host: str, port: int, ip: str, timeout: float) -> None:
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self) -> None:
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)  # type: ignore[attr-defined]


class HttpWebFetcher:
    def __init__(self, *, timeout: float = 10.0, user_agent: str = "yt-agentic-research/1") -> None:
        self._timeout, self._ua = timeout, user_agent

    def _open(self, host: str, port: int, ip: str, path: str) -> tuple[int, Any, bytes, str]:
        """One GET against the pinned address: (status, headers, body, charset)."""
        deadline = time.monotonic() + TOTAL_SECONDS
        conn = _PinnedHttpsConnection(host, port, ip, self._timeout)
        try:
            conn.request(
                "GET", path, headers={"User-Agent": self._ua, "Accept-Encoding": "identity"}
            )
            resp = conn.getresponse()
            body = b""
            while len(body) <= MAX_BYTES:
                if time.monotonic() > deadline:
                    raise FetchRefused("fetch took too long")
                chunk = resp.read(65536)
                if not chunk:
                    break
                body += chunk
            return (
                resp.status,
                resp.headers,
                body[:MAX_BYTES],
                resp.headers.get_content_charset() or "utf-8",
            )
        finally:
            conn.close()

    def fetch(self, url: str) -> FetchedPage:
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            host, port, path = _parse(current)
            ip = resolve_public(host, port)
            status, headers, raw, charset = self._open(host, port, ip, path)
            if status in (301, 302, 303, 307, 308):
                location = headers.get("Location")
                if not location:
                    raise FetchRefused("redirect without a location")
                current = urljoin(current, location)
                continue
            if status != 200:
                raise FetchRefused(f"unexpected status {status}")
            title, text = extract_text(raw.decode(charset, errors="replace"))
            return FetchedPage(url, title[:300] or url, text, file_digest(raw))
        raise FetchRefused("too many redirects")
