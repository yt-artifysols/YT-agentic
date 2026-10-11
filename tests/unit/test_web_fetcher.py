"""The research web fetcher: no SSRF, including DNS rebinding and redirect tricks."""

from __future__ import annotations

import socket
from typing import Any

import pytest

from ytcore.agents import web
from ytcore.agents.web import FetchRefused, HttpWebFetcher, resolve_public

PUBLIC = "93.184.216.34"


def _addr(ip: str) -> list[Any]:
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 443))]


@pytest.mark.parametrize(
    "ip", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254", "::1", "100.64.0.1"]
)
def test_non_public_addresses_are_refused(monkeypatch: pytest.MonkeyPatch, ip: str) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addr(ip))
    with pytest.raises(FetchRefused, match="non-public"):
        resolve_public("example.test", 443)


def test_any_private_answer_among_several_refuses_the_host(monkeypatch: pytest.MonkeyPatch) -> None:
    both = _addr(PUBLIC) + _addr("10.0.0.9")
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: both)
    with pytest.raises(FetchRefused):
        resolve_public("example.test", 443)


def test_dns_rebinding_cannot_change_the_address_after_the_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The host answers with a public address first and a private one on any later lookup. The
    fetcher resolves ONCE per hop and connects to that exact address."""
    answers = [_addr(PUBLIC), _addr("169.254.169.254")]
    lookups: list[str] = []

    def getaddrinfo(host: str, *a: Any, **k: Any) -> list[Any]:
        lookups.append(host)
        return answers[min(len(lookups) - 1, 1)]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    connected: list[str] = []

    def fake_open(self: HttpWebFetcher, host: str, port: int, ip: str, path: str) -> Any:
        connected.append(ip)
        return 200, _Headers(), b"<html><title>T</title><p>hello</p></html>", "utf-8"

    monkeypatch.setattr(HttpWebFetcher, "_open", fake_open)
    page = HttpWebFetcher().fetch("https://example.test/a")
    assert connected == [PUBLIC] and len(lookups) == 1
    assert page.title == "T" and "hello" in page.text


class _Headers(dict[str, str]):
    def get_content_charset(self) -> str | None:
        return None


def test_a_redirect_to_a_private_address_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    def getaddrinfo(host: str, *a: Any, **k: Any) -> list[Any]:
        return _addr("10.1.2.3" if host == "internal.test" else PUBLIC)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    hops: list[str] = []

    def fake_open(self: HttpWebFetcher, host: str, port: int, ip: str, path: str) -> Any:
        hops.append(host)
        return 302, _Headers({"Location": "https://internal.test/admin"}), b"", "utf-8"

    monkeypatch.setattr(HttpWebFetcher, "_open", fake_open)
    with pytest.raises(FetchRefused, match="non-public"):
        HttpWebFetcher().fetch("https://example.test/start")
    assert hops == ["example.test"]  # never connected to the internal host


@pytest.mark.parametrize("url", ["http://example.test/", "ftp://example.test/", "https:///x"])
def test_only_https_urls_are_fetched(url: str) -> None:
    with pytest.raises(FetchRefused):
        HttpWebFetcher().fetch(url)


def test_redirect_loops_are_cut_off(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket, "getaddrinfo", lambda *a, **k: _addr(PUBLIC))
    monkeypatch.setattr(
        HttpWebFetcher,
        "_open",
        lambda self, h, p, i, path: (302, _Headers({"Location": "/again"}), b"", "utf-8"),
    )
    with pytest.raises(FetchRefused, match="too many redirects"):
        HttpWebFetcher().fetch("https://example.test/")


def test_the_total_download_time_is_bounded() -> None:
    assert web.TOTAL_SECONDS <= 30 and web.MAX_BYTES <= 2_000_000
