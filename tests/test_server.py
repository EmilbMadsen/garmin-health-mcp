"""Tool tests against the synthetic fixtures through the HTTP test double."""

from __future__ import annotations

import json
import re
from typing import Any

import pytest
from mcp.server.mcpserver.exceptions import ToolError

from garmin_health_mcp import server as srv

from .conftest import Harness, Scripted

ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d[+-]\d\d:\d\d$")

EXPECTED_TOOLS = {
    "get_sleep",
    "get_sleep_summaries",
    "get_hrv",
    "get_hrv_summaries",
    "get_body_battery",
    "get_stress",
    "get_steps",
    "get_daily_summaries",
    "get_heart_rate",
    "get_heart_rate_zones",
    "get_spo2",
    "get_fitness_age",
    "get_weight",
    "get_weight_goals",
    "get_hydration",
    "get_food_logs",
    "get_wellness_activity_summaries",
    "get_profile_settings",
    "search_activities",
    "get_activity",
    "get_activity_workouts",
    "get_activity_laps",
    "get_activity_splits",
    "get_activity_time_in_hr_zones",
    "get_activity_summary_stats",
}

FORBIDDEN = ("Example User", "example.invalid", "Latitude", "Longitude", "userProfile", "ownerId")


def assert_clean(result: Any) -> str:
    text = json.dumps(result)
    for marker in FORBIDDEN:
        assert marker not in text, marker
    assert "GMT" not in text and "Gmt" not in text
    assert ": null" not in text
    assert not re.search(r"(?<![\d.])1[67]\d{11}(?![\d.])", text), "raw epoch-ms timestamp"
    for value in times(result):
        assert ISO.match(value) or re.match(r"^\d{4}-\d\d-\d\d$", value), value
    return text


def times(value: Any, out: list[str] | None = None) -> list[str]:
    """Every string value that looks like a timestamp."""
    out = [] if out is None else out
    if isinstance(value, dict):
        for item in value.values():
            times(item, out)
    elif isinstance(value, list):
        for item in value:
            times(item, out)
    elif isinstance(value, str) and re.match(r"^\d{4}-\d\d-\d\d[T ]\d", value):
        out.append(value)
    return out


async def test_tools_are_registered_and_read_only() -> None:
    tools = await srv.server.list_tools()
    assert {t.name for t in tools} == EXPECTED_TOOLS
    for tool in tools:
        assert tool.annotations is not None and tool.annotations.read_only_hint, tool.name
        assert tool.description and len(tool.description) > 40, tool.name


async def test_missing_api_is_explained() -> None:
    assert "training readiness" in (srv.server.instructions or "")


async def test_sleep(harness: Harness) -> None:
    result = await srv.get_sleep("2024-01-10")
    sleep = result["sleep"]
    assert ISO.match(sleep["sleepStartTimestamp"])
    assert "sleepStartTimestampLocal" not in sleep
    assert sleep["sleepScores"]["overall"]["value"] > 0
    assert_clean(result)
    _, query, _ = harness.garmin.requests[0]
    assert query == {"date": ["2024-01-10"]}


async def test_sleep_summaries_are_compact(harness: Harness) -> None:
    result = await srv.get_sleep_summaries("2024-01-04", "2024-01-10")
    nights = result["nights"]
    assert len(nights) == 7
    assert {"calendarDate", "sleepTimeSeconds", "sleepScore"} <= set(nights[0])
    assert len(json.dumps(result)) < 7 * 800
    assert_clean(result)


async def test_hrv_hides_readings_by_default(harness: Harness) -> None:
    result = await srv.get_hrv("2024-01-10")
    assert "hrvReadings" not in result["hrv"]
    assert result["hrv"]["readingCount"] > 0
    full = await srv.get_hrv("2024-01-10", include_readings=True)
    assert ISO.match(full["hrv"]["hrvReadings"][0]["readingTime"])
    assert_clean(full)


