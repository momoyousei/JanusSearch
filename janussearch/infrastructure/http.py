#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small HTTP client with explicit transport failures and content decoding."""

from __future__ import annotations

import gzip
import json
import logging
import socket
import time
import zlib
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

LOGGER = logging.getLogger("janussearch.http")

DEFAULT_HEADERS = {
    "User-Agent": "JanusSearch/1.0 (+https://github.com/)",
    "Accept": "application/json,text/html,application/xhtml+xml,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate",
}


@dataclass(frozen=True)
class HttpFetchError(RuntimeError):
    """One exhausted HTTP request with a machine-readable failure category."""

    url: str
    category: str
    message: str
    status_code: int | None = None

    def __str__(self) -> str:
        status = f" status={self.status_code}" if self.status_code is not None else ""
        return f"{self.category}{status} url={self.url}: {self.message}"


@dataclass(frozen=True)
class HttpResponse:
    """Decoded HTTP response payload and final URL."""

    url: str
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def text(self) -> str:
        """Decode the response body as UTF-8."""
        return decode_response_body(self.body, self.headers).decode("utf-8", "replace")

    def json(self) -> Any:
        """Decode a JSON response without hiding malformed payloads."""
        try:
            return json.loads(self.text())
        except json.JSONDecodeError as exc:
            raise HttpFetchError(
                url=self.url,
                category="invalid_json",
                status_code=self.status_code,
                message=str(exc),
            ) from exc


def decode_response_body(body: bytes, headers: Mapping[str, str] | None = None) -> bytes:
    """Decode gzip/deflate, including gzip bodies missing Content-Encoding."""
    normalized_headers = {str(key).lower(): str(value).lower() for key, value in (headers or {}).items()}
    encoding = normalized_headers.get("content-encoding", "")
    if body.startswith(b"\x1f\x8b") or "gzip" in encoding:
        return gzip.decompress(body)
    if "deflate" in encoding:
        try:
            return zlib.decompress(body)
        except zlib.error:
            return zlib.decompress(body, -zlib.MAX_WBITS)
    return body


def _http_category(status_code: int) -> str:
    if status_code == 403:
        return "http_forbidden"
    if status_code == 429:
        return "rate_limit"
    return "http_error"


def fetch_response(
    url: str,
    *,
    timeout: float = 30.0,
    retries: int = 3,
    min_interval: float = 0.5,
    headers: Mapping[str, str] | None = None,
) -> HttpResponse:
    """Fetch one URL with redirects and bounded retries."""
    request_headers = dict(DEFAULT_HEADERS)
    if headers:
        request_headers.update(headers)
    last_error: HttpFetchError | None = None
    attempts = max(1, retries)
    for attempt in range(1, attempts + 1):
        if attempt > 1 and min_interval > 0:
            time.sleep(min_interval)
        request = Request(url, headers=request_headers)
        try:
            with urlopen(request, timeout=timeout) as response:
                response_headers = {key.lower(): value for key, value in response.headers.items()}
                return HttpResponse(
                    url=response.geturl(),
                    status_code=int(response.status),
                    headers=response_headers,
                    body=response.read(),
                )
        except HTTPError as exc:
            category = _http_category(exc.code)
            last_error = HttpFetchError(
                url=url,
                category=category,
                status_code=exc.code,
                message=exc.reason or str(exc),
            )
            retryable = exc.code == 429 or 500 <= exc.code < 600
            if not retryable:
                raise last_error from exc
        except (URLError, TimeoutError, socket.timeout, ConnectionError, OSError) as exc:
            last_error = HttpFetchError(
                url=url,
                category="network_error",
                message=str(exc),
            )
        if attempt < attempts:
            LOGGER.warning("HTTP fetch failed (%s/%s): %s", attempt, attempts, last_error)
    assert last_error is not None
    raise last_error


def fetch_text(url: str, **kwargs: Any) -> str:
    """Fetch and decode text."""
    return fetch_response(url, **kwargs).text()


def fetch_json(url: str, **kwargs: Any) -> Any:
    """Fetch and decode JSON."""
    return fetch_response(url, **kwargs).json()
