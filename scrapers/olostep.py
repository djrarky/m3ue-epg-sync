"""
scrapers.olostep - Olostep web scraping API backend.

API docs: https://olostep.com
Set scraping_api: "olostep" and scraping_api_key to your Olostep Bearer token.
"""
from __future__ import annotations

try:
    import requests
except ImportError:
    import sys
    sys.exit("Missing dependency: pip install requests")


def fetch_url(api_key: str, url: str, fmt: str = "text", timeout: int = 60) -> str:
    """Fetch a URL via the Olostep scraping API."""
    resp = requests.post(
        "https://api.olostep.com/v1/scrapes",
        json={"url_to_scrape": url, "formats": [fmt]},
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        timeout=timeout,
    )
    resp.raise_for_status()
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError(f"Olostep returned unexpected response type: {type(data).__name__}")
    result = data.get("result", {})
    if not isinstance(result, dict):
        raise RuntimeError(f"Olostep 'result' is not a dict: {type(result).__name__}")
    return result.get(f"{fmt}_content") or ""
