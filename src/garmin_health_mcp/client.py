"""Service layer over Garmin's synchronous ``garmin-connect-personal-api-python``.

- Calls run in a worker thread (``anyio.to_thread``), so the event loop never blocks.
- The bearer token is obtained lazily and cached by Garmin's authenticator (0600, state dir).
- HTTP 401: one forced token refresh and a retry, then an error with a setup hint.
- HTTP 429/5xx and network errors: exponential backoff with jitter, at most 3 attempts in total,
  honouring ``Retry-After``.
- Date ranges are split into chunks the API accepts and merged again.
- Logs carry method, path and status only. Response bodies are health data and never logged.
"""

from __future__ import annotations

import logging
import random
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any
from urllib.parse import urlsplit

import anyio.to_thread
import requests
from garmin_personal_api import (
    GarminPersonalApiClient,
    GarminPersonalApiError,
    HttpError,
    PersonalApiAuthenticator,
)

from .credentials import SETUP_HINT, CredentialsMissingError, load_api_key, token_cache_file

_LOGGER = logging.getLogger(__name__)

MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 1.0
MAX_RETRY_AFTER_SECONDS = 60.0
MAX_RANGE_DAYS = 366

Api = GarminPersonalApiClient
Call = Callable[[Api], Any]


class GarminError(Exception):
    """A failure with a message that is safe to show to the user (no response bodies)."""


@dataclass(frozen=True)
class RangeEndpoint:
    """A range endpoint and the largest range (in days) it is called with at once.

    ``list_key`` names the list inside an object response (``{"data": [...]}``) to merge; None
    means the response itself is a list.
    """

    method: str
    chunk_days: int
    list_key: str | None = None


# Chunk sizes per endpoint; endpoints without a known limit use a conservative 28 days.
SLEEP_SUMMARIES = RangeEndpoint("get_daily_sleep_summaries", 31)
HRV_SUMMARIES = RangeEndpoint("get_daily_hrv_summaries", 28, "hrvSummaries")
BODY_BATTERY = RangeEndpoint("get_daily_body_battery", 31)
STRESS = RangeEndpoint("get_daily_stress", 28)
STEPS = RangeEndpoint("get_daily_steps", 28)
DAILY_SUMMARIES = RangeEndpoint("get_daily_summaries", MAX_RANGE_DAYS, "data")
HYDRATION = RangeEndpoint("get_daily_hydration", 28)
WELLNESS_ACTIVITIES = RangeEndpoint("get_wellness_activity_summaries", 28)


def parse_date(value: str, name: str = "date") -> date:
    try:
        if len(value) != 10:
            raise ValueError
        return date.fromisoformat(value)
    except ValueError:
        raise GarminError(f"{name} must be a date in YYYY-MM-DD format, got {value!r}.") from None


def parse_range(start: str, end: str) -> tuple[date, date]:
    first, last = parse_date(start, "start_date"), parse_date(end, "end_date")
    if last < first:
        raise GarminError("end_date must be on or after start_date.")
    days = (last - first).days + 1
    if days > MAX_RANGE_DAYS:
        raise GarminError(f"The range is {days} days; at most {MAX_RANGE_DAYS} days (1 year).")
    return first, last


def check_activity_id(activity_id: int) -> int:
    if isinstance(activity_id, bool) or not isinstance(activity_id, int) or activity_id <= 0:
        raise GarminError("activity_id must be a positive integer.")
    return activity_id


def chunks(first: date, last: date, size: int) -> list[tuple[date, date]]:
    out: list[tuple[date, date]] = []
    cursor = first
    while cursor <= last:
        stop = min(cursor + timedelta(days=size - 1), last)
        out.append((cursor, stop))
        cursor = stop + timedelta(days=1)
    return out


def merge(parts: list[Any], list_key: str | None) -> Any:
    if len(parts) == 1:
        return parts[0]
    if list_key is None:
        return [item for part in parts if isinstance(part, list) for item in part]
    merged: dict[str, Any] = dict(parts[0]) if isinstance(parts[0], dict) else {}
    merged[list_key] = [
        item
        for part in parts
        if isinstance(part, dict) and isinstance(part.get(list_key), list)
        for item in part[list_key]
    ]
    return merged


def describe_error(exc: BaseException) -> str:
    """A log-safe one-line description: method, path and status, never the response body."""
    if isinstance(exc, HttpError):
        return f"{exc.method} {urlsplit(exc.url).path} -> HTTP {exc.status_code}"
    if isinstance(exc, GarminPersonalApiError):
        # The library's own non-HTTP errors are short validation messages without data.
        return f"{type(exc).__name__}: {exc}"
    return type(exc).__name__


def status_code(exc: BaseException) -> int | None:
    return exc.status_code if isinstance(exc, HttpError) else None


def open_client(
    api_key: str | None = None,
    *,
    env: Mapping[str, str] | None = None,
    force_refresh: bool = False,
) -> GarminPersonalApiClient:
    """Exchange the API key for a (cached) bearer token and return a ready client."""
    key = api_key if api_key is not None else load_api_key(env)
    with PersonalApiAuthenticator(bearer_token_cache_file=token_cache_file(env)) as auth:
        token = auth.obtain_token(key, force_refresh=force_refresh)
    client: GarminPersonalApiClient = GarminPersonalApiClient(token.access_token)
    return client


