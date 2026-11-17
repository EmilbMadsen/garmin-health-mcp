"""Turn raw Personal API responses into compact, model-friendly data.

Generic rules (``clean``):

- Timestamps in GMT (epoch milliseconds, ``2026-01-02T03:04:05.0`` or ``2026-01-02 03:04:05``)
  become ISO 8601 in the user's time zone (``GARMIN_HEALTH_MCP_TZ``, default
  Europe/Copenhagen). The ``GMT``/``Gmt`` suffix is dropped from the key, and the ``...Local``
  twin of a converted key is removed (it is the same instant without an offset).
- ``null``, empty strings, empty lists and empty objects are removed.
- Floats lose their binary noise: 1 decimal from 100, 2 from 1, else 4.
- Personal identifiers are removed: names, display names, profile/owner/device IDs, profile
  image URLs and GPS start/end coordinates.
- Time series sent as ``[[ts, value, ...], ...]`` plus a descriptor list become
  ``[{"time": ..., "<key>": value}, ...]``.

Endpoint-specific helpers then summarize long series so responses stay small.
"""

from __future__ import annotations

import os
import re
from collections import defaultdict
from datetime import UTC, datetime, tzinfo
from statistics import mean
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

TZ_ENV_VAR = "GARMIN_HEALTH_MCP_TZ"
DEFAULT_TZ = "Europe/Copenhagen"

_GMT_SUFFIXES = ("GMT", "Gmt")
_EPOCH_KEYS = frozenset({"beginTimestamp"})
_DROP_KEYS = frozenset(
    k.lower()
    for k in (
        "userProfilePk",
        "userProfileId",
        "ownerId",
        "ownerDisplayName",
        "ownerFullName",
        "displayName",
        "fullName",
        "userInfoDto",
        "uuid",
        "activityUUID",
        "deviceId",
        "accessControlRuleDTO",
        "privacy",
        "createTimeStamp",  # zone undocumented; bookkeeping only
    )
)
_DATETIME = re.compile(r"^\d{4}-\d\d-\d\d[T ]\d\d:\d\d:\d\d(\.\d+)?$")
_MIDNIGHT = re.compile(r"^\d{4}-\d\d-\d\dT00:00:00(\.0+)?$")


def user_tz() -> tzinfo:
    name = os.environ.get(TZ_ENV_VAR, "").strip() or DEFAULT_TZ
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return ZoneInfo(DEFAULT_TZ)


def to_local_iso(value: Any, tz: tzinfo | None = None) -> Any:
    """Convert a GMT timestamp (epoch ms or naive ISO string) to local ISO 8601."""
    zone = tz or user_tz()
    if isinstance(value, bool):
        return value
    if isinstance(value, int | float) and value > 10**11:
        moment = datetime.fromtimestamp(value / 1000, UTC)
    elif isinstance(value, str) and _DATETIME.match(value):
        moment = datetime.fromisoformat(value).replace(tzinfo=UTC)
    else:
        return value
    return moment.astimezone(zone).isoformat(timespec="seconds")


