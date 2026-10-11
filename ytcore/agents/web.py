"""Read-only web fetcher for the research pool (stdlib only).

GET over https only, no cookies or credentials, bounded size and time, redirects only to https,
and private, loopback and link-local addresses are refused (no SSRF into the platform). The
page text is untrusted data: it goes to the model only inside data blocks.
"""

from __future__ import annotations

import ipaddress
import re
import socket
import urllib.request
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urlparse

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


def _public_https(url: str) -> None:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("only https URLs can be fetched")
    for info in socket.getaddrinfo(parsed.hostname, parsed.port or 443):
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ValueError("refusing to fetch a non-public address")


class _HttpsOnlyRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any,
                         newurl: str) -> Any:  # fmt: skip
        _public_https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class HttpWebFetcher:
    def __init__(self, *, timeout: float = 10.0, user_agent: str = "yt-agentic-research/1") -> None:
        self._timeout, self._ua = timeout, user_agent
        self._opener = urllib.request.build_opener(_HttpsOnlyRedirects())

    def fetch(self, url: str) -> FetchedPage:
        _public_https(url)
        headers = {"User-Agent": self._ua}
        req = urllib.request.Request(url, method="GET", headers=headers)  # noqa: S310 - https only
        with self._opener.open(req, timeout=self._timeout) as resp:
            raw = resp.read(MAX_BYTES + 1)[:MAX_BYTES]
            charset = resp.headers.get_content_charset() or "utf-8"
        page = raw.decode(charset, errors="replace")
        title, text = extract_text(page)
        return FetchedPage(url, title[:300] or url, text, file_digest(raw))
