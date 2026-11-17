# garmin-health-mcp

Unofficial, read-only MCP server for the
[Garmin Connect Personal API](https://github.com/garmin/connect-personal-api-python).

> **Unofficial, not affiliated with Garmin.** Single-user and self-hosted only: a Personal API key
> may only be used by its owner. Do not run this as a shared or hosted service.

[![CI](https://github.com/EmilbMadsen/garmin-health-mcp/actions/workflows/ci.yml/badge.svg)](https://github.com/EmilbMadsen/garmin-health-mcp/actions/workflows/ci.yml)
[![PyPI](https://img.shields.io/pypi/v/garmin-health-mcp)](https://pypi.org/project/garmin-health-mcp/)

Lets Claude (or any MCP client) read your own sleep, HRV, heart rate, stress, Body Battery,
weight, nutrition and activities, including laps, splits and time in heart-rate zones from the
original FIT files. It uses Garmin's official, key-based Personal API, so there is no password
login and no scraping. See [docs/capabilities.md](docs/capabilities.md) for what each endpoint
returns.

## Requirements

- [uv](https://docs.astral.sh/uv/) (it installs Python 3.13 for you if needed)
- A Garmin Connect Personal API key: create one at
  <https://connect.garmin.com/app/settings/personalApiToken> (only one key can exist at a time;
  choose its lifetime there)

## Setup

Store the key in the system keyring (macOS Keychain, Windows Credential Locker or the Secret
Service on Linux). `setup` verifies it with one call before saving it:

```bash
uvx garmin-health-mcp setup
```

`uvx garmin-health-mcp setup --delete` removes it again. The key is looked up in this order:
`GARMIN_PERSONAL_API_KEY_FILE`, then `GARMIN_PERSONAL_API_KEY`, then the keyring (service
`garmin-health-mcp`). It is never printed, logged or written to `.env`. Garmin's bearer token is
cached with mode 0600 in `GARMIN_HEALTH_MCP_STATE_DIR` (default `~/.config/garmin-health-mcp/`,
mode 0700).

## Use with Claude

The server runs over stdio: your MCP client starts it when needed.

**Claude Code:**

```bash
claude mcp add --scope user garmin-health -- uvx garmin-health-mcp
```

**Claude Desktop** (`~/Library/Application Support/Claude/claude_desktop_config.json` on macOS),
then restart Claude Desktop:

```json
{
  "mcpServers": {
    "garmin-health": {
      "command": "uvx",
      "args": ["garmin-health-mcp"]
    }
  }
}
```

If Claude Desktop cannot find `uvx`, use its full path (`which uvx`). The server is named
`garmin-health`, so it can run next to another Garmin MCP server without tool name clashes.

## Remote access (claude.ai and the Claude mobile app)

`garmin-health-mcp serve --http` serves Streamable HTTP at `/mcp`, protected by a built-in
single-user OAuth 2.1 server: one static client (no dynamic registration), PKCE S256, login with
password + TOTP, 1-hour access tokens, rotating 30-day refresh tokens with reuse detection, and
`garmin-health-mcp auth revoke-all` as an emergency brake. Without `--http` it runs stdio with no
auth, as before.

- Auth layer: [mcp-owner-auth](https://github.com/EmilbMadsen/mcp-owner-auth)
- Design and threat model: [docs/security.md](docs/security.md)
- VPS + Docker + Cloudflare Tunnel + claude.ai setup: [docs/deployment.md](docs/deployment.md)

## Tools

All tools are read-only. Dates are `YYYY-MM-DD`; single-day tools default to today and range tools
to the last 7 days (at most 1 year).

| Tool | Data |
|---|---|
| `get_sleep` | one night in detail: stages, sleep score and factors, HR, SpO2, respiration, sleep need |
| `get_sleep_summaries` | compact sleep summary per night over a range |
| `get_hrv` / `get_hrv_summaries` | HRV status, last-night average, baseline (optional 5-min readings) |
| `get_body_battery` | charged/drained, level over the day, events with impact |
| `get_stress` | daily stress level and time per stress band |
| `get_steps` | daily steps, distance, goal |
| `get_daily_summaries` | one overview per day: movement, calories, HR, intensity minutes, stress, Body Battery, respiration, pulse ox |
| `get_heart_rate` | resting HR, 7-day average, min/max, hourly min/avg/max (optional raw samples) |
| `get_heart_rate_zones` | zone floors per sport and the HR values used |
| `get_spo2` | SpO2 averages and hourly averages |
| `get_fitness_age` | fitness age and its components |
| `get_weight` / `get_weight_goals` | weigh-ins and goals |
| `get_hydration` / `get_food_logs` | logged water and food |
| `get_wellness_activity_summaries` | wellness activities |
| `get_profile_settings` | units, locale, time zone (no name or other personal data) |
| `search_activities` | activities with training effect, training load and time in HR zones |
| `get_activity` / `get_activity_workouts` | one activity in detail and its planned workout |
| `get_activity_laps` | laps from the FIT file: pace, GAP, HR, cadence, power, ascent, intensity |
| `get_activity_splits` | even splits (per km or any distance) computed from the recorded data |
| `get_activity_time_in_hr_zones` | time in zones from recorded HR, with current or recorded zones |
| `get_activity_summary_stats` | session stats incl. running dynamics, power, respiration, RPE |

Menstrual-cycle data is off by default; set `GARMIN_HEALTH_MCP_ENABLE_CYCLE=1` to add
`get_menstrual_cycle`.

Not available through the Personal API: training readiness, training status, race predictions,
VO2 max, calendar and workouts. See [docs/capabilities.md](docs/capabilities.md).

### FIT-file analysis

The four `get_activity_*` analysis tools download the activity's original FIT file once and cache
it in `<state dir>/fit/<activity_id>.fit` (directory 0700, files 0600; it is health data). It is
decoded with Garmin's [FIT SDK](https://github.com/garmin/fit-python-sdk). Splits and zone times
exclude paused time; elevation uses a 1.5 m hysteresis against barometer noise; running cadence is
reported in steps/min. Raw record streams and GPS positions are never returned. Delete the cache
directory at any time to free space.

## What the server does to responses

- **Time:** every GMT timestamp (epoch ms or naive string) becomes ISO 8601 in
  `GARMIN_HEALTH_MCP_TZ` (default `Europe/Copenhagen`). The `GMT` suffix is dropped from the key
  and the duplicate `...Local` field is removed.
- **Noise and privacy:** `null` and empty fields, names, display names, profile/owner/device IDs,
  profile image URLs and GPS start/end coordinates are removed.
- **Size:** long series (heart rate, SpO2, HRV readings) are summarized; raw samples are opt-in.
- **Reliability:** HTTP 401 triggers one token refresh; 429/5xx and network errors are retried
  with exponential backoff and jitter (3 attempts, `Retry-After` honoured); date ranges are split
  into chunks the API accepts.
- **Logs:** method, path and status only. Response bodies are health data and are never logged.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Test fixtures are synthetic: `scripts/anonymize.py` keeps
the structure of real responses but replaces every identifier and value.

## Disclaimer

Unofficial project, not affiliated with or endorsed by Garmin. Garmin Connect is a trademark of
Garmin Ltd. Use your own Personal API key for your own data only, within Garmin's terms. This is
not medical software.

## Credits

- [garmin-connect-personal-api-python](https://github.com/garmin/connect-personal-api-python) and
  the [FIT Python SDK](https://github.com/garmin/fit-python-sdk) by Garmin (Apache-2.0)
- [MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk)

## License

Apache-2.0, see [LICENSE](LICENSE).
