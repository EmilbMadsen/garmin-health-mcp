# Contributing

Thanks for helping.

## Scope

garmin-health-mcp wraps Garmin's official
[Personal API client](https://github.com/garmin/connect-personal-api-python) and
[FIT SDK](https://github.com/garmin/fit-python-sdk). Please keep it that way:

- Read-only. The Personal API has no write endpoints, and this server adds none.
- Only the official, key-based Personal API. No password logins, no scraping of Garmin Connect,
  no undocumented endpoints.
- Responses are summaries: no raw record streams, no GPS positions, no names or profile IDs.
- Single-user and self-hosted. A Personal API key may only be used by its owner.

## Development setup

You need Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
uv run ruff check
uv run ruff format --check
uv run mypy
uv run pytest
```

CI runs the same four checks on every pull request. The tests use synthetic fixtures and
synthetic FIT files and need no Garmin account.

To try the server end to end over stdio, as an MCP client starts it:

```bash
uv run python tests/smoke_stdio.py          # lists tools, no key needed
uv run python tests/smoke_stdio.py --live   # also calls two tools with your key
```

## Recording new responses

1. Store your key: `uv run garmin-health-mcp setup`.
2. Record every endpoint once: `uv run python scripts/discover.py`. Raw responses go to
   `dumps/<timestamp>/`, which git ignores. **They are your health data: never commit them or
   paste them anywhere.**
3. Turn them into fixtures: `uv run python scripts/anonymize.py dumps/<timestamp> --out
   tests/fixtures/garmin`. Identifiers are replaced, personal values (age, weight, zones) are
   fixed, every other number becomes a random value of the same magnitude and all timestamps are
   shifted. **Read the result yourself before committing it.**

## Pull requests

- Keep each pull request to one change.
- Add or update tests for behaviour changes.
- Never include keys, tokens, dumps or health data, including in screenshots and logs.

## Releasing (maintainers)

Releases go to PyPI from GitHub Actions with
[trusted publishing](https://docs.pypi.org/trusted-publishers/), so no API token exists anywhere.

1. Bump `version` in `pyproject.toml` (or `uv version --bump patch`) and commit.
2. Tag and push: `git tag v0.1.1 && git push origin v0.1.1`.
3. `.github/workflows/release.yml` runs the checks, verifies the tag matches the version, builds,
   publishes to PyPI and creates a GitHub release.

One-time setup: on pypi.org, add a trusted publisher for project `garmin-health-mcp` with owner
`EmilbMadsen`, repository `garmin-health-mcp`, workflow `release.yml` and environment `pypi`.
