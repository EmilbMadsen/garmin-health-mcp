from __future__ import annotations

import itertools
import logging
from datetime import date

import pytest
import requests
from garmin_personal_api import GarminPersonalApiError, HttpError

from garmin_health_mcp.client import (
    STRESS,
    GarminError,
    check_activity_id,
    chunks,
    describe_error,
    merge,
    parse_range,
    status_code,
)

from .conftest import Harness, Scripted

SECRET = "secret health payload"


def test_http_error_description_has_no_body_or_query() -> None:
    exc = HttpError(
        status_code=429,
        method="GET",
        url="https://personal.connectapi.garmin.com/sleep-service/sleep/dailySleep?date=2026-11-16",
        response_text='{"sleepScore": 81}',
    )
    assert describe_error(exc) == "GET /sleep-service/sleep/dailySleep -> HTTP 429"
    assert status_code(exc) == 429
    assert describe_error(GarminPersonalApiError("bad id")) == "GarminPersonalApiError: bad id"
    assert describe_error(ValueError("x")) == "ValueError"


def test_chunks_cover_the_range_without_gaps() -> None:
    parts = chunks(date(2024, 1, 1), date(2024, 3, 1), 28)
    assert parts[0] == (date(2024, 1, 1), date(2024, 1, 28))
    assert parts[-1][1] == date(2024, 3, 1)
    for (_, end), (start, _) in itertools.pairwise(parts):
        assert (start - end).days == 1
    assert chunks(date(2024, 1, 1), date(2024, 1, 1), 28) == [(date(2024, 1, 1), date(2024, 1, 1))]


def test_merge_lists_and_wrapped_lists() -> None:
    assert merge([[1], [2, 3]], None) == [1, 2, 3]
    assert merge([{"data": [1], "x": 1}, {"data": [2]}], "data") == {"data": [1, 2], "x": 1}


@pytest.mark.parametrize(
    ("start", "end", "message"),
    [
        ("2024-1-01", "2024-01-02", "YYYY-MM-DD"),
        ("2024-01-01", "tomorrow", "YYYY-MM-DD"),
        ("2024-01-05", "2024-01-01", "on or after"),
        ("2023-01-01", "2024-01-02", "at most 366"),
    ],
)
def test_parse_range_rejects(start: str, end: str, message: str) -> None:
    with pytest.raises(GarminError, match=message):
        parse_range(start, end)


def test_one_full_year_is_allowed() -> None:
    assert parse_range("2024-01-01", "2024-12-31")


@pytest.mark.parametrize("bad", [0, -5, True])
def test_activity_id_validation(bad: int) -> None:
    with pytest.raises(GarminError):
        check_activity_id(bad)


async def test_range_is_chunked_and_merged(harness: Harness) -> None:
    result = await harness.service.call_range(STRESS, "2024-01-01", "2024-02-29")
    paths = [path for path, _, _ in harness.garmin.requests]
    assert paths == [
        "/usersummary-service/stats/stress/daily/2024-01-01/2024-01-28",
        "/usersummary-service/stats/stress/daily/2024-01-29/2024-02-25",
        "/usersummary-service/stats/stress/daily/2024-02-26/2024-02-29",
    ]
    assert isinstance(result, list) and len(result) == 3 * 7


async def test_401_refreshes_the_token_once(harness: Harness) -> None:
    harness.garmin.queue.append(Scripted(401))
    result = await harness.service.call(lambda c: c.get_heart_rate_zones())
    assert isinstance(result, list)
    assert harness.tokens == [False, True]
    assert [auth for _, _, auth in harness.garmin.requests] == [
        "Bearer token-1",
        "Bearer token-2",
    ]


async def test_second_401_gives_setup_hint(harness: Harness) -> None:
    harness.garmin.queue += [Scripted(401), Scripted(401)]
    with pytest.raises(GarminError, match="setup") as info:
        await harness.service.call(lambda c: c.get_heart_rate_zones())
    assert SECRET not in str(info.value)
    assert harness.tokens == [False, True]


async def test_429_honours_retry_after(harness: Harness) -> None:
    harness.garmin.queue.append(Scripted(429, headers={"Retry-After": "7"}))
    await harness.service.call(lambda c: c.get_heart_rate_zones())
    assert harness.sleeps and harness.sleeps[0] >= 7


async def test_long_retry_after_fails_fast(harness: Harness) -> None:
    harness.garmin.queue.append(Scripted(429, headers={"Retry-After": "3600"}))
    with pytest.raises(GarminError, match="wait 3600"):
        await harness.service.call(lambda c: c.get_heart_rate_zones())
    assert harness.sleeps == []


async def test_5xx_gives_up_after_three_attempts(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    harness.garmin.queue += [Scripted(503), Scripted(502), Scripted(500), Scripted(500)]
    with caplog.at_level(logging.DEBUG), pytest.raises(GarminError, match="HTTP 500") as info:
        await harness.service.call(lambda c: c.get_heart_rate_zones())
    assert len(harness.garmin.requests) == 3
    assert len(harness.sleeps) == 2
    assert harness.sleeps[1] >= 2  # exponential: base 1 s, then 2 s (+ jitter)
    assert SECRET not in str(info.value)
    assert SECRET not in caplog.text
    assert "test-api-key" not in caplog.text and "token-1" not in caplog.text


async def test_network_errors_are_retried(harness: Harness) -> None:
    harness.garmin.queue.append(requests.ConnectionError("boom"))
    assert await harness.service.call(lambda c: c.get_heart_rate_zones())
    assert len(harness.sleeps) == 1


async def test_404_is_reported_as_no_data(harness: Harness) -> None:
    harness.garmin.queue.append(Scripted(404))
    with pytest.raises(GarminError, match="no data"):
        await harness.service.call(lambda c: c.get_heart_rate_zones())
