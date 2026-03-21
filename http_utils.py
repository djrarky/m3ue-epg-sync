"""
http_utils - Shared HTTP utilities for m3u-editor API calls.
"""
from __future__ import annotations

import logging
import time

import requests

logger = logging.getLogger(__name__)

# HTTP status codes that should not be retried - the request itself is wrong.
_NO_RETRY_STATUSES = {400, 401, 403, 404, 405, 409, 422}


def request_with_retry(
    method: str,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    json: dict | None = None,
    session: requests.Session | None = None,
    timeout: int = 20,
    max_retries: int = 3,
    context: str = "",
) -> requests.Response:
    """Make an HTTP request with exponential backoff retry.

    Client errors (400, 401, 403, 404, etc.) fail immediately - retrying
    won't help and just delays the error. Only transient failures (5xx,
    timeouts, connection errors) are retried.

    Raises RuntimeError after all retries are exhausted.
    """
    requester = session or requests
    for attempt in range(1, max_retries + 1):
        try:
            resp = requester.request(
                method, url,
                headers=headers, params=params, json=json,
                timeout=timeout,
            )
            resp.raise_for_status()
            return resp
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else 0
            if status in _NO_RETRY_STATUSES:
                raise RuntimeError(
                    f"{context or url} failed with {status}: {exc}"
                ) from exc
            # Server error (5xx) - retry
            if attempt == max_retries:
                raise RuntimeError(
                    f"{context or url} failed after {max_retries} attempts: {exc}"
                ) from exc
            logger.warning("%s attempt %d failed (%d): %s", context or url, attempt, status, exc)
            time.sleep(min(2 ** attempt, 10))
        except requests.RequestException as exc:
            # Connection error, timeout, etc. - retry
            if attempt == max_retries:
                raise RuntimeError(
                    f"{context or url} failed after {max_retries} attempts: {exc}"
                ) from exc
            logger.warning("%s attempt %d failed: %s", context or url, attempt, exc)
            time.sleep(min(2 ** attempt, 10))
    raise RuntimeError("unreachable")  # type checker appeasement
