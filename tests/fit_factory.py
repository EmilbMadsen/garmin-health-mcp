"""Build small synthetic FIT activities with Garmin's encoder. No real data involved.

``synthetic_run()`` is a 2.5 km run at a steady 4:00 /km (1 record per second):

- 0-300 s: 125 bpm, 300-600 s: 145 bpm, then 165 bpm
- a 60 s pause (timer stop/start) at 400 s, while the watch keeps recording at the same spot
- altitude climbs 10 m over the first km and drops 10 m over the second
- two laps (1.5 km and 1.0 km) and a session time-in-zone message
"""

from __future__ import annotations

import io
import zipfile
from datetime import UTC, datetime, timedelta
from typing import Any

from garmin_fit_sdk import Encoder, Profile

START = datetime(2024, 1, 15, 6, 0, 0, tzinfo=UTC)  # 07:00 in Copenhagen
SPEED = 1000 / 240  # 4:00 /km
PAUSE_AT, PAUSE_SECONDS = 400, 60
DISTANCE = 2500.0
ZONE_BOUNDS = [110, 130, 150, 160, 170, 190]  # floors z1-z5, then max


def _hr(moving_seconds: float) -> int:
    if moving_seconds < 300:
        return 125
    if moving_seconds < 600:
        return 145
    return 165


def _altitude(distance: float) -> float:
    return 50 + (distance / 100 if distance <= 1000 else max(0.0, 10 - (distance - 1000) / 100))


def synthetic_run() -> bytes:
    mesg = Profile["mesg_num"]
    encoder = Encoder()
    encoder.on_mesg(
        mesg["FILE_ID"],
        {"type": "activity", "manufacturer": "development", "product": 0, "time_created": START},
    )
    encoder.on_mesg(mesg["EVENT"], {"timestamp": START, "event": "timer", "event_type": "start"})

    records: list[dict[str, Any]] = []
    moving = 0.0
    clock = 0
    while True:
        distance = min(DISTANCE, moving * SPEED)
        stamp = START + timedelta(seconds=clock)
        records.append(
            {
                "timestamp": stamp,
                "distance": distance,
                "enhanced_speed": SPEED,
                "heart_rate": _hr(moving),
                "cadence": 85,
                "fractional_cadence": 0.5,
                "power": 300,
                "enhanced_altitude": _altitude(distance),
            }
        )
        if distance >= DISTANCE:
            break
        if clock == PAUSE_AT:
            encoder.on_mesg(
                mesg["EVENT"], {"timestamp": stamp, "event": "timer", "event_type": "stop_all"}
            )
            for _ in range(PAUSE_SECONDS):  # watch keeps recording, timer stopped
                clock += 1
                records.append({**records[-1], "timestamp": START + timedelta(seconds=clock)})
            encoder.on_mesg(
                mesg["EVENT"],
                {
                    "timestamp": START + timedelta(seconds=clock),
                    "event": "timer",
                    "event_type": "start",
                },
            )
        clock += 1
        moving += 1
    for record in records:
        encoder.on_mesg(mesg["RECORD"], record)

    end = records[-1]["timestamp"]
    timer = moving
    elapsed = float(clock)
    lap_split = START + timedelta(seconds=360)  # 1.5 km at 4:00 /km
    laps = [
        (START, 1500.0, 360.0, 360.0, 130, 145),
        (
            lap_split + timedelta(seconds=PAUSE_SECONDS),
            1000.0,
            timer - 360,
            elapsed - 360,
            160,
            165,
        ),
    ]
    for index, (start, dist, lap_timer, lap_elapsed, avg_hr, max_hr) in enumerate(laps):
        encoder.on_mesg(
            mesg["LAP"],
            {
                "message_index": index,
                "timestamp": start + timedelta(seconds=lap_elapsed),
                "start_time": start,
                "total_distance": dist,
                "total_timer_time": lap_timer,
                "total_elapsed_time": lap_elapsed,
                "enhanced_avg_speed": SPEED,
                "avg_heart_rate": avg_hr,
                "max_heart_rate": max_hr,
                "avg_cadence": 85,
                "avg_fractional_cadence": 0.5,
                "avg_power": 300,
                "intensity": "active",
                "lap_trigger": "distance" if index == 0 else "session_end",
                "sport": "running",
                # GPS fields that must never reach a tool response
                "start_position_lat": 663_000_000,
                "start_position_long": 148_000_000,
            },
        )
    encoder.on_mesg(
        mesg["TIME_IN_ZONE"],
        {
            "timestamp": end,
            "reference_mesg": "session",
            "reference_index": 0,
            "hr_zone_high_boundary": ZONE_BOUNDS,
            "time_in_hr_zone": [0.0, 0.0, 300.0, 300.0, 0.0, timer - 600, 0.0],
        },
    )
    encoder.on_mesg(
        mesg["SESSION"],
        {
            "message_index": 0,
            "timestamp": end,
            "start_time": START,
            "sport": "running",
            "sub_sport": "generic",
            "total_distance": DISTANCE,
            "total_timer_time": timer,
            "total_elapsed_time": elapsed,
            "enhanced_avg_speed": SPEED,
            "enhanced_max_speed": SPEED * 1.1,
            "avg_heart_rate": 148,
            "max_heart_rate": 165,
            "avg_cadence": 85,
            "avg_fractional_cadence": 0.5,
            "max_cadence": 90,
            "avg_power": 300,
            "total_ascent": 10,
            "total_descent": 10,
            "total_calories": 180,
            "total_training_effect": 3.1,
            "total_anaerobic_training_effect": 1.2,
            "avg_stance_time": 240.0,
            "avg_vertical_oscillation": 85.0,
            "avg_step_length": 1460.0,
            "first_lap_index": 0,
            "num_laps": 2,
            "start_position_lat": 663_000_000,
        },
    )
    encoder.on_mesg(
        mesg["ACTIVITY"],
        {"timestamp": end, "num_sessions": 1, "type": "manual", "total_timer_time": timer},
    )
    return bytes(encoder.close())


def synthetic_strength() -> bytes:
    """Ten minutes of heart rate only, no distance."""
    mesg = Profile["mesg_num"]
    encoder = Encoder()
    encoder.on_mesg(
        mesg["FILE_ID"],
        {"type": "activity", "manufacturer": "development", "product": 0, "time_created": START},
    )
    for second in range(600):
        encoder.on_mesg(
            mesg["RECORD"],
            {"timestamp": START + timedelta(seconds=second), "heart_rate": 100 + second // 60},
        )
    encoder.on_mesg(
        mesg["SESSION"],
        {
            "timestamp": START + timedelta(seconds=599),
            "start_time": START,
            "sport": "training",
            "sub_sport": "strength_training",
            "total_timer_time": 599.0,
            "total_elapsed_time": 599.0,
            "total_distance": 0.0,
            "avg_heart_rate": 105,
        },
    )
    return bytes(encoder.close())


def zipped(fit_bytes: bytes, name: str = "1001_ACTIVITY.fit") -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, fit_bytes)
    return buffer.getvalue()
