"""HTTP sessions with retry policy.

Reads and writes get *different* policies on purpose.

Reads are idempotent, so they retry broadly: 429 and 5xx, with exponential
backoff that honours ``Retry-After``.

The BambooHR balance-adjustment write is NOT idempotent -- each successful
call creates another adjustment. If we send a PUT and then fail to read the
response, we genuinely do not know whether the balance moved, and retrying
could double it. So the write session retries only where the request
provably never reached the server (connection refused, DNS failure) or where
the server explicitly declined to process it (429). Anything else raises
``AmbiguousWriteError``, which the caller records in the ledger as
``uncertain`` and blocks until a human confirms.
"""

from __future__ import annotations

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


class AmbiguousWriteError(RuntimeError):
    """A write may or may not have been applied. Never retry automatically."""


class ApiError(RuntimeError):
    """A read failed after exhausting retries."""


def _adapter(retry: Retry) -> HTTPAdapter:
    return HTTPAdapter(max_retries=retry, pool_maxsize=10)


def build_read_session(
    *, max_retries: int, backoff_seconds: float, user_agent: str
) -> requests.Session:
    retry = Retry(
        total=max_retries,
        connect=max_retries,
        read=max_retries,
        status=max_retries,
        backoff_factor=backoff_seconds,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset({"GET"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", _adapter(retry))
    session.headers.update({"User-Agent": user_agent})
    return session


def build_write_session(
    *, backoff_seconds: float, user_agent: str
) -> requests.Session:
    """Retries only conditions that cannot have applied the adjustment."""
    retry = Retry(
        total=3,
        connect=3,  # connection never established -> nothing was applied
        read=0,  # response lost after send -> outcome unknown, do not retry
        status=3,
        backoff_factor=backoff_seconds,
        status_forcelist=(429,),  # explicitly refused, not processed
        allowed_methods=frozenset({"PUT"}),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    session = requests.Session()
    session.mount("https://", _adapter(retry))
    session.headers.update({"User-Agent": user_agent})
    return session


def get_json(
    session: requests.Session,
    url: str,
    *,
    timeout: float,
    context: str,
    **kwargs: object,
) -> object:
    """GET returning parsed JSON, raising ``ApiError`` with useful detail."""
    try:
        response = session.get(url, timeout=timeout, **kwargs)  # type: ignore[arg-type]
    except requests.RequestException as exc:
        raise ApiError(f"{context}: request to {url} failed: {exc}") from exc

    if response.status_code >= 400:
        raise ApiError(
            f"{context}: {url} returned HTTP {response.status_code}: "
            f"{response.text[:500]}"
        )
    try:
        return response.json()
    except ValueError as exc:
        raise ApiError(
            f"{context}: {url} returned non-JSON body: {response.text[:500]}"
        ) from exc


def put_json(
    session: requests.Session,
    url: str,
    payload: dict,
    *,
    timeout: float,
    context: str,
    **kwargs: object,
) -> None:
    """PUT a write, distinguishing 'definitely failed' from 'unknown'.

    Raises ``ApiError`` when the server clearly rejected the request (4xx, so
    nothing was applied) and ``AmbiguousWriteError`` when we cannot tell.
    """
    try:
        response = session.put(
            url, json=payload, timeout=timeout, **kwargs  # type: ignore[arg-type]
        )
    except requests.ConnectionError as exc:
        # Retries for connect failures are already exhausted; the request
        # never established a connection, so nothing was applied.
        raise ApiError(f"{context}: could not connect to {url}: {exc}") from exc
    except requests.Timeout as exc:
        # The request was sent but no response arrived. It may have applied.
        raise AmbiguousWriteError(
            f"{context}: timed out waiting for {url}. The adjustment may or "
            f"may not have been applied: {exc}"
        ) from exc
    except requests.RequestException as exc:
        raise AmbiguousWriteError(
            f"{context}: request to {url} failed after sending: {exc}"
        ) from exc

    if 200 <= response.status_code < 300:
        return

    if 400 <= response.status_code < 500 and response.status_code != 429:
        # The server refused it outright; the balance did not move.
        raise ApiError(
            f"{context}: {url} rejected the adjustment with HTTP "
            f"{response.status_code}: {response.text[:500]}"
        )

    # 5xx, or a 429 that survived retries: the server may have applied the
    # adjustment before failing to respond cleanly.
    raise AmbiguousWriteError(
        f"{context}: {url} returned HTTP {response.status_code}. Cannot "
        f"confirm whether the adjustment applied: {response.text[:500]}"
    )
