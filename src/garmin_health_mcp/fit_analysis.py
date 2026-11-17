"""Activity analysis from the original FIT file (Garmin's official ``garmin-fit-sdk``).

The FIT file is downloaded once per activity as a ZIP, the ``.fit`` inside is cached in
``<state dir>/fit/<activity_id>.fit`` (0600; it is health data) and decoded in memory.

Everything returned is a summary. Raw record streams and GPS positions are never returned.
Units: distance m, time s, speed m/s, pace s/km (plus a ``m:ss`` string), heart rate bpm,
cadence steps/min for running (cycles/min otherwise), power W, stance time ms, vertical
oscillation cm, step length m.
"""

from __future__ import annotations

import io
import itertools
import os
import zipfile
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from garmin_fit_sdk import Decoder, Stream

from . import presentation as p

MAX_SPLITS = 60  # keeps a response under ~20 kB
FOOT_SPORTS = frozenset({"running", "walking", "hiking"})
ELEVATION_THRESHOLD_M = 1.5


class FitError(Exception):
    """A FIT file could not be read or does not contain what was asked for."""


# -- file handling ----------------------------------------------------------------------------


def extract_fit(zip_bytes: bytes) -> bytes:
    """Return the first ``.fit`` member of Garmin's activity ZIP."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
            for name in archive.namelist():
                if name.lower().endswith(".fit"):
                    return archive.read(name)
    except zipfile.BadZipFile as exc:
        raise FitError("The activity download is not a valid ZIP file.") from exc
    raise FitError("The activity download contains no FIT file (it may be a GPX/TCX upload).")


class FitCache:
    """Per-activity FIT files on disk in a private directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def path(self, activity_id: int) -> Path:
        return self.directory / f"{activity_id}.fit"

    def get(self, activity_id: int) -> bytes | None:
        try:
            return self.path(activity_id).read_bytes()
        except FileNotFoundError:
            return None

    def put(self, activity_id: int, data: bytes) -> None:
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if os.name == "posix":
            self.directory.chmod(0o700)
        target = self.path(activity_id)
        temporary = target.with_suffix(".tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        temporary.replace(target)


@dataclass
class Activity:
    """The decoded messages of one FIT file that the analysis needs."""

    session: dict[str, Any]
    laps: list[dict[str, Any]]
    records: list[dict[str, Any]]
    events: list[dict[str, Any]]
    time_in_zone: list[dict[str, Any]]
    sets: list[dict[str, Any]]
    message_counts: dict[str, int]

    @property
    def sport(self) -> str:
        return str(self.session.get("sport", "unknown"))


def decode(data: bytes) -> Activity:
    try:
        decoded, errors = Decoder(Stream.from_byte_array(bytearray(data))).read()
    except Exception as exc:  # the SDK raises plain exceptions on corrupt input
        raise FitError(f"The FIT file could not be decoded ({type(exc).__name__}).") from exc
    if errors:
        raise FitError(f"The FIT file could not be decoded ({len(errors)} errors).")
    messages: dict[Any, Any] = dict(decoded)
    sessions = messages.get("session_mesgs") or [{}]
    return Activity(
        session=sessions[0],
        laps=messages.get("lap_mesgs", []),
        records=[r for r in messages.get("record_mesgs", []) if "timestamp" in r],
        events=messages.get("event_mesgs", []),
        time_in_zone=messages.get("time_in_zone_mesgs", []),
        sets=messages.get("set_mesgs", []),
        message_counts={
            k.removesuffix("_mesgs"): len(v)
            for k, v in messages.items()
            if isinstance(k, str) and v
        },
    )


# -- formatting -------------------------------------------------------------------------------


def _iso(value: Any) -> Any:
    return p.to_local_iso(value.timestamp() * 1000) if isinstance(value, datetime) else value


def _num(value: Any) -> Any:
    if isinstance(value, float):
        return p.tidy_number(value)
    return value


def pace(speed: Any) -> dict[str, Any]:
    """Speed in m/s -> pace in s/km and as ``m:ss``. Empty for no movement."""
    if not isinstance(speed, int | float) or speed <= 0.1:
        return {}
    seconds = round(1000 / speed)
    return {"paceSecondsPerKm": seconds, "pace": f"{seconds // 60}:{seconds % 60:02d} /km"}


def _cadence(message: dict[str, Any], sport: str, *, avg: bool) -> Any:
    """FIT stores running cadence per foot (strides/min); report steps/min for foot sports."""
    prefix = "avg" if avg else "max"
    base = message.get(f"{prefix}_cadence")
    if not isinstance(base, int | float):
        return None
    value = base + (message.get(f"{prefix}_fractional_cadence") or 0)
    return round(value * 2 if sport in FOOT_SPORTS else value)


def _clean(out: dict[str, Any]) -> dict[str, Any]:
    return {k: _num(v) for k, v in out.items() if v is not None and v != {} and v != []}


# -- laps ---------------------------------------------------------------------------------------


def laps(activity: Activity) -> list[dict[str, Any]]:
    out = []
    for number, lap in enumerate(activity.laps, start=1):
        speed = lap.get("enhanced_avg_speed", lap.get("avg_speed"))
        row: dict[str, Any] = {
            "lap": number,
            "startTime": _iso(lap.get("start_time")),
            "distance": lap.get("total_distance"),
            "timerTime": lap.get("total_timer_time"),
            "elapsedTime": lap.get("total_elapsed_time"),
            **pace(speed if activity.sport in FOOT_SPORTS else None),
            "avgSpeed": speed if activity.sport not in FOOT_SPORTS else None,
            "avgGradeAdjustedPace": pace(lap.get("avg_grade_adjusted_speed")).get("pace"),
            "avgHeartRate": lap.get("avg_heart_rate"),
            "maxHeartRate": lap.get("max_heart_rate"),
            "avgCadence": _cadence(lap, activity.sport, avg=True),
            "avgPower": lap.get("avg_power"),
            "normalizedPower": lap.get("normalized_power"),
            "ascent": lap.get("total_ascent"),
            "descent": lap.get("total_descent"),
            "intensity": lap.get("intensity"),
            "trigger": lap.get("lap_trigger"),
        }
        out.append(_clean(row))
    return out


# -- records: timer-aware time ----------------------------------------------------------------


def _paused_ranges(events: Iterable[dict[str, Any]]) -> list[tuple[datetime, datetime | None]]:
    """Ranges between a timer stop and the next timer start."""
    ranges: list[tuple[datetime, datetime | None]] = []
    stopped: datetime | None = None
    timer = sorted(
        (
            e
            for e in events
            if e.get("event") == "timer" and isinstance(e.get("timestamp"), datetime)
        ),
        key=lambda e: e["timestamp"],
    )
    for event in timer:
        kind = str(event.get("event_type", ""))
        if kind.startswith("stop") and stopped is None:
            stopped = event["timestamp"]
        elif kind == "start" and stopped is not None:
            ranges.append((stopped, event["timestamp"]))
            stopped = None
    if stopped is not None:
        ranges.append((stopped, None))
    return ranges


def _active_deltas(records: list[dict[str, Any]], events: Iterable[dict[str, Any]]) -> list[float]:
    """Seconds between each record and the previous one; 0 if the timer was paused between."""
    paused = _paused_ranges(events)
    deltas = [0.0]
    for previous, current in itertools.pairwise(records):
        start, end = previous["timestamp"], current["timestamp"]
        across_pause = any(s < end and (e is None or e > start) for s, e in paused)
        deltas.append(0.0 if across_pause else max(0.0, (end - start).total_seconds()))
    return deltas


def _weighted(values: list[tuple[float, float]]) -> float | None:
    total = sum(w for _, w in values)
    return sum(v * w for v, w in values) / total if total else None


# -- splits -------------------------------------------------------------------------------------


@dataclass
class _Bucket:
    distance_start: float
    time: float = 0.0
    hr: list[tuple[float, float]] | None = None
    max_hr: int | None = None
    cadence: list[tuple[float, float]] | None = None
    power: list[tuple[float, float]] | None = None
    ascent: float = 0.0
    descent: float = 0.0


def splits(activity: Activity, split_meters: float = 1000.0) -> list[dict[str, Any]]:
    if split_meters < 100:
        raise FitError("split_meters must be at least 100.")
    rows = [r for r in activity.records if isinstance(r.get("distance"), int | float)]
    if len(rows) < 2 or rows[-1]["distance"] - rows[0]["distance"] < 1:
        raise FitError("This activity has no distance data, so it cannot be split by distance.")
    total = rows[-1]["distance"] - rows[0]["distance"]
    if total / split_meters > MAX_SPLITS:
        raise FitError(f"That would be more than {MAX_SPLITS} splits; use a longer split.")

    deltas = _active_deltas(rows, activity.events)
    foot = activity.sport in FOOT_SPORTS
    origin = rows[0]["distance"]
    buckets: list[_Bucket] = [_Bucket(0.0, hr=[], cadence=[], power=[])]
    last_altitude: float | None = None
    for record, delta in zip(rows, deltas, strict=True):
        travelled = record["distance"] - origin
        while travelled > buckets[-1].distance_start + split_meters:
            start = buckets[-1].distance_start + split_meters
            buckets.append(_Bucket(start, hr=[], cadence=[], power=[]))
        bucket = buckets[-1]
        bucket.time += delta
        hr = record.get("heart_rate")
        if isinstance(hr, int) and hr > 0:
            assert bucket.hr is not None
            bucket.hr.append((hr, delta or 1.0))
            bucket.max_hr = max(bucket.max_hr or 0, hr)
        cadence = record.get("cadence")
        if isinstance(cadence, int) and cadence > 0 and delta:
            assert bucket.cadence is not None
            value = cadence + (record.get("fractional_cadence") or 0)
            bucket.cadence.append((value * 2 if foot else value, delta))
        power = record.get("power")
        if isinstance(power, int) and delta:
            assert bucket.power is not None
            bucket.power.append((power, delta))
        altitude = record.get("enhanced_altitude", record.get("altitude"))
        if isinstance(altitude, int | float):
            # Hysteresis: count a climb or descent only once it exceeds the threshold, so
            # barometer noise does not add up (matches Garmin's own totals closely).
            if last_altitude is None:
                last_altitude = altitude
            elif altitude - last_altitude >= ELEVATION_THRESHOLD_M:
                bucket.ascent += altitude - last_altitude
                last_altitude = altitude
            elif last_altitude - altitude >= ELEVATION_THRESHOLD_M:
                bucket.descent += last_altitude - altitude
                last_altitude = altitude

    out = []
    for number, bucket in enumerate(buckets, start=1):
        distance = min(split_meters, total - bucket.distance_start)
        if distance <= 0:
            continue
        speed = distance / bucket.time if bucket.time else None
        avg_hr = _weighted(bucket.hr or [])
        avg_cadence = _weighted(bucket.cadence or [])
        avg_power = _weighted(bucket.power or [])
        row: dict[str, Any] = {
            "split": number,
            "distance": round(distance, 1),
            "time": round(bucket.time, 1),
            **(pace(speed) if foot else {"avgSpeed": speed}),
            "avgHeartRate": round(avg_hr) if avg_hr else None,
            "maxHeartRate": bucket.max_hr,
            "avgCadence": round(avg_cadence) if avg_cadence else None,
            "avgPower": round(avg_power) if avg_power else None,
            "elevationGain": round(bucket.ascent, 1) if last_altitude is not None else None,
            "elevationLoss": round(bucket.descent, 1) if last_altitude is not None else None,
        }
        out.append(_clean(row))
    return out


# -- heart rate zones -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Zones:
    """Zone floors 1-5 plus max HR, and where they came from."""

    floors: tuple[int, int, int, int, int]
    max_hr: int
    source: str


def _five(values: Iterable[int]) -> tuple[int, int, int, int, int]:
    a, b, c, d, e = values
    return (a, b, c, d, e)


def zones_from_api(raw: Any, sport: str) -> Zones | None:
    """Pick the sport's zones from get_heart_rate_zones, else the DEFAULT entry."""
    entries = [z for z in raw if isinstance(z, dict)] if isinstance(raw, list) else []
    wanted = sport.upper()
    chosen = next((z for z in entries if z.get("sport") == wanted), None) or next(
        (z for z in entries if z.get("sport") == "DEFAULT"), None
    )
    if chosen is None:
        return None
    try:
        floors = _five(int(chosen[f"zone{i}Floor"]) for i in range(1, 6))
        source = f"current Garmin zones ({chosen['sport']})"
        return Zones(floors, int(chosen["maxHeartRateUsed"]), source)
    except (KeyError, TypeError, ValueError):
        return None


def zones_from_fit(activity: Activity) -> Zones | None:
    """Zones the watch used when recording (session time_in_zone message)."""
    for message in activity.time_in_zone:
        bounds = message.get("hr_zone_high_boundary")
        if (
            message.get("reference_mesg") == "session"
            and isinstance(bounds, list)
            and len(bounds) >= 6
        ):
            source = "zones on the watch during the activity"
            return Zones(_five(int(b) for b in bounds[:5]), int(bounds[5]), source)
    return None


def device_time_in_zones(activity: Activity) -> list[float] | None:
    for message in activity.time_in_zone:
        times = message.get("time_in_hr_zone")
        is_session = message.get("reference_mesg") == "session"
        if is_session and isinstance(times, list) and len(times) >= 6:
            return [float(t) for t in times]
    return None


def time_in_zones(activity: Activity, zones: Zones) -> dict[str, Any]:
    """Seconds below zone 1, in zones 1-5 (zone 5 includes everything above max)."""
    deltas = _active_deltas(activity.records, activity.events)
    seconds = [0.0] * 6  # index 0 = below zone 1
    counted = 0.0
    for record, delta in zip(activity.records, deltas, strict=True):
        hr = record.get("heart_rate")
        if not isinstance(hr, int) or hr <= 0 or not delta:
            continue
        index = sum(1 for floor in zones.floors if hr >= floor)
        seconds[index] += delta
        counted += delta
    if not counted:
        raise FitError("This activity has no heart rate data.")
    labels = ["belowZone1", "zone1", "zone2", "zone3", "zone4", "zone5"]
    ranges = [f"<{zones.floors[0]}"] + [
        f"{zones.floors[i]}-{(zones.floors[i + 1] - 1) if i < 4 else zones.max_hr}"
        for i in range(5)
    ]
    return {
        label: {"bpm": bpm, "seconds": round(s), "percent": round(100 * s / counted, 1)}
        for label, bpm, s in zip(labels, ranges, seconds, strict=True)
    }


# -- summary ------------------------------------------------------------------------------------

_RECORD_FIELD_NAMES = {
    "heart_rate": "heart rate",
    "distance": "distance",
    "enhanced_speed": "speed",
    "speed": "speed",
    "enhanced_altitude": "altitude",
    "altitude": "altitude",
    "cadence": "cadence",
    "power": "power",
    "stance_time": "ground contact time",
    "vertical_oscillation": "vertical oscillation",
    "vertical_ratio": "vertical ratio",
    "step_length": "step length",
    "enhanced_respiration_rate": "respiration rate",
    "temperature": "temperature",
    "position_lat": "GPS track (not returned)",
}


def summary_stats(activity: Activity) -> dict[str, Any]:
    s = activity.session
    foot = activity.sport in FOOT_SPORTS
    speed = s.get("enhanced_avg_speed", s.get("avg_speed"))
    max_speed = s.get("enhanced_max_speed", s.get("max_speed"))
    oscillation = s.get("avg_vertical_oscillation")
    step = s.get("avg_step_length")
    out: dict[str, Any] = {
        "sport": activity.sport,
        "subSport": s.get("sub_sport"),
        "startTime": _iso(s.get("start_time")),
        "distance": s.get("total_distance") or None,
        "timerTime": s.get("total_timer_time"),
        "elapsedTime": s.get("total_elapsed_time"),
        **(pace(speed) if foot else {"avgSpeed": speed}),
        "bestPace": pace(max_speed).get("pace") if foot else None,
        "maxSpeed": max_speed if not foot else None,
        "avgGradeAdjustedPace": pace(s.get("avg_grade_adjusted_speed")).get("pace"),
        "avgHeartRate": s.get("avg_heart_rate"),
        "maxHeartRate": s.get("max_heart_rate"),
        "avgCadence": _cadence(s, activity.sport, avg=True),
        "maxCadence": _cadence(s, activity.sport, avg=False),
        "avgPower": s.get("avg_power"),
        "maxPower": s.get("max_power"),
        "normalizedPower": s.get("normalized_power"),
        "ascent": s.get("total_ascent"),
        "descent": s.get("total_descent"),
        "calories": s.get("total_calories"),
        "aerobicTrainingEffect": s.get("total_training_effect"),
        "anaerobicTrainingEffect": s.get("total_anaerobic_training_effect"),
        "trainingLoadPeak": s.get("training_load_peak"),
        "runningDynamics": _clean(
            {
                "groundContactTimeMs": s.get("avg_stance_time"),
                "groundContactBalanceLeftPercent": s.get("avg_stance_time_balance"),
                "verticalOscillationCm": oscillation / 10 if oscillation else None,
                "verticalRatioPercent": s.get("avg_vertical_ratio"),
                "stepLengthM": step / 1000 if step else None,
            }
        ),
        "respirationRate": _clean(
            {
                "avg": s.get("enhanced_avg_respiration_rate"),
                "min": s.get("enhanced_min_respiration_rate"),
                "max": s.get("enhanced_max_respiration_rate"),
            }
        ),
        "workoutFeel": s.get("workout_feel"),
        "workoutRpe": rpe / 10 if isinstance(rpe := s.get("workout_rpe"), int | float) else None,
        "totalReps": s.get("total_reps"),
        "lapCount": len(activity.laps),
        "setCount": len(activity.sets) or None,
        "recordedData": sorted(
            {
                name
                for record in activity.records[:50]
                for key, name in _RECORD_FIELD_NAMES.items()
                if key in record
            }
        ),
    }
    return _clean(out)
