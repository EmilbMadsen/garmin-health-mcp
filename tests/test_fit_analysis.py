"""FIT analysis on synthetic activities built with Garmin's encoder (see fit_factory)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from garmin_health_mcp import fit_analysis as fit

from .fit_factory import PAUSE_SECONDS, synthetic_run, synthetic_strength, zipped


@pytest.fixture(scope="module")
def run() -> fit.Activity:
    return fit.decode(synthetic_run())


def test_extract_fit_from_zip() -> None:
    data = synthetic_run()
    assert fit.extract_fit(zipped(data)) == data
    with pytest.raises(fit.FitError, match="no FIT"):
        fit.extract_fit(zipped(b"<gpx/>", name="1_ACTIVITY.gpx"))
    with pytest.raises(fit.FitError, match="ZIP"):
        fit.extract_fit(b"not a zip")


def test_corrupt_fit_is_a_clean_error() -> None:
    with pytest.raises(fit.FitError, match="could not be decoded"):
        fit.decode(b"\x0e\x10garbage" * 10)


def test_cache_is_private(tmp_path: Path) -> None:
    cache = fit.FitCache(tmp_path / "fit")
    assert cache.get(1) is None
    cache.put(1, b"abc")
    assert cache.get(1) == b"abc"
    assert stat.S_IMODE(cache.path(1).stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "fit").stat().st_mode) == 0o700


def test_pace() -> None:
    assert fit.pace(1000 / 240) == {"paceSecondsPerKm": 240, "pace": "4:00 /km"}
    assert fit.pace(0) == {}
    assert fit.pace(None) == {}


def test_laps(run: fit.Activity) -> None:
    laps = fit.laps(run)
    assert [lap["distance"] for lap in laps] == [1500.0, 1000.0]
    first, second = laps
    assert first["startTime"] == "2024-01-15T07:00:00+01:00"
    assert first["pace"] == "4:00 /km"
    assert first["avgCadence"] == 171  # 85.5 strides/min per foot -> steps/min
    assert second["elapsedTime"] - second["timerTime"] == PAUSE_SECONDS
    assert "663000000" not in json.dumps(laps) and "position" not in json.dumps(laps)


def test_splits_exclude_the_pause(run: fit.Activity) -> None:
    splits = fit.splits(run)
    assert [s["distance"] for s in splits] == [1000.0, 1000.0, 500.0]
    assert [s["time"] for s in splits] == [240.0, 240.0, 120.0]
    assert {s["pace"] for s in splits} == {"4:00 /km"}
    assert splits[0]["avgHeartRate"] == 125
    assert splits[0]["avgCadence"] == 171
    assert splits[0]["elevationGain"] > 8 and splits[0]["elevationLoss"] == 0
    assert splits[1]["elevationLoss"] > 8


def test_custom_split_length(run: fit.Activity) -> None:
    splits = fit.splits(run, 2000)
    assert [s["distance"] for s in splits] == [2000.0, 500.0]


def test_split_limits(run: fit.Activity, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(fit.FitError, match="at least 100"):
        fit.splits(run, 50)
    monkeypatch.setattr(fit, "MAX_SPLITS", 10)
    with pytest.raises(fit.FitError, match="more than 10"):
        fit.splits(run, 100)


def test_strength_has_no_splits() -> None:
    with pytest.raises(fit.FitError, match="no distance"):
        fit.splits(fit.decode(synthetic_strength()))


def test_time_in_zones(run: fit.Activity) -> None:
    zones = fit.zones_from_fit(run)
    assert zones is not None and zones.floors == (110, 130, 150, 160, 170)
    result = fit.time_in_zones(run, zones)
    seconds = {k: v["seconds"] for k, v in result.items()}
    assert sum(seconds.values()) == 600  # timer time; the 60 s pause is not counted
    assert seconds["zone1"] == pytest.approx(300, abs=1)
    assert seconds["zone2"] == pytest.approx(300, abs=1)
    assert result["zone2"]["bpm"] == "130-149"
    assert result["zone5"]["bpm"] == "170-190"


def test_zones_from_api_prefers_the_sport() -> None:
    raw = [
        {"sport": "DEFAULT", "zone1Floor": 100, "zone2Floor": 120, "zone3Floor": 140,
         "zone4Floor": 160, "zone5Floor": 170, "maxHeartRateUsed": 190},
        {"sport": "RUNNING", "zone1Floor": 110, "zone2Floor": 130, "zone3Floor": 150,
         "zone4Floor": 165, "zone5Floor": 175, "maxHeartRateUsed": 190},
    ]  # fmt: skip
    running = fit.zones_from_api(raw, "running")
    assert running is not None and running.floors[0] == 110
    cycling = fit.zones_from_api(raw, "cycling")
    assert cycling is not None and cycling.floors[0] == 100
    assert fit.zones_from_api([], "running") is None


def test_no_heart_rate() -> None:
    activity = fit.decode(synthetic_run())
    for record in activity.records:
        record.pop("heart_rate", None)
    zones = fit.zones_from_fit(activity)
    assert zones is not None
    with pytest.raises(fit.FitError, match="no heart rate"):
        fit.time_in_zones(activity, zones)


def test_summary_stats(run: fit.Activity) -> None:
    summary = fit.summary_stats(run)
    assert summary["sport"] == "running"
    assert summary["pace"] == "4:00 /km"
    assert summary["avgCadence"] == 171
    assert summary["runningDynamics"] == {
        "groundContactTimeMs": 240.0,
        "verticalOscillationCm": 8.5,
        "stepLengthM": 1.46,
    }
    assert "heart rate" in summary["recordedData"]
    text = json.dumps(summary)
    assert "663000000" not in text and "position_" not in text
    assert len(text) < 2000


def test_strength_summary_has_no_distance_or_pace() -> None:
    summary = fit.summary_stats(fit.decode(synthetic_strength()))
    assert "distance" not in summary and "pace" not in summary
    assert "avgCadence" not in summary