def _is_empty(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _dropped(key: str) -> bool:
    lowered = key.lower()
    return (
        lowered in _DROP_KEYS
        or "imageurl" in lowered
        or lowered.endswith(("latitude", "longitude"))  # start/end GPS points reveal home
    )


def _gmt_base(key: str) -> str | None:
    for suffix in _GMT_SUFFIXES:
        if key.endswith(suffix) and len(key) > len(suffix):
            return key[: -len(suffix)]
    return None


def tidy_number(value: float) -> float:
    """Strip float noise (1.600000023841858 -> 1.6) while keeping meaningful precision."""
    magnitude = abs(value)
    if magnitude >= 100:
        return round(value, 1)
    if magnitude >= 1:
        return round(value, 2)
    return round(value, 4)


def clean(value: Any, tz: tzinfo | None = None) -> Any:
    zone = tz or user_tz()
    if isinstance(value, list):
        items = [clean(item, zone) for item in value]
        return [item for item in items if not _is_empty(item)]
    if isinstance(value, float):
        return tidy_number(value)
    if not isinstance(value, dict):
        return value

    converted_bases = {base for key in value if (base := _gmt_base(key)) is not None}
    out: dict[str, Any] = {}
    for key, item in value.items():
        if _dropped(key):
            continue
        if key.endswith("Local") and key[:-5] in converted_bases:
            continue
        base = _gmt_base(key)
        if base is not None:
            out[base] = to_local_iso(item, zone)
            continue
        if key in _EPOCH_KEYS:
            out[key] = to_local_iso(item, zone)
            continue
        if key == "date" and isinstance(item, int) and "timestampGMT" in value:
            continue  # weigh-ins: local-shifted epoch duplicating timestampGMT
        if isinstance(item, str) and _MIDNIGHT.match(item):
            out[key] = item[:10]  # a date sent as a naive midnight timestamp
            continue
        cleaned = clean(item, zone)
        if not _is_empty(cleaned):
            out[key] = cleaned
    return {k: v for k, v in out.items() if not _is_empty(v)}


def series(
    rows: Any, descriptors: Any, *, index_key: str, name_key: str, tz: tzinfo | None = None
) -> list[dict[str, Any]]:
    """``[[ts, v1, v2], ...]`` + ``[{index, key}, ...]`` -> ``[{"time": iso, key: v}, ...]``."""
    if not isinstance(rows, list) or not isinstance(descriptors, list):
        return []
    names = {d[index_key]: d[name_key] for d in descriptors if isinstance(d, dict)}
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, list):
            continue
        point: dict[str, Any] = {}
        for i, cell in enumerate(row):
            name = names.get(i, f"v{i}")
            if name == "timestamp":
                point["time"] = to_local_iso(cell, tz)
            elif cell is not None:
                point[name] = cell
        if len(point) > 1:
            out.append(point)
    return out


def hourly(points: list[dict[str, Any]], key: str) -> list[dict[str, Any]]:
    """Min/avg/max of ``key`` per local hour."""
    buckets: dict[str, list[float]] = defaultdict(list)
    for point in points:
        value = point.get(key)
        if isinstance(value, int | float) and not isinstance(value, bool) and value > 0:
            stamp = str(point["time"])  # 2024-01-15T07:42:00+01:00 -> 2024-01-15T07:00:00+01:00
            buckets[f"{stamp[:13]}:00:00{stamp[19:]}"].append(float(value))
    return [
        {"hour": hour, "min": min(vals), "avg": round(mean(vals)), "max": max(vals)}
        for hour, vals in sorted(buckets.items())
    ]


# -- endpoint-specific ------------------------------------------------------------------------

_SLEEP_COMPACT_KEYS = (
    "calendarDate",
    "sleepStartTimestamp",
    "sleepEndTimestamp",
    "sleepTimeSeconds",
    "napTimeSeconds",
    "deepSleepSeconds",
    "lightSleepSeconds",
    "remSleepSeconds",
    "awakeSleepSeconds",
    "awakeCount",
    "avgHeartRate",
    "avgSleepStress",
    "averageSpO2Value",
    "lowestSpO2Value",
    "averageRespirationValue",
    "breathingDisruptionSeverity",
    "sleepScoreFeedback",
)


def sleep_day(raw: Any) -> Any:
    out = clean(raw)
    if isinstance(out, dict):
        for key in ("sleepWindowConfirmationType", "sleepVersion", "id", "retro", "ageGroup"):
            out.pop(key, None)
    return out


