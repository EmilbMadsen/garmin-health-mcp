# Capabilities of the Garmin Connect Personal API

Based on a discovery run (`scripts/discover.py`) on 2026-11-17 from a home connection, with
`garmin-connect-personal-api-python` 1.0.0. Single-date endpoints were called for 2026-11-16 and
range endpoints for 2026-11-10..2026-11-16. All 22 calls returned HTTP 200 in 0.14 to 0.61 s.
This file describes structure only, never values.

## Coverage

The library wraps 24 calls: 21 JSON reads, 2 menstrual-cycle reads (not called; opt-in) and the
activity download. **`get_activity_types` and `get_daily_vo2_max`
do not exist in v1.0.0**, so `list_activity_types` and `get_vo2max` need either a later library
version or an undocumented path, which this project does not use.

Not available at all, as expected: training readiness, training status, training load balance,
race predictions, calendar, workout library, write operations.

## Timestamp formats (for presentation.py)

The API mixes four formats. All must become ISO 8601 in the user's time zone.

| Format | Example shape | Where |
|---|---|---|
| Epoch milliseconds (int) | `9999999999999` | sleep start/end, `beginTimestamp`, time-series arrays (HR, body battery, SpO2) |
| ISO without zone, `.0` fraction | `9999-99-99T99:99:99.9` | HRV, SpO2, heart rate, body battery (`...GMT` and `...Local` pairs) |
| Space separated, no zone | `9999-99-99 99:99:99` | activities (`startTimeGMT`, `startTimeLocal`) |
| Date only | `9999-99-99` | `calendarDate` everywhere |

"Local" sleep timestamps are epoch-ms shifted by the local offset, not real epochs. Use the GMT
fields and convert.

## Endpoints

| Endpoint (library method) | Shape | Content | Caveats |
|---|---|---|---|
| `get_user_profile_settings` | object | display name, locale, unit system, time zone, date/time/number formats, first day of week | Contains `displayName`. Return only units, time zone and locale. |
| `get_daily_sleep` (date) | object, ~3 kB | durations per stage (deep/light/REM/awake), nap time, sleep scores (overall + per factor with optimal ranges), avg HR, SpO2, respiration, sleep stress, awake count, sleep need (today + next), feedback/insight keys | Epoch-ms timestamps. `autoSleep*` and `sleepQualityTypePK` were null. |
| `get_daily_sleep_summaries` (range) | list per day, ~3.8 kB/day | same as daily sleep, plus `breathingDisruptionSeverity` | Covers the single-day endpoint for most uses. |
| `get_daily_hrv` (date) | object, ~15 kB | `hrvSummary` (weekly avg, last night avg, 5-min high, baseline bands, status, feedback) + `hrvReadings` (~100 five-minute readings) | Readings make it large; summarize or make them opt-in. |
| `get_daily_hrv_summaries` (range) | `{hrvSummaries: [...]}` | the `hrvSummary` per day | Compact; good default for trends. |
| `get_daily_body_battery` (range) | list per day, ~2.3 kB/day | charged, drained, values array `[epoch_ms, level]`, activity events with impact, dynamic feedback | Values array is sparse (a few points per day here). |
| `get_daily_stress` (range) | list per day | overall level and durations rest/low/medium/high | Small. Durations in seconds. |
| `get_daily_steps` (range) | list per day | total steps, distance, step goal | Small. |
| `get_daily_summaries` (range) | `{data: [...]}`, ~4.3 kB/day | per day: movement (steps, floors), calories (active/resting/total), heart rate (min/max/resting), intensity minutes vs goal, stress breakdown, body battery (min/max/latest/charged/drained, events), hydration, respiration, pulse ox, jet lag | Best "one call per day" overview. Durations in ms. |
| `get_daily_heart_rate` (date) | object, ~14 kB | min, max, resting, 7-day avg resting, values `[epoch_ms, bpm]` (~300 points at ~2 min) | Summarize the series; never return raw by default. |
| `get_heart_rate_zones` | list per sport | method, zone floors 1-5, max/resting/LT HR used | Two entries here (default + a sport). LT HR null. |
| `get_daily_spo2` (date) | object, ~14 kB | averages (day, sleep, 7-day), lowest, latest, hourly averages, monitoring-environment series | Series are large; summarize. |
| `get_fitness_age` (date) | object | fitness age, previous, chronological age, components (vigorous days/minutes, RHR, BMI with staleness) | |
| `get_weight_measurements` (range) | object | daily weight summaries, total average, nearest previous/next weigh-in | Empty range still returns the previous weigh-in, which is useful. Body composition fields null (no smart scale). |
| `get_effective_weight_goals` (range) | object | user goals and ranges | Empty for this account. |
| `get_daily_hydration` (range) | list | hydration log per day | Empty `[]` for this account. |
| `get_daily_food_logs` (date) | object | meal details and logged foods with serving sizes | Empty lists for this account. |
| `get_wellness_activity_summaries` (range) | list | wellness activities | Empty `[]`; purpose unclear. |
| `search_activities` | list, ~5.4 kB/activity | name, type, start, distance, durations, speed, calories, HR avg/max, **time in HR zones 1-5**, **aerobic/anaerobic training effect + label**, **activity training load**, intensity minutes, body-battery delta, strength sets (category/reps/volume/weight), lap count | Includes `ownerFullName`, `ownerDisplayName` and profile image URLs: strip them. Default limit 20. |
| `get_activity` (id) | object, ~2.8 kB | `summaryDTO` (similar to search, plus min HR, moving duration, steps), type, event type, time zone, metadata (lap count, device manufacturer, file format) | `metadataDTO.userInfoDto` has full name and image URLs: strip. |
| `get_activity_workouts` (id) | list | workout attached to the activity | Empty for an activity without a planned workout. |
| `download_activity` (id) | ZIP bytes | one `<id>_ACTIVITY.fit` (~35 kB for a short strength session) | Input for the FIT analysis tools. |
| `get_menstrual_cycle_*` | | | Not called. Off by default (`GARMIN_HEALTH_MCP_ENABLE_CYCLE=1`). |

## Training-adjacent data that is available

Even without training status/readiness, the Personal API exposes per-activity training effect,
training load and HR-zone time (`search_activities`), plus HRV status, sleep score, body battery
and resting HR. Together these cover much of what readiness is built from.

## Verified since

- The API answers from a datacenter IP: token exchange and reads work from a Hetzner VPS
  (November 2026).

## Still to verify

- Real range limits (the 28/31-day chunk sizes are conservative and not yet tested against the API).
- Behaviour for future dates, dates before the account existed, and an invalid activity ID.
- Throttling thresholds; none were hit at one call per second.
