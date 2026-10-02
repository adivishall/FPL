"""Webhook SSRF policy (§75): user-supplied delivery URLs are tightly constrained."""

from __future__ import annotations

import httpx
import pytest

from fpl_notifications.rules import Alert
from fpl_notifications.store import UnsafeUrl, WebhookChannel, validate_webhook_url

HOSTS = ["hooks.example.com"]


def public(_h: str, _p: int) -> list[str]:
    return ["93.184.216.34"]


@pytest.mark.parametrize(
    ("url", "why"),
    [
        ("http://hooks.example.com/x", "https"),
        ("https://user:pw@hooks.example.com/x", "credentials"),
        ("https://evil.example.net/x", "allow-list"),
        ("https://hooks.example.com:8443/x", "port"),
        ("https://hooks.example.com.evil.net/x", "allow-list"),
    ],
)
def test_rejects_unsafe_urls(url: str, why: str) -> None:
    with pytest.raises(UnsafeUrl, match=why):
        validate_webhook_url(url, HOSTS, public)


@pytest.mark.parametrize("addr", ["127.0.0.1", "10.0.0.5", "169.254.169.254", "::1", "fd00::1"])
def test_rejects_allow_listed_host_resolving_to_private_address(addr: str) -> None:
    with pytest.raises(UnsafeUrl, match="non-public"):
        validate_webhook_url("https://hooks.example.com/x", HOSTS, lambda h, p: [addr])


def test_accepts_and_posts_without_redirects() -> None:
    seen: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(302, headers={"location": "http://169.254.169.254/"})

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    ch = WebhookChannel("https://HOOKS.example.com/x", HOSTS, client=client, resolver=public)
    a = Alert("deadline", "info", "t", "b", 1.0, "k")
    assert ch.send("m1", a) is False  # a redirect is not success and is never followed
    assert len(seen) == 1 and seen[0].url.host == "hooks.example.com"
