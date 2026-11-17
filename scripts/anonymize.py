"""Turn a discovery dump into synthetic test fixtures that are safe to commit.

Real health data must never reach git, so this does more than strip identifiers. It keeps the
*structure* of every response and makes the *values* synthetic:

- names, display names, location names, GPS coordinates, image URLs, UUIDs and
  profile/device/owner IDs -> fixed placeholders
- activity IDs -> 1001, 1002, ... (consistently across files)
- every date -> shifted by the same whole number of weeks into January 2024; every timestamp
  additionally by a fixed odd offset, so real times of day (bedtimes, workouts) do not survive
- age, fitness age, weight, BMI, height, body composition and heart-rate zones -> fixed values
- every other number -> a random value of the same order of magnitude (fixed seed): only the
  number of digits survives, not the value
- long series -> truncated (at most 48 points), search results -> at most 3 activities

Afterwards it fails if any original identifier or ID is still present.

    uv run python scripts/anonymize.py dumps/20261117T105914Z --out tests/fixtures/garmin
"""

from __future__ import annotations

import argparse
import json
import math
import random
import re
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

TARGET_MONDAY = date(2024, 1, 15)
MAX_SERIES = 48
MAX_ACTIVITIES = 3

NAME_KEYS = {
    "displayname",
    "fullname",
    "ownerdisplayname",
    "ownerfullname",
    "activityname",
    "locationname",
}
COORDINATE_KEYS = re.compile(r"(latitude|longitude)$", re.IGNORECASE)
ID_KEYS = {"userprofilepk", "userprofileid", "ownerid", "deviceid", "id"}
UUID_KEYS = {"uuid", "activityuuid"}
# Numbers that are codes, not measurements: keep as they are.
KEEP_NUMBER_KEYS = re.compile(
    r"(index|typeid|parenttypeid|sortorder|formatid|dayid|version|fraction|"
    r"sporttypeid|timezoneoffset|lapcount)",
    re.IGNORECASE,
)
DATE = re.compile(r"\d{4}-\d\d-\d\d")
DATETIME = re.compile(r"^(\d{4}-\d\d-\d\d)([T ])(\d\d:\d\d:\d\d)(\.\d+)?$")
CLOCK_OFFSET = timedelta(hours=2, minutes=37)
# Values that describe the person rather than a day: replaced by fixed, typical numbers.
FIXED_VALUES: dict[str, float] = {
    "chronologicalage": 35,
    "fitnessage": 33.5,
    "previousfitnessage": 34.0,
    "weight": 72500.0,
    "bmi": 23.1,
    "height": 180.0,
    "bodyfat": 18.0,
    "bodywater": 55.0,
    "bonemass": 3200.0,
    "musclemass": 33000.0,
    "visceralfat": 6.0,
    "metabolicage": 33.0,
    "weightdelta": -150.0,
    "zone1floor": 100,
    "zone2floor": 120,
    "zone3floor": 140,
    "zone4floor": 160,
    "zone5floor": 175,
    "maxheartrateused": 190,
    "restingheartrateused": 50,
    "lactatethresholdheartrateused": 168,
}
EPOCH_MS_MIN = 10**12


