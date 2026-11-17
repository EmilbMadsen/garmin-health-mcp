"""MCP server exposing the Garmin Connect Personal API as read-only tools.

One tool per dataset, named after the data rather than the endpoint. Every tool is read-only:
the Personal API has no write operations.

Entry point for the ``garmin-health-mcp`` console script:

- no arguments or ``serve``: run the MCP server over stdio
- ``setup``: store the Personal API key in the system keyring
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Annotated, Any, Literal

import anyio.to_thread
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from mcp_owner_auth import ConfigError, OwnerAuthConfig
from mcp_owner_auth import cli as auth_cli
from mcp_owner_auth import serve as serve_http
from pydantic import Field

from . import __version__
from . import fit_analysis as fit
from . import presentation as p
from .client import (
    BODY_BATTERY,
    DAILY_SUMMARIES,
    HRV_SUMMARIES,
    HYDRATION,
    SLEEP_SUMMARIES,
    STEPS,
    STRESS,
    WELLNESS_ACTIVITIES,
    Api,
    GarminError,
    GarminService,
    RangeEndpoint,
    check_activity_id,
    parse_date,
    parse_range,
)
from .credentials import CredentialsMissingError, state_dir
from .setup_cli import run_setup

APP_NAME = "Garmin Health"
CONSENT = (
    "read-only access to your Garmin health and activity data (sleep, HRV, heart rate, "
    "stress, Body Battery, weight, nutrition and activities)"
)
CYCLE_ENV_VAR = "GARMIN_HEALTH_MCP_ENABLE_CYCLE"
ENABLE_CYCLE = os.environ.get(CYCLE_ENV_VAR, "").strip() == "1"
DEFAULT_RANGE_DAYS = 7

service = GarminService()

INSTRUCTIONS = (
    "Read-only access to the user's own Garmin Connect health and activity data through Garmin's "
    "official Personal API (unofficial server, not affiliated with Garmin). Times are ISO 8601 in "
    "the user's local time zone. Dates are YYYY-MM-DD; a day's sleep belongs to the date the user "
    "woke up. Range tools default to the last 7 days and accept up to 1 year. Not available "
    "through this API: training readiness, training status, race predictions, VO2 max, calendar "
    "and workouts. Activities do include training effect, training load and time in HR zones; "
    "laps, splits, zone time and running dynamics come from the activity's FIT file."
)


@asynccontextmanager
async def _lifespan(_: MCPServer[None]) -> AsyncIterator[None]:
    try:
        yield
    finally:
        service.close()


server: MCPServer[None] = MCPServer(
    name="garmin-health",
    version=__version__,
    instructions=INSTRUCTIONS,
    lifespan=_lifespan,
)

READ_ONLY = ToolAnnotations(read_only_hint=True, open_world_hint=True)

Day = Annotated[
    str | None,
    Field(description="Date as YYYY-MM-DD in the user's time zone. Leave empty for today."),
]
Start = Annotated[
    str | None,
    Field(description="First date (YYYY-MM-DD), inclusive. Default: 6 days before end_date."),
]
End = Annotated[str | None, Field(description="Last date (YYYY-MM-DD), inclusive. Default: today.")]
ActivityId = Annotated[
    int, Field(gt=0, description="Activity ID from search_activities (a positive integer).")
]


def _today() -> date:
    return datetime.now(p.user_tz()).date()


def _day(value: str | None) -> date:
    try:
        return parse_date(value) if value else _today()
    except GarminError as err:
        raise ToolError(str(err)) from err


def _range(start: str | None, end: str | None) -> tuple[str, str]:
    last = _day(end).isoformat() if end else _today().isoformat()
    first = start or (date.fromisoformat(last) - timedelta(days=DEFAULT_RANGE_DAYS - 1)).isoformat()
    try:
        parse_range(first, last)
    except GarminError as err:
        raise ToolError(str(err)) from err
    return first, last


async def _fetch(call: Callable[[Api], Any]) -> Any:
    try:
        return await service.call(call)
    except GarminError as err:
        raise ToolError(str(err)) from err


async def _fetch_range(endpoint: RangeEndpoint, start: str | None, end: str | None) -> Any:
    first, last = _range(start, end)
    try:
        return await service.call_range(endpoint, first, last)
    except GarminError as err:
        raise ToolError(str(err)) from err


def _items(raw: Any, key: str | None = None) -> list[Any]:
    if key is not None and isinstance(raw, dict):
        raw = raw.get(key)
    return raw if isinstance(raw, list) else []


# -- sleep ------------------------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def get_sleep(date: Day = None) -> dict[str, Any]:
    """Full sleep record for one night (the date the user woke up): sleep and wake time, time in
    deep/light/REM/awake, sleep score with per-factor ratings and optimal ranges, average heart
    rate, SpO2, respiration, sleep stress, awake count, and sleep need for today and tomorrow."""
    day = _day(date)
    return {"sleep": p.sleep_day(await _fetch(lambda c: c.get_daily_sleep(day)))}


@server.tool(annotations=READ_ONLY)
async def get_sleep_summaries(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """Compact sleep summary per night over a date range: duration, stages, sleep score,
    average heart rate, stress, SpO2 and respiration. Use get_sleep for one night in detail."""
    raw = await _fetch_range(SLEEP_SUMMARIES, start_date, end_date)
    return {"nights": [p.sleep_compact(night) for night in _items(raw)]}


# -- HRV --------------------------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def get_hrv(
    date: Day = None,
    include_readings: Annotated[
        bool, Field(description="Also return every 5-minute HRV reading of the night.")
    ] = False,
) -> dict[str, Any]:
    """Heart rate variability for one night: last-night average, 5-minute high, weekly average,
    personal baseline band and HRV status (e.g. BALANCED, UNBALANCED, LOW)."""
    day = _day(date)
    raw = await _fetch(lambda c: c.get_daily_hrv(day))
    return {"hrv": p.hrv(raw, include_readings=include_readings)}


@server.tool(annotations=READ_ONLY)
async def get_hrv_summaries(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """HRV summary per night over a date range: last-night average, weekly average, baseline
    band and status. Best for HRV trends."""
    raw = await _fetch_range(HRV_SUMMARIES, start_date, end_date)
    return {"days": p.clean(_items(raw, "hrvSummaries"))}


# -- daily wellness ---------------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def get_body_battery(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """Body Battery per day: charged and drained, level over the day, and events that changed
    it (sleep, activities, stress) with their impact and Garmin's feedback."""
    raw = await _fetch_range(BODY_BATTERY, start_date, end_date)
    return {"days": [p.body_battery_day(day) for day in _items(raw)]}


