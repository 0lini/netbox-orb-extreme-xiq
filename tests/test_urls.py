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
        "",
        "http://netbox.example.com",
        "ftp://netbox.example.com",
        "https://",
        "not-a-url",
    ],
)
def test_require_https_url_rejects_non_https_or_hostless(url):
    with pytest.raises(ValueError, match="https://"):
        require_https_url(url, what="TEST_URL")