def sleep_compact(raw: Any) -> dict[str, Any]:
    day = clean(raw)
    if not isinstance(day, dict):
        return {}
    out = {k: day[k] for k in _SLEEP_COMPACT_KEYS if k in day}
    scores = day.get("sleepScores", {})
    if isinstance(scores, dict) and isinstance(scores.get("overall"), dict):
        out["sleepScore"] = scores["overall"].get("value")
        out["sleepScoreQualifier"] = scores["overall"].get("qualifierKey")
    need = day.get("sleepNeed")
    if isinstance(need, dict) and "actual" in need:
        out["sleepNeedMinutes"] = need["actual"]
    return {k: v for k, v in out.items() if not _is_empty(v)}


def heart_rate(raw: Any, *, include_samples: bool) -> Any:
    if not isinstance(raw, dict):
        return clean(raw)
    points = series(
        raw.get("heartRateValues"),
        raw.get("heartRateValueDescriptors"),
        index_key="index",
        name_key="key",
    )
    rest = {
        k: v for k, v in raw.items() if k not in ("heartRateValues", "heartRateValueDescriptors")
    }
    out: dict[str, Any] = clean(rest)
    out["hourly"] = hourly(points, "heartrate")
    if include_samples:
        out["samples"] = [[p["time"], p["heartrate"]] for p in points if "heartrate" in p]
    return out


def hrv(raw: Any, *, include_readings: bool) -> Any:
    out = clean(raw)
    if isinstance(out, dict) and not include_readings:
        readings = out.pop("hrvReadings", None)
        if isinstance(readings, list):
            out["readingCount"] = len(readings)
    return out


