from __future__ import annotations

from typing import Any
from zoneinfo import ZoneInfo

import pytest

from garmin_health_mcp import presentation as p

CPH = ZoneInfo("Europe/Copenhagen")


def test_timestamp_formats_become_local_iso() -> None:
    # 2024-01-15 06:30 UTC is 07:30 in Copenhagen (winter, +01:00)
    expected = "2024-01-15T07:30:00+01:00"
    assert p.to_local_iso(1705300200000, CPH) == expected
    assert p.to_local_iso("2024-01-15T06:30:00.0", CPH) == expected
    assert p.to_local_iso("2024-01-15 06:30:00", CPH) == expected
    assert p.to_local_iso("2024-01-15", CPH) == "2024-01-15"
    assert p.to_local_iso(42, CPH) == 42


def test_summer_time_offset() -> None:
    assert p.to_local_iso("2024-07-01T10:00:00.0", CPH) == "2024-07-01T12:00:00+02:00"


def test_clean_renames_gmt_and_drops_local_twin() -> None:
    raw = {
        "startTimeGMT": "2024-01-15 06:30:00",
        "startTimeLocal": "2024-01-15 07:30:00",
        "eventTimestampGmt": "2024-01-15T06:30:00.0",
        "calendarDate": "2024-01-15",
    }
    assert p.clean(raw, CPH) == {
        "startTime": "2024-01-15T07:30:00+01:00",
        "eventTimestamp": "2024-01-15T07:30:00+01:00",
        "calendarDate": "2024-01-15",
    }


def test_clean_removes_empty_identifying_and_location_fields() -> None:
    raw = {
        "ownerFullName": "Someone",
        "ownerProfileImageUrlSmall": "https://x/y.png",
        "userProfilePK": 123,
        "startLatitude": 55.6,
        "endLongitude": 12.5,
        "nested": {"displayName": "x", "value": None, "list": []},
        "steps": 0,
        "kept": False,
    }
    assert p.clean(raw, CPH) == {"steps": 0, "kept": False}


def test_series_and_hourly() -> None:
    rows = [[1705300200000, 60], [1705300800000, 70], [1705303800000, 80], [1705304000000, None]]
    descriptors = [{"index": 0, "key": "timestamp"}, {"index": 1, "key": "heartrate"}]
    points = p.series(rows, descriptors, index_key="index", name_key="key", tz=CPH)
    assert points[0] == {"time": "2024-01-15T07:30:00+01:00", "heartrate": 60}
    assert len(points) == 3  # the row without a value is dropped
    assert p.hourly(points, "heartrate") == [
        {"hour": "2024-01-15T07:00:00+01:00", "min": 60.0, "avg": 65, "max": 70.0},
        {"hour": "2024-01-15T08:00:00+01:00", "min": 80.0, "avg": 80, "max": 80.0},
    ]


def test_profile_keeps_only_settings() -> None:
    raw = {
        "displayName": "secret",
        "measurementSystem": "metric",
        "timeZone": "Europe/Copenhagen",
        "firstDayOfWeek": {"dayName": "monday"},
        "timeFormat": {"formatKey": "time_twenty_four_hr"},
    }
    assert p.profile(raw) == {
        "measurementSystem": "metric",
        "timeZone": "Europe/Copenhagen",
        "firstDayOfWeek": "monday",
        "timeFormat": "time_twenty_four_hr",
    }


def test_invalid_tz_falls_back(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(p.TZ_ENV_VAR, "Not/AZone")
    assert str(p.user_tz()) == p.DEFAULT_TZ


def test_float_noise_is_removed() -> None:
    raw = {
        "te": 1.600000023841858,
        "load": 24.266754150390625,
        "dur": 2346.279052734375,
        "marker": 0.012237549,
        "n": 5,
    }
    assert p.clean(raw, CPH) == {"te": 1.6, "load": 24.27, "dur": 2346.3, "marker": 0.0122, "n": 5}


STRENGTH: dict[str, Any] = {
    "activityId": 1,
    "distance": 0.0,
    "averageSpeed": 0.0,
    "steps": 0,
    "totalSets": 1,
    "activeSets": 1,
    "totalReps": 0,
    "aerobicTrainingEffect": 1.600000023841858,
    "summarizedExerciseSets": [
        {
            "category": "UNKNOWN",
            "reps": 0,
            "volume": 0,
            "duration": 2346279.05,
            "sets": 1,
            "maxWeight": 0,
        }
    ],
}


def test_strength_activity_without_real_sets() -> None:
    out = p.activity_compact(STRENGTH)
    assert out == {"activityId": 1, "aerobicTrainingEffect": 1.6}


def test_real_exercise_sets_are_kept_in_seconds() -> None:
    raw = dict(STRENGTH)
    raw["totalReps"] = 24
    raw["summarizedExerciseSets"] = [
        {
            "category": "SQUAT",
            "reps": 24,
            "volume": 2400.0,
            "duration": 95000.0,
            "sets": 3,
            "maxWeight": 100.0,
        },
        STRENGTH["summarizedExerciseSets"][0],
    ]
    out = p.activity_compact(raw)
    assert out["summarizedExerciseSets"] == [
        {
            "category": "SQUAT",
            "reps": 24,
            "volume": 2400.0,
            "sets": 3,
            "maxWeight": 100.0,
            "durationSeconds": 95,
        }
    ]
    assert out["totalSets"] == 1 and out["totalReps"] == 24


def test_running_keeps_distance_and_detail_drops_zero_fields() -> None:
    run = p.activity_compact({"activityId": 2, "distance": 18235.150390625, "steps": 16432})
    assert run == {"activityId": 2, "distance": 18235.2, "steps": 16432}
    detail = p.activity_detail(
        {"summaryDTO": {"distance": 0.0, "totalExerciseReps": 0, "calories": 340.0}}
    )
    assert detail["summaryDTO"] == {"calories": 340.0}
