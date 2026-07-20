"""HTTPS URL helper tests."""

from __future__ import annotations

import pytest

from orb_extreme_platformone.urls import require_https_url


@pytest.mark.parametrize(
    "url",
    [
        "https://cloudapi.extremecloudiq.com",
        "https://cloudapi.extremecloudiq.com/",
        " https://netbox.example.com/ ",
        "https://netbox.example.com/netbox",
        "https://netbox.example.com:443",
    ],
)
def test_require_https_url_accepts_https_hosts(url):
    cleaned = require_https_url(url, what="TEST_URL")
    assert cleaned.startswith("https://")
    assert not cleaned.endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "http://netbox.local",
        "http://netbox:8080",
        " http://localhost:8000/ ",
    ],
)
def test_require_https_url_accepts_http_for_local_dev_hosts(url):
    cleaned = require_https_url(url, what="TEST_URL")
    assert cleaned.startswith("http://")
    assert not cleaned.endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://netbox.example.com",
        "http://evil.example.com",
        "http://metadata",
        "http://kubernetes",
        "ftp://netbox.example.com",
        "https://",
        "not-a-url",
        "https://cloudapi.extremecloudiq.com@evil.com",
        "https://user:pass@cloudapi.extremecloudiq.com",
        "http://user@localhost:8000",
        "https://netbox.example.com?x=1",
        "https://netbox.example.com#frag",
    ],
)
def test_require_https_url_rejects_non_https_userinfo_or_hostless(url):
    with pytest.raises(ValueError):
        require_https_url(url, what="TEST_URL")