def spo2(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return clean(raw)
    hourly_avg = series(
        raw.get("spO2HourlyAverages"),
        raw.get("spO2HourlyAveragesDescriptorList"),
        index_key="spo2ValueDescIndex",
        name_key="spo2ValueDescKey",
    )
    skip = {
        "spO2HourlyAverages",
        "spO2HourlyAveragesDescriptorList",
        "spO2SingleValues",
        "spO2SingleValuesDescriptorList",
        "monitoringEnvironmentValues",
        "monitoringEnvironmentValuesDescriptorList",
    }
    out: dict[str, Any] = clean({k: v for k, v in raw.items() if k not in skip})
    out["hourlyAverages"] = hourly_avg
    return out


def body_battery_day(raw: Any) -> Any:
    if not isinstance(raw, dict):
        return clean(raw)
    values = series(
        raw.get("bodyBatteryValuesArray"),
        raw.get("bodyBatteryValueDescriptorDTOList"),
        index_key="bodyBatteryValueDescriptorIndex",
        name_key="bodyBatteryValueDescriptorKey",
    )
    skip = {"bodyBatteryValuesArray", "bodyBatteryValueDescriptorDTOList"}
    out: dict[str, Any] = clean({k: v for k, v in raw.items() if k not in skip})
    out["levels"] = values
    return out


def daily_summary(raw: Any) -> Any:
    day = clean(raw)
    if not isinstance(day, dict):
        return day
    for key in ("source", "includesWellnessData", "includesActivityData", "wellnessChronology"):
        day.pop(key, None)
    battery = day.get("bodyBattery")
    if isinstance(battery, dict):
        for key in ("activityEvents", "dynamicFeedbackEvent", "featureVersion"):
            battery.pop(key, None)
    return day


_ACTIVITY_KEYS = (
    "activityId",
    "activityName",
    "startTime",
    "endTime",
    "distance",
    "duration",
    "movingDuration",
    "elapsedDuration",
    "averageSpeed",
    "calories",
    "averageHR",
    "maxHR",
    "steps",
    "aerobicTrainingEffect",
    "anaerobicTrainingEffect",
    "trainingEffectLabel",
    "activityTrainingLoad",
    "moderateIntensityMinutes",
    "vigorousIntensityMinutes",
    "differenceBodyBattery",
    "lapCount",
    "totalSets",
    "totalReps",
    "summarizedExerciseSets",
    "manufacturer",
    "locationName",
)


# Zero in these means "not measured for this kind of activity" (e.g. distance for strength).
_ZERO_MEANS_MISSING = frozenset(
    {"distance", "averageSpeed", "averageMovingSpeed", "steps", "totalReps", "totalExerciseReps"}
)


def _exercise_sets(sets: Any) -> list[dict[str, Any]]:
    """Drop placeholder sets (category UNKNOWN, nothing counted); duration ms -> seconds."""
    out: list[dict[str, Any]] = []
    for entry in sets if isinstance(sets, list) else []:
        if not isinstance(entry, dict):
            continue
        counted = any(entry.get(k) for k in ("reps", "volume", "maxWeight"))
        if entry.get("category") == "UNKNOWN" and not counted:
            continue
        tidy = {k: v for k, v in entry.items() if k != "duration" and v != 0}
        if isinstance(entry.get("duration"), int | float) and entry["duration"]:
            tidy["durationSeconds"] = round(entry["duration"] / 1000)
        out.append(tidy)
    return out


def _drop_unmeasured(item: dict[str, Any]) -> None:
    for key in _ZERO_MEANS_MISSING:
        if item.get(key) == 0:
            del item[key]
    if "summarizedExerciseSets" in item:
        sets = _exercise_sets(item.pop("summarizedExerciseSets"))
        if sets:
            item["summarizedExerciseSets"] = sets
        else:  # only placeholder sets: the set/rep totals are placeholders too
            for key in ("totalSets", "activeSets", "totalReps"):
                item.pop(key, None)


def activity_compact(raw: Any) -> dict[str, Any]:
    item = clean(raw)
    if not isinstance(item, dict):
        return {}
    _drop_unmeasured(item)
    out: dict[str, Any] = {k: item[k] for k in _ACTIVITY_KEYS if k in item}
    activity_type = item.get("activityType")
    if isinstance(activity_type, dict):
        out["activityType"] = activity_type.get("typeKey")
    zones = {
        f"zone{i}": item[f"hrTimeInZone_{i}"] for i in range(1, 6) if f"hrTimeInZone_{i}" in item
    }
    if zones:
        out["hrTimeInZoneSeconds"] = zones
    if item.get("pr") or item.get("isPR"):
        out["personalRecord"] = True
    if item.get("manualActivity"):
        out["manualActivity"] = True
    return out


def activity_detail(raw: Any) -> Any:
    item = clean(raw)
    if not isinstance(item, dict):
        return item
    metadata = item.pop("metadataDTO", {})
    if isinstance(metadata, dict):
        keep = ("lapCount", "manufacturer", "manualActivity", "personalRecord", "isOriginal")
        item["metadata"] = {k: metadata[k] for k in keep if k in metadata}
        file_format = metadata.get("fileFormat")
        if isinstance(file_format, dict) and "formatKey" in file_format:
            item["metadata"]["fileFormat"] = file_format["formatKey"]
    for key, inner in (("activityTypeDTO", "activityType"), ("eventTypeDTO", "eventType")):
        value = item.pop(key, None)
        if isinstance(value, dict):
            item[inner] = value.get("typeKey")
    zone = item.pop("timeZoneUnitDTO", None)
    if isinstance(zone, dict):
        item["timeZone"] = zone.get("timeZone")
    summary = item.get("summaryDTO")
    if isinstance(summary, dict):
        _drop_unmeasured(summary)
    return item


def profile(raw: Any) -> dict[str, Any]:
    item = raw if isinstance(raw, dict) else {}
    out: dict[str, Any] = {
        k: item[k]
        for k in ("measurementSystem", "preferredLocale", "timeZone", "hydrationMeasurementUnit")
        if item.get(k)
    }
    first_day = item.get("firstDayOfWeek")
    if isinstance(first_day, dict) and first_day.get("dayName"):
        out["firstDayOfWeek"] = first_day["dayName"]
    for key in ("timeFormat", "dateFormat"):
        fmt = item.get(key)
        if isinstance(fmt, dict) and fmt.get("formatKey"):
            out[key] = fmt["formatKey"]
    return out