def _retry_after_seconds(raw: str | None) -> float | None:
    if raw is None:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None  # HTTP-date form: fall back to our own backoff


class GarminService:
    """Owns one authenticated client and runs calls with refresh, retry and chunking."""

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        api_key_loader: Callable[[], str] | None = None,
        token_provider: Callable[[str, bool], str] | None = None,
        session_factory: Callable[[], requests.Session] = requests.Session,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._env = env
        self._load_key = api_key_loader or (lambda: load_api_key(env))
        self._token_provider = token_provider or self._obtain_token
        self._session_factory = session_factory
        self._sleep = sleep
        self._lock = threading.Lock()
        self._client: GarminPersonalApiClient | None = None
        self._last = threading.local()

    # -- token -----------------------------------------------------------------------------

    def _obtain_token(self, api_key: str, force_refresh: bool) -> str:
        cache = token_cache_file(self._env)
        with PersonalApiAuthenticator(bearer_token_cache_file=cache) as auth:
            token: str = auth.obtain_token(api_key, force_refresh=force_refresh).access_token
        return token

    def _token(self, force_refresh: bool) -> str:
        try:
            return self._token_provider(self._load_key(), force_refresh)
        except CredentialsMissingError as exc:
            raise GarminError(str(exc)) from None
        except HttpError as exc:
            _LOGGER.warning("token exchange failed: %s", describe_error(exc))
            if exc.status_code in (400, 401, 403):
                raise GarminError(
                    f"Garmin rejected the Personal API key (HTTP {exc.status_code}). "
                    f"It may have expired or been replaced. {SETUP_HINT}"
                ) from None
            raise GarminError(f"Token exchange failed with HTTP {exc.status_code}.") from None
        except (GarminPersonalApiError, requests.RequestException) as exc:
            raise GarminError(f"Token exchange failed: {describe_error(exc)}") from None

    def _api(self, *, force_refresh: bool = False) -> GarminPersonalApiClient:
        with self._lock:
            if self._client is None:
                session = self._session_factory()
                session.hooks["response"].append(self._remember_response)
                self._client = GarminPersonalApiClient(self._token(force_refresh), session=session)
            elif force_refresh:
                self._client.update_bearer_token(self._token(True))
            return self._client

    def _remember_response(self, response: requests.Response, *_: Any, **__: Any) -> None:
        self._last.retry_after = response.headers.get("Retry-After")
        method = response.request.method if response.request is not None else "GET"
        _LOGGER.info("%s %s -> %s", method, urlsplit(response.url).path, response.status_code)

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                self._client.close()
                self._client = None

    # -- calls -----------------------------------------------------------------------------

    def call_sync(self, call: Call) -> Any:
        api = self._api()
        refreshed = False
        attempt = 1
        while True:
            self._last.retry_after = None
            try:
                return call(api)
            except HttpError as exc:
                code = exc.status_code
                if code == 401 and not refreshed:
                    _LOGGER.info("HTTP 401; refreshing the bearer token once")
                    api = self._api(force_refresh=True)
                    refreshed = True
                    continue
                if code in (401, 403):
                    raise GarminError(
                        f"Garmin refused access (HTTP {code}) even with a fresh token. {SETUP_HINT}"
                    ) from None
                if code == 404:
                    raise GarminError("Garmin has no data for that request (HTTP 404).") from None
                if (code == 429 or code >= 500) and attempt < MAX_ATTEMPTS:
                    self._backoff(attempt, describe_error(exc))
                    attempt += 1
                    continue
                if code == 429:
                    raise GarminError(
                        "Garmin is throttling requests (HTTP 429). Try again in a few minutes."
                    ) from None
                raise GarminError(f"Garmin request failed: {describe_error(exc)}") from None
            except requests.RequestException as exc:
                if attempt < MAX_ATTEMPTS:
                    self._backoff(attempt, type(exc).__name__)
                    attempt += 1
                    continue
                raise GarminError(f"Could not reach Garmin ({type(exc).__name__}).") from None
            except GarminPersonalApiError as exc:
                raise GarminError(describe_error(exc)) from None

    def _backoff(self, attempt: int, reason: str) -> None:
        retry_after = _retry_after_seconds(getattr(self._last, "retry_after", None))
        if retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
            raise GarminError(
                f"Garmin asked to wait {retry_after:.0f} s before retrying. Try again later."
            )
        delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
        delay += random.uniform(0, delay)  # jitter on top of the base delay
        if retry_after is not None:
            delay = max(delay, retry_after)
        _LOGGER.warning("%s; retrying in %.1f s (attempt %d)", reason, delay, attempt + 1)
        self._sleep(delay)

    async def call(self, call: Call) -> Any:
        return await anyio.to_thread.run_sync(self.call_sync, call)

    async def call_range(self, endpoint: RangeEndpoint, start: str, end: str) -> Any:
        first, last = parse_range(start, end)
        parts: list[Any] = []
        for chunk_start, chunk_end in chunks(first, last, endpoint.chunk_days):
            parts.append(await self.call(_range_call(endpoint.method, chunk_start, chunk_end)))
        return merge(parts, endpoint.list_key)


def _range_call(method: str, first: date, last: date) -> Call:
    def call(api: Api) -> Any:
        return getattr(api, method)(first, last)

    return call
