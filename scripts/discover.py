"""Phase 0 discovery: call every Personal API endpoint once and keep the raw JSON.

Raw responses go to ``dumps/<timestamp>/`` (git-ignored, health data). The terminal and
``dumps/<timestamp>/_status.json`` only show endpoint, HTTP status, duration, size and the shape
of the response (top-level keys / list length), never values. Run ``scripts/anonymize.py``
before anything from a dump becomes a test fixture.

    uv run python scripts/discover.py
    uv run python scripts/discover.py --days 7 --no-download
    uv run python scripts/discover.py --cycle            # also the menstrual-cycle endpoints

Calls are spaced out (``--pause``) and never retried, to stay well clear of throttling.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from garmin_personal_api import GarminPersonalApiClient, GarminPersonalApiError

from garmin_health_mcp.client import describe_error, open_client, status_code
from garmin_health_mcp.credentials import CredentialsMissingError

DUMPS = Path(__file__).resolve().parent.parent / "dumps"

Call = Callable[[GarminPersonalApiClient], Any]


def endpoints(day: date, start: date, end: date, *, cycle: bool) -> list[tuple[str, Call]]:
    calls: list[tuple[str, Call]] = [
        ("user_profile_settings", lambda c: c.get_user_profile_settings()),
        ("daily_sleep", lambda c: c.get_daily_sleep(day)),
        ("daily_sleep_summaries", lambda c: c.get_daily_sleep_summaries(start, end)),
        ("daily_hrv", lambda c: c.get_daily_hrv(day)),
        ("daily_hrv_summaries", lambda c: c.get_daily_hrv_summaries(start, end)),
        ("daily_body_battery", lambda c: c.get_daily_body_battery(start, end)),
        ("daily_stress", lambda c: c.get_daily_stress(start, end)),
        ("daily_steps", lambda c: c.get_daily_steps(start, end)),
        ("daily_summaries", lambda c: c.get_daily_summaries(start, end)),
        ("daily_heart_rate", lambda c: c.get_daily_heart_rate(day)),
        ("heart_rate_zones", lambda c: c.get_heart_rate_zones()),
        ("daily_spo2", lambda c: c.get_daily_spo2(day)),
        ("fitness_age", lambda c: c.get_fitness_age(day)),
        ("weight_measurements", lambda c: c.get_weight_measurements(start, end)),
        ("effective_weight_goals", lambda c: c.get_effective_weight_goals(start, end)),
        ("daily_hydration", lambda c: c.get_daily_hydration(start, end)),
        ("daily_food_logs", lambda c: c.get_daily_food_logs(day)),
        (
            "wellness_activity_summaries",
            lambda c: c.get_wellness_activity_summaries(start, end),
        ),
        ("search_activities", lambda c: c.search_activities(limit=10)),
    ]
    if cycle:
        calls += [
            ("menstrual_cycle_summary", lambda c: c.get_menstrual_cycle_summary(day)),
            ("menstrual_cycle_day_view", lambda c: c.get_menstrual_cycle_day_view(day, day)),
        ]
    return calls


def shape(value: Any) -> str:
    """Describe the structure of a response without revealing any values."""
    if value is None:
        return "empty"
    if isinstance(value, list):
        inner = f" of {shape(value[0])}" if value else ""
        return f"list[{len(value)}]{inner}"
    if isinstance(value, dict):
        keys = sorted(value)
        more = f", +{len(keys) - 12}" if len(keys) > 12 else ""
        return "{" + ", ".join(keys[:12]) + more + "}"
    return type(value).__name__


def first_activity_id(activities: Any) -> int | None:
    items = activities if isinstance(activities, list) else []
    if isinstance(activities, dict):
        for key in ("activityList", "activities", "data"):
            if isinstance(activities.get(key), list):
                items = activities[key]
                break
    for item in items:
        if isinstance(item, dict) and isinstance(item.get("activityId"), int):
            return int(item["activityId"])
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7, help="range length for range endpoints")
    parser.add_argument("--pause", type=float, default=1.0, help="seconds between calls")
    parser.add_argument("--no-download", action="store_true", help="skip the activity ZIP")
    parser.add_argument("--cycle", action="store_true", help="include menstrual-cycle endpoints")
    args = parser.parse_args()

    day = date.today() - timedelta(days=1)
    start, end = day - timedelta(days=args.days - 1), day
    out = DUMPS / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out.mkdir(parents=True, exist_ok=True, mode=0o700)

    try:
        client = open_client()
    except CredentialsMissingError as exc:
        print(exc, file=sys.stderr)
        return 1
    except GarminPersonalApiError as exc:
        print(f"Token exchange failed: {describe_error(exc)}", file=sys.stderr)
        return 1

    print(f"Single day: {day}   range: {start}..{end}   output: {out}\n")
    status: list[dict[str, Any]] = []

    def record(name: str, call: Call) -> Any:
        started = time.monotonic()
        entry: dict[str, Any] = {"endpoint": name}
        result: Any = None
        try:
            result = call(client)
            entry["status"] = 200
            if isinstance(result, bytes):
                (out / f"{name}.zip").write_bytes(result)
                entry["bytes"] = len(result)
                entry["shape"] = "zip"
            else:
                text = json.dumps(result, indent=2, ensure_ascii=False)
                (out / f"{name}.json").write_text(text, encoding="utf-8")
                entry["bytes"] = len(text.encode())
                entry["shape"] = shape(result)
        except GarminPersonalApiError as exc:
            entry["status"] = status_code(exc) or "error"
            entry["error"] = describe_error(exc)
        entry["seconds"] = round(time.monotonic() - started, 2)
        status.append(entry)
        print(
            f"{entry['status']!s:>5}  {name:<30} {entry['seconds']:>5.2f}s  "
            f"{entry.get('bytes', '-')!s:>8} B  {entry.get('shape', entry.get('error', ''))}"
        )
        time.sleep(args.pause)
        return result

    with client:
        activities: Any = None
        for name, call in endpoints(day, start, end, cycle=args.cycle):
            result = record(name, call)
            if name == "search_activities":
                activities = result

        activity_id = first_activity_id(activities)
        if activity_id is None:
            print("\nNo activity found; skipping the per-activity endpoints.")
        else:
            print("\nUsing the most recent activity for per-activity endpoints.")
            record("activity", lambda c: c.get_activity(activity_id))
            record("activity_workouts", lambda c: c.get_activity_workouts(activity_id))
            if not args.no_download:
                record("activity_download", lambda c: c.download_activity(activity_id))

    (out / "_status.json").write_text(json.dumps(status, indent=2), encoding="utf-8")
    ok = sum(1 for s in status if s["status"] == 200)
    print(f"\n{ok}/{len(status)} calls succeeded. Raw data in {out} (git-ignored).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