async def test_heart_rate_is_summarized_per_hour(harness: Harness) -> None:
    result = await srv.get_heart_rate("2024-01-10")
    hr = result["heartRate"]
    assert hr["restingHeartRate"] > 0
    assert hr["hourly"] and set(hr["hourly"][0]) == {"hour", "min", "avg", "max"}
    assert "samples" not in hr and "heartRateValues" not in hr
    with_samples = await srv.get_heart_rate("2024-01-10", include_samples=True)
    assert ISO.match(with_samples["heartRate"]["samples"][0][0])


async def test_body_battery_levels(harness: Harness) -> None:
    result = await srv.get_body_battery("2024-01-04", "2024-01-10")
    day = result["days"][0]
    assert ISO.match(day["levels"][0]["time"])
    assert "bodyBatteryLevel" in day["levels"][0]
    assert_clean(result)


async def test_spo2_drops_raw_series(harness: Harness) -> None:
    result = await srv.get_spo2("2024-01-10")
    spo2 = result["spo2"]
    assert "monitoringEnvironmentValues" not in spo2
    assert ISO.match(spo2["hourlyAverages"][0]["time"])
    assert_clean(result)


async def test_daily_summaries(harness: Harness) -> None:
    result = await srv.get_daily_summaries("2024-01-04", "2024-01-10")
    day = result["days"][0]
    assert {"calendarDate", "movement", "calories", "heartRate"} <= set(day)
    assert "activityEvents" not in day.get("bodyBattery", {})
    assert_clean(result)


@pytest.mark.parametrize(
    "tool",
    [
        srv.get_stress,
        srv.get_steps,
        srv.get_hrv_summaries,
        srv.get_hydration,
        srv.get_weight,
        srv.get_weight_goals,
        srv.get_wellness_activity_summaries,
    ],
)
async def test_range_tools_run(harness: Harness, tool: Any) -> None:
    result = await tool("2024-01-04", "2024-01-10")
    assert_clean(result)
    for value in times(result):
        assert ISO.match(value) or re.match(r"^\d{4}-\d\d-\d\d$", value), value


async def test_single_tools_run(harness: Harness) -> None:
    for result in (
        await srv.get_heart_rate_zones(),
        await srv.get_fitness_age("2024-01-10"),
        await srv.get_food_logs("2024-01-10"),
    ):
        assert_clean(result)


async def test_profile_is_minimal(harness: Harness) -> None:
    result = await srv.get_profile_settings()
    assert set(result["profile"]) <= {
        "measurementSystem",
        "preferredLocale",
        "timeZone",
        "hydrationMeasurementUnit",
        "firstDayOfWeek",
        "timeFormat",
        "dateFormat",
    }


async def test_search_activities(harness: Harness) -> None:
    result = await srv.search_activities(start_date="2024-01-01", limit=5)
    activities = result["activities"]
    assert [a["activityId"] for a in activities] == [1001, 1002, 1003]
    first = activities[0]
    assert ISO.match(first["startTime"])
    assert isinstance(first["activityType"], str)
    assert "zone1" in first["hrTimeInZoneSeconds"]
    assert len(json.dumps(first)) < 1500
    assert_clean(result)
    _, query, _ = harness.garmin.requests[0]
    assert query["startDate"] == ["2024-01-01"] and query["limit"] == ["5"]


async def test_activity_detail(harness: Harness) -> None:
    result = await srv.get_activity(1001)
    activity = result["activity"]
    assert activity["activityId"] == 1001
    assert isinstance(activity["activityType"], str)
    assert ISO.match(activity["summaryDTO"]["startTime"])
    assert "userInfoDto" not in json.dumps(activity)
    assert_clean(result)
    assert (await srv.get_activity_workouts(1001)) == {"workouts": []}