@server.tool(annotations=READ_ONLY)
async def get_stress(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """Daily stress: overall stress level (0-100) and seconds spent at rest and in low, medium
    and high stress."""
    raw = await _fetch_range(STRESS, start_date, end_date)
    return {"days": p.clean(_items(raw))}


@server.tool(annotations=READ_ONLY)
async def get_steps(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """Daily steps, distance (meters) and step goal."""
    raw = await _fetch_range(STEPS, start_date, end_date)
    return {"days": p.clean(_items(raw))}


@server.tool(annotations=READ_ONLY)
async def get_daily_summaries(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """One overview per day: steps and floors, calories (active, resting, total), heart rate
    (min, max, resting), intensity minutes vs goal, stress breakdown, Body Battery (min, max,
    charged, drained), hydration, respiration and pulse ox. The best single call for "how was
    my day/week". Durations are in milliseconds."""
    raw = await _fetch_range(DAILY_SUMMARIES, start_date, end_date)
    return {"days": [p.daily_summary(day) for day in _items(raw, "data")]}


@server.tool(annotations=READ_ONLY)
async def get_heart_rate(
    date: Day = None,
    include_samples: Annotated[
        bool, Field(description="Also return every sample (about one per 2 minutes).")
    ] = False,
) -> dict[str, Any]:
    """Heart rate for one day: resting heart rate, 7-day average resting heart rate, min and max,
    and min/avg/max per hour."""
    day = _day(date)
    raw = await _fetch(lambda c: c.get_daily_heart_rate(day))
    return {"heartRate": p.heart_rate(raw, include_samples=include_samples)}


@server.tool(annotations=READ_ONLY)
async def get_heart_rate_zones() -> dict[str, Any]:
    """The user's heart rate zones per sport: zone floors 1-5, method (e.g. % of max HR or
    HR reserve), and the max, resting and lactate threshold heart rate used."""
    return {"zones": p.clean(_items(await _fetch(lambda c: c.get_heart_rate_zones())))}


@server.tool(annotations=READ_ONLY)
async def get_spo2(date: Day = None) -> dict[str, Any]:
    """Blood oxygen (SpO2) for one day: day and sleep averages, lowest, latest, 7-day average,
    and hourly averages."""
    day = _day(date)
    return {"spo2": p.spo2(await _fetch(lambda c: c.get_daily_spo2(day)))}


@server.tool(annotations=READ_ONLY)
async def get_fitness_age(date: Day = None) -> dict[str, Any]:
    """Garmin fitness age vs chronological age, and its components (vigorous days and minutes,
    resting heart rate, BMI)."""
    day = _day(date)
    return {"fitnessAge": p.clean(await _fetch(lambda c: c.get_fitness_age(day)))}


# -- body and nutrition -----------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def get_weight(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """Weigh-ins in a date range (weight in grams, BMI and body composition if measured), the
    range average, and the nearest weigh-in before and after the range."""
    first, last = _range(start_date, end_date)
    raw = await _fetch(lambda c: c.get_weight_measurements(first, last))
    weight = p.clean(raw)
    if isinstance(weight, dict):
        average = weight.get("totalAverage")
        if isinstance(average, dict) and set(average) <= {"from", "until"}:
            del weight["totalAverage"]  # no weigh-ins in range: only epoch range bounds left
        elif isinstance(average, dict):
            average.pop("from", None)
            average.pop("until", None)
    return {"weight": weight}


@server.tool(annotations=READ_ONLY)
async def get_weight_goals(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """Weight goals in effect during a date range."""
    first, last = _range(start_date, end_date)
    return {"goals": p.clean(await _fetch(lambda c: c.get_effective_weight_goals(first, last)))}


@server.tool(annotations=READ_ONLY)
async def get_hydration(start_date: Start = None, end_date: End = None) -> dict[str, Any]:
    """Logged water intake per day. Empty if the user does not log hydration."""
    raw = await _fetch_range(HYDRATION, start_date, end_date)
    return {"days": p.clean(_items(raw))}


@server.tool(annotations=READ_ONLY)
async def get_food_logs(date: Day = None) -> dict[str, Any]:
    """Foods logged in Garmin Connect for one day, with meals and serving sizes. Empty if the
    user does not log food in Garmin."""
    day = _day(date)
    return {"foodLog": p.clean(await _fetch(lambda c: c.get_daily_food_logs(day)))}


# -- activities -------------------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def search_activities(
    start_date: Annotated[
        str | None, Field(description="Only activities on or after this date (YYYY-MM-DD).")
    ] = None,
    end_date: Annotated[
        str | None, Field(description="Only activities on or before this date (YYYY-MM-DD).")
    ] = None,
    activity_type: Annotated[
        str | None,
        Field(description="Garmin type key, e.g. running, cycling, strength_training."),
    ] = None,
    limit: Annotated[int, Field(ge=1, le=100, description="Max activities to return.")] = 20,
    start: Annotated[int, Field(ge=0, description="Offset for paging, newest first.")] = 0,
) -> dict[str, Any]:
    """Recorded activities, newest first: name, type, start time, distance (m), duration (s),
    speed (m/s), calories, average/max HR, aerobic and anaerobic training effect, training
    load, seconds in HR zones 1-5, intensity minutes, Body Battery change and strength sets."""
    first = _day(start_date) if start_date else None
    last = _day(end_date) if end_date else None
    if first and last and last < first:
        raise ToolError("end_date must be on or after start_date.")
    raw = await _fetch(
        lambda c: c.search_activities(
            start=start, limit=limit, start_date=first, end_date=last, activity_type=activity_type
        )
    )
    return {"activities": [p.activity_compact(a) for a in _items(raw)]}


@server.tool(annotations=READ_ONLY)
async def get_activity(activity_id: ActivityId) -> dict[str, Any]:
    """Details of one activity: summary (distance, durations, speed, heart rate, calories,
    training effect and load, intensity minutes, steps), type, time zone and device."""
    try:
        activity = check_activity_id(activity_id)
    except GarminError as err:
        raise ToolError(str(err)) from err
    return {"activity": p.activity_detail(await _fetch(lambda c: c.get_activity(activity)))}


@server.tool(annotations=READ_ONLY)
async def get_activity_workouts(activity_id: ActivityId) -> dict[str, Any]:
    """The planned workout (steps and targets) the activity was recorded against, if any."""
    try:
        activity = check_activity_id(activity_id)
    except GarminError as err:
        raise ToolError(str(err)) from err
    return {"workouts": p.clean(await _fetch(lambda c: c.get_activity_workouts(activity)))}


@server.tool(annotations=READ_ONLY)
async def get_wellness_activity_summaries(
    start_date: Start = None, end_date: End = None
) -> dict[str, Any]:
    """Wellness activity summaries (e.g. breathwork or relaxation sessions) in a date range."""
    raw = await _fetch_range(WELLNESS_ACTIVITIES, start_date, end_date)
    return {"summaries": p.clean(_items(raw))}


# -- activity analysis from the FIT file ----------------------------------------------------

MAX_LAPS = 60  # keeps a response under ~20 kB


def _fit_cache() -> fit.FitCache:
    return fit.FitCache(state_dir() / "fit")


async def _load_activity(activity_id: int) -> fit.Activity:
    """Download the original FIT file once (cached per activity), then decode it."""
    try:
        activity = check_activity_id(activity_id)
    except GarminError as err:
        raise ToolError(str(err)) from err
    cache = _fit_cache()
    data = await anyio.to_thread.run_sync(cache.get, activity)
    if data is None:
        zipped = await _fetch(lambda c: c.download_activity(activity))
        try:
            data = fit.extract_fit(zipped)
        except fit.FitError as err:
            raise ToolError(str(err)) from err
        await anyio.to_thread.run_sync(cache.put, activity, data)
    try:
        return await anyio.to_thread.run_sync(fit.decode, data)
    except fit.FitError as err:
        raise ToolError(str(err)) from err


@server.tool(annotations=READ_ONLY)
async def get_activity_laps(activity_id: ActivityId) -> dict[str, Any]:
    """Laps of one activity from its FIT file: distance, timer time, pace (or speed), grade-
    adjusted pace, average/max heart rate, cadence, power, ascent/descent, intensity (active,
    rest, warmup, cooldown) and what ended the lap (manual, distance, time). For structured
    workouts and intervals, laps are the reps."""
    activity = await _load_activity(activity_id)
    laps = fit.laps(activity)
    out: dict[str, Any] = {"sport": activity.sport, "lapCount": len(laps), "laps": laps[:MAX_LAPS]}
    if len(laps) > MAX_LAPS:
        out["note"] = f"Only the first {MAX_LAPS} laps are shown."
    return out


@server.tool(annotations=READ_ONLY)
async def get_activity_splits(
    activity_id: ActivityId,
    split_meters: Annotated[
        float,
        Field(ge=100, le=100_000, description="Split length in meters, e.g. 1000 or 1609.34."),
    ] = 1000.0,
) -> dict[str, Any]:
    """Even distance splits of one activity (per km by default), computed from the recorded
    data: time, pace, average/max heart rate, cadence, power and elevation gain/loss per split.
    Paused time is excluded. The last split is usually shorter. Needs distance data."""
    activity = await _load_activity(activity_id)
    try:
        splits = fit.splits(activity, split_meters)
    except fit.FitError as err:
        raise ToolError(str(err)) from err
    return {"sport": activity.sport, "splitMeters": split_meters, "splits": splits}


@server.tool(annotations=READ_ONLY)
async def get_activity_time_in_hr_zones(
    activity_id: ActivityId,
    zones: Annotated[
        Literal["current", "recorded"],
        Field(
            description="'current': the user's heart rate zones in Garmin Connect now. "
            "'recorded': the zones the watch used during the activity."
        ),
    ] = "current",
) -> dict[str, Any]:
    """Time in heart rate zones for one activity, computed from the recorded heart rate:
    seconds and percent below zone 1 and in zones 1-5, with each zone's bpm range. Paused
    time is excluded. Also returns the watch's own time-in-zone totals for comparison."""
    activity = await _load_activity(activity_id)
    chosen: fit.Zones | None = None
    if zones == "current":
        raw = await _fetch(lambda c: c.get_heart_rate_zones())
        chosen = fit.zones_from_api(raw, activity.sport)
    if chosen is None:
        chosen = fit.zones_from_fit(activity)
    if chosen is None:
        raise ToolError("No heart rate zones available for this activity.")
    try:
        computed = fit.time_in_zones(activity, chosen)
    except fit.FitError as err:
        raise ToolError(str(err)) from err
    out: dict[str, Any] = {
        "sport": activity.sport,
        "zoneSource": chosen.source,
        "maxHeartRate": chosen.max_hr,
        "timeInZones": computed,
    }
    device = fit.device_time_in_zones(activity)
    if device:
        labels = ["belowZone1", "zone1", "zone2", "zone3", "zone4", "zone5"]
        device_seconds = {label: round(s) for label, s in zip(labels, device, strict=False)}
        device_seconds["zone5"] += round(sum(device[6:]))  # above max HR counts as zone 5
        out["watchTimeInZonesSeconds"] = device_seconds
    return out


@server.tool(annotations=READ_ONLY)
async def get_activity_summary_stats(activity_id: ActivityId) -> dict[str, Any]:
    """Session summary from the activity's FIT file: distance, timer and elapsed time, pace and
    best pace (or speed), grade-adjusted pace, heart rate, cadence, power (avg, max,
    normalized), ascent/descent, calories, aerobic and anaerobic training effect, training
    load, running dynamics (ground contact time and balance, vertical oscillation and ratio,
    step length), respiration rate, perceived effort and feel, and which data the file
    contains."""
    activity = await _load_activity(activity_id)
    return {"summary": fit.summary_stats(activity)}


# -- profile ----------------------------------------------------------------------------------


@server.tool(annotations=READ_ONLY)
async def get_profile_settings() -> dict[str, Any]:
    """The user's unit system, locale, time zone, first day of week and date/time formats."""
    return {"profile": p.profile(await _fetch(lambda c: c.get_user_profile_settings()))}


# -- menstrual cycle (opt-in) -----------------------------------------------------------------

if ENABLE_CYCLE:

    @server.tool(annotations=READ_ONLY)
    async def get_menstrual_cycle(date: Day = None) -> dict[str, Any]:
        """Menstrual cycle summary and day view for one date."""
        day = _day(date)
        today = _today()
        summary = await _fetch(lambda c: c.get_menstrual_cycle_summary(day))
        view = await _fetch(lambda c: c.get_menstrual_cycle_day_view(day, today))
        return {"summary": p.clean(summary), "dayView": p.clean(view)}


# -- entry point ------------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="garmin-health-mcp",
        description="Unofficial, read-only MCP server for the Garmin Connect Personal API. "
        "Run without arguments to serve MCP over stdio.",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command")
    setup = commands.add_parser("setup", help="store your Personal API key in the keyring")
    setup.add_argument("--delete", action="store_true", help="remove the stored key instead")
    serve = commands.add_parser("serve", help="run the MCP server (stdio unless --http)")
    serve.add_argument("--http", action="store_true", help="Streamable HTTP with OAuth")
    serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    serve.add_argument("--port", type=int, default=8000, help="port (default 8000)")
    auth_cli.add_parser(commands)
    args = parser.parse_args(argv)

    if args.command == "setup":
        sys.exit(run_setup(delete=args.delete))
    if args.command == "auth":
        sys.exit(
            auth_cli.run(
                args.action,
                auth_db=state_dir() / "auth.db",
                app_name=APP_NAME,
                command="garmin-health-mcp",
                force=args.force,
                redirect_uris=args.redirect_uri,
            )
        )

    logging.basicConfig(level=logging.WARNING, stream=sys.stderr)
    # Garmin's library logs every request URL at INFO; keep it quiet.
    logging.getLogger("garmin_personal_api").setLevel(logging.WARNING)
    if args.command == "serve" and args.http:
        _serve_http(args.host, args.port)
        return
    server.run("stdio")


def auth_config() -> OwnerAuthConfig:
    return OwnerAuthConfig.from_env(
        "GARMIN_HEALTH_MCP",
        state_dir=state_dir(),
        app_name=APP_NAME,
        command="garmin-health-mcp",
        scope="garmin:read",
        consent=CONSENT,
    )


def _serve_http(host: str, port: int) -> None:
    from .credentials import load_api_key

    logging.getLogger().setLevel(logging.INFO)
    try:
        config = auth_config()
        load_api_key()  # fail at startup, not on the first tool call
        print(f"Serving {config.resource} on {host}:{port} ({config.auth_mode})", file=sys.stderr)
        serve_http(server, config, host=host, port=port)
    except (ConfigError, CredentialsMissingError, OSError, ValueError) as err:
        print(f"Cannot start: {err}", file=sys.stderr)
        sys.exit(2)


if __name__ == "__main__":
    main()
