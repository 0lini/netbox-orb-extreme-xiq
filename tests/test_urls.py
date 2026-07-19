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
    ],
)
def test_require_https_url_accepts_https_hosts(url):
    assert require_https_url(url, what="TEST_URL").startswith("https://")
    assert not require_https_url(url, what="TEST_URL").endswith("/")


@pytest.mark.parametrize(
    "url",
    [
        "http://localhost",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
        "http://[::1]:8000",
        "http://netbox.local",
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
        "ftp://netbox.example.com",
        "https://",
        "not-a-url",
    ],
)
def test_require_https_url_rejects_non_https_or_hostless(url):
    with pytest.raises(ValueError, match="https://"):
        require_https_url(url, what="TEST_URL")
