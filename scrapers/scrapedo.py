"""
scrapers.scrapedo - scrape.do web scraping API backend.

API docs: https://scrape.do
Set scraping_api: "scrapedo" and scraping_api_key to your scrape.do token.
"""
from __future__ import annotations

import urllib.parse

try:
    import requests
except ImportError:
    import sys
    sys.exit("Missing dependency: pip install requests")


def fetch_url(api_key: str, url: str, fmt: str = "text", timeout: int = 60) -> str:
    """Fetch a URL via the scrape.do scraping API."""
    params = {
        "token": api_key,
        "url": url,
        "super": "true",
        "geoCode": "GB",
        "render": "true",
        "blockResources": "false",
    }
    resp = requests.get(
        "https://api.scrape.do/",
        params=params,
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.text
