"""
scrapers - Pluggable web scraping API backends.

To add a new scraper, create a Python file in this directory that implements:

    def fetch_url(api_key: str, url: str, fmt: str = "text", timeout: int = 60) -> str:
        '''Fetch a URL and return its content as a string.'''
        ...

The scraper is selected by the per-playlist config key `scraping_api`.
The file name (without .py) becomes the scraper name.

Parameters:
    api_key  - authentication credential (from config key `scraping_api_key`)
    url      - the URL to fetch
    fmt      - desired content format: "text" (plain text) or "html" (raw HTML)
    timeout  - request timeout in seconds

Must return the page content as a string. Raise on failure -
fetch_cached() catches exceptions, warns in the CLI, and falls back to
stale cache if available.
Caching is handled automatically - scrapers just fetch.
"""
from __future__ import annotations

import hashlib
import importlib
import logging
import time
from pathlib import Path

import requests
from typing import Any

logger = logging.getLogger(__name__)


# ── Scraper discovery ────────────────────────────────────────────────────────

_SCRAPERS_DIR = Path(__file__).parent


def available_scrapers() -> list[str]:
    """List available scraper names (auto-discovered from this directory)."""
    return sorted(
        p.stem for p in _SCRAPERS_DIR.glob("*.py")
        if p.stem != "__init__" and not p.stem.startswith("_")
    )


def get_scraper(name: str) -> Any:
    """Import and return a scraper module by name."""
    names = available_scrapers()
    if name not in names:
        raise ValueError(f"Unknown scraper '{name}'. Available: {', '.join(names)}")
    return importlib.import_module(f"scrapers.{name}")


# ── Shared caching logic ─────────────────────────────────────────────────────

def cache_file_path(cache_dir: Path, url: str, fmt: str) -> Path:
    """Deterministic cache path for a URL + format combination."""
    digest = hashlib.sha256(f"{fmt}:{url}".encode("utf-8")).hexdigest()[:16]
    return cache_dir / f"{fmt}_{digest}.cache"


def fetch_cached(
    scraper_name: str,
    api_key: str,
    url: str,
    fmt: str,
    cache_dir: Path,
    cache_days: int,
    force_refresh: bool = False,
    timeout: int = 60,
    max_retries: int = 3,
) -> tuple[str, bool, Path]:
    """Fetch via the configured scraper with on-disk cache and retry.

    Retries up to max_retries with exponential backoff. Falls back to
    stale cache if all retries fail and a cache file exists.

    Returns: (content, from_cache, cache_path)
    """
    scraper = get_scraper(scraper_name)
    cache_dir.mkdir(parents=True, exist_ok=True)
    cp = cache_file_path(cache_dir, url, fmt)

    if not force_refresh and cp.exists():
        age_days = (time.time() - cp.stat().st_mtime) / 86400
        if age_days < cache_days:
            return cp.read_text(encoding="utf-8"), True, cp

    last_exc = None
    for attempt in range(1, max_retries + 1):
        try:
            content = scraper.fetch_url(api_key, url, fmt=fmt, timeout=timeout)
            if not content or len(content) < 1000:
                raise RuntimeError(
                    f"Scraper returned suspiciously short content "
                    f"({len(content) if content else 0} chars)"
                )
            cp.write_text(content, encoding="utf-8")
            return content, False, cp
        except requests.HTTPError as exc:
            if exc.response is not None and 400 <= exc.response.status_code < 500:
                raise  # permanent client error, don't retry
            last_exc = exc
            if attempt < max_retries:
                logger.warning("Scraper attempt %d failed: %s", attempt, exc)
                time.sleep(min(2 ** attempt, 10))
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                logger.warning("Scraper attempt %d failed: %s", attempt, exc)
                time.sleep(min(2 ** attempt, 10))

    # All retries exhausted - fall back to stale cache if available
    if cp.exists():
        logger.warning("Scraper failed after %d attempts (%s) - using stale cache: %s",
                        max_retries, last_exc, cp.name)
        return cp.read_text(encoding="utf-8"), True, cp
    raise RuntimeError(f"Scraper failed after {max_retries} attempts: {last_exc}") from last_exc