class Anonymizer:
    def __init__(self, shift: timedelta, seed: int = 42) -> None:
        self.shift = shift
        self.random = random.Random(seed)
        self.activity_ids: dict[int, int] = {}
        self.originals: set[str] = set()

    def activity_id(self, value: int) -> int:
        self.originals.add(str(value))
        return self.activity_ids.setdefault(value, 1001 + len(self.activity_ids))

    def shift_text(self, text: str) -> str:
        full = DATETIME.match(text)
        if full:
            moment = datetime.fromisoformat(f"{full[1]}T{full[3]}") + self.shift
            if full[3] != "00:00:00":  # a bare date sent as midnight stays a date
                moment += CLOCK_OFFSET
            fraction = full[4] or ""
            return moment.strftime(f"%Y-%m-%d{full[2]}%H:%M:%S") + fraction

        def repl(match: re.Match[str]) -> str:
            return (date.fromisoformat(match.group()) + self.shift).isoformat()

        return DATE.sub(repl, text)

    def number(self, key: str, value: int | float) -> int | float:
        lowered = key.lower()
        if lowered in FIXED_VALUES:
            fixed = FIXED_VALUES[lowered]
            return int(fixed) if isinstance(value, int) else float(fixed)
        if KEEP_NUMBER_KEYS.search(key) or value == 0:
            return value
        if isinstance(value, int) and value >= EPOCH_MS_MIN:
            return value + int((self.shift + CLOCK_OFFSET).total_seconds() * 1000)
        magnitude = abs(value)
        if magnitude < 1:
            synthetic = self.random.uniform(0.01, 0.99)
        else:
            digits = math.floor(math.log10(magnitude))
            synthetic = self.random.uniform(10**digits, 10 ** (digits + 1) - 1)
        synthetic = math.copysign(synthetic, value)
        return round(synthetic) if isinstance(value, int) else round(synthetic, 2)

    def value(self, key: str, value: Any) -> Any:
        lowered = key.lower()
        if isinstance(value, dict):
            return {k: self.value(k, v) for k, v in value.items()}
        if isinstance(value, list):
            limit = MAX_ACTIVITIES if lowered == "search_activities" else MAX_SERIES
            return [self.value(key, item) for item in value[:limit]]
        if value is None or isinstance(value, bool):
            return value
        if lowered == "activityid" and isinstance(value, int):
            return self.activity_id(value)
        if lowered in ID_KEYS and isinstance(value, int):
            self.originals.add(str(value))
            return 1
        if lowered in NAME_KEYS and isinstance(value, str):
            self.originals.add(value)
            return {"activityname": "Activity", "locationname": "Example City"}.get(
                lowered, "Example User"
            )
        if COORDINATE_KEYS.search(key) and isinstance(value, int | float):
            return 0.0
        if lowered in UUID_KEYS and isinstance(value, str):
            self.originals.add(value)
            return "00000000-0000-0000-0000-000000000000"
        if "imageurl" in lowered and isinstance(value, str):
            return "https://example.invalid/image.png"
        if isinstance(value, int | float):
            return self.number(key, value)
        if isinstance(value, str):
            return self.shift_text(value)
        return value


def find_dates(data: Any, out: list[date]) -> None:
    if isinstance(data, dict):
        for key, item in data.items():
            if key == "calendarDate" and isinstance(item, str):
                out.append(date.fromisoformat(item))
            find_dates(item, out)
    elif isinstance(data, list):
        for item in data:
            find_dates(item, out)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("source", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()

    files = sorted(f for f in args.source.glob("*.json") if not f.name.startswith("_"))
    raw = {f.stem: json.loads(f.read_text(encoding="utf-8")) for f in files}
    dates: list[date] = []
    find_dates(raw, dates)
    newest = max(dates) if dates else date.today()
    weeks = (TARGET_MONDAY - (newest - timedelta(days=newest.weekday()))).days // 7
    anonymizer = Anonymizer(timedelta(weeks=weeks))

    # Activity IDs first, so the same ID maps the same way in every file.
    for activity in raw.get("search_activities", []):
        if isinstance(activity, dict) and isinstance(activity.get("activityId"), int):
            anonymizer.activity_id(activity["activityId"])

    args.out.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for name, data in raw.items():
        result = anonymizer.value(name, data)
        text = json.dumps(result, indent=2, ensure_ascii=False) + "\n"
        written[name] = text
        (args.out / f"{name}.json").write_text(text, encoding="utf-8")

    leaks = [
        (name, original)
        for name, text in written.items()
        for original in anonymizer.originals
        if len(original) >= 4 and re.search(rf"(?<![\d.]){re.escape(original)}(?![\d.])", text)
    ]
    if leaks:
        for name, _ in leaks:
            print(f"LEAK in {name}.json: an original identifier survived", file=sys.stderr)
        return 1
    print(f"Wrote {len(written)} fixtures to {args.out}; shifted by {weeks} weeks.")
    print(f"Generated at {datetime.now().isoformat(timespec='seconds')}. Review before commit.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