async def test_default_range_is_last_7_days(harness: Harness) -> None:
    await srv.get_steps()
    path, _, _ = harness.garmin.requests[0]
    first, last = path.rsplit("/", 2)[-2:]
    from datetime import date

    assert (date.fromisoformat(last) - date.fromisoformat(first)).days == 6


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda: srv.get_sleep("16-11-2026"), "YYYY-MM-DD"),
        (lambda: srv.get_steps("2023-01-01", "2024-06-01"), "at most 366"),
        (lambda: srv.get_steps("2024-01-05", "2024-01-01"), "on or after"),
        (lambda: srv.search_activities("2024-01-05", "2024-01-01"), "on or after"),
        (lambda: srv.get_activity(0), "positive"),
    ],
)
async def test_invalid_input(harness: Harness, call: Any, message: str) -> None:
    with pytest.raises(ToolError, match=message):
        await call()
    assert harness.garmin.requests == []


async def test_auth_failure_becomes_tool_error(harness: Harness) -> None:
    harness.garmin.queue += [Scripted(401), Scripted(401)]
    with pytest.raises(ToolError, match="setup"):
        await srv.get_heart_rate_zones()


# -- FIT analysis tools -----------------------------------------------------------------------


@pytest.fixture
def with_run(harness: Harness) -> Harness:
    from .fit_factory import synthetic_run, zipped

    harness.garmin.downloads[1001] = zipped(synthetic_run())
    return harness


def _downloads(harness: Harness) -> int:
    return sum(1 for path, _, _ in harness.garmin.requests if "/download-service/" in path)


async def test_fit_file_is_downloaded_once_and_cached(with_run: Harness, tmp_path: Any) -> None:
    await srv.get_activity_laps(1001)
    await srv.get_activity_splits(1001)
    await srv.get_activity_summary_stats(1001)
    assert _downloads(with_run) == 1
    cached = tmp_path / "state" / "fit" / "1001.fit"
    assert cached.exists() and cached.stat().st_mode & 0o077 == 0


async def test_laps_tool(with_run: Harness) -> None:
    result = await srv.get_activity_laps(1001)
    assert result["sport"] == "running" and result["lapCount"] == 2
    assert_clean(result)


async def test_splits_tool(with_run: Harness) -> None:
    result = await srv.get_activity_splits(1001, split_meters=500)
    assert len(result["splits"]) == 5
    assert result["splits"][0]["pace"] == "4:00 /km"
    assert len(json.dumps(result)) < 20_000


async def test_time_in_zones_uses_current_garmin_zones(with_run: Harness) -> None:
    result = await srv.get_activity_time_in_hr_zones(1001)
    assert result["zoneSource"].startswith("current Garmin zones")
    total = sum(z["seconds"] for z in result["timeInZones"].values())
    assert total == 600
    assert sum(result["watchTimeInZonesSeconds"].values()) == 600
    assert any("heartRateZones" in path for path, _, _ in with_run.garmin.requests)


async def test_time_in_zones_with_recorded_zones(with_run: Harness) -> None:
    result = await srv.get_activity_time_in_hr_zones(1001, zones="recorded")
    assert result["zoneSource"] == "zones on the watch during the activity"
    assert result["timeInZones"]["zone1"]["bpm"] == "110-129"
    assert not any("heartRateZones" in path for path, _, _ in with_run.garmin.requests)


async def test_summary_tool(with_run: Harness) -> None:
    result = await srv.get_activity_summary_stats(1001)
    assert result["summary"]["aerobicTrainingEffect"] == 3.1
    assert ISO.match(result["summary"]["startTime"])
    assert_clean(result)


async def test_strength_splits_are_a_tool_error(harness: Harness) -> None:
    from .fit_factory import synthetic_strength, zipped

    harness.garmin.downloads[1002] = zipped(synthetic_strength())
    with pytest.raises(ToolError, match="no distance"):
        await srv.get_activity_splits(1002)


async def test_missing_activity_file(harness: Harness) -> None:
    with pytest.raises(ToolError, match="no data"):
        await srv.get_activity_laps(9999)


async def test_non_fit_upload(harness: Harness) -> None:
    from .fit_factory import zipped

    harness.garmin.downloads[1003] = zipped(b"<gpx/>", name="1003.gpx")
    with pytest.raises(ToolError, match="no FIT"):
        await srv.get_activity_summary_stats(1003)
