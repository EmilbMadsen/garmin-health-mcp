"""HTTP test double: a requests transport adapter that serves the synthetic fixtures.

The real ``GarminPersonalApiClient`` runs unchanged on top of it, so URL building, status
handling and the service's retry/refresh logic are all exercised. No network, no keyring.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
from requests.adapters import BaseAdapter
from requests.structures import CaseInsensitiveDict

from garmin_health_mcp import server as server_module
from garmin_health_mcp.client import GarminService

FIXTURES = Path(__file__).parent / "fixtures" / "garmin"

ROUTES: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"/userprofile-service/userprofile/settings$"), "user_profile_settings"),
    (re.compile(r"/sleep-service/sleep/dailySleep$"), "daily_sleep"),
    (re.compile(r"/sleep-service/sleep/dailySleepSummariesByDate$"), "daily_sleep_summaries"),
    (re.compile(r"/hrv-service/hrv/daily/"), "daily_hrv_summaries"),
    (re.compile(r"/hrv-service/hrv/\d{4}-"), "daily_hrv"),
    (re.compile(r"/bodyBattery/reports/daily$"), "daily_body_battery"),
    (re.compile(r"/stats/stress/daily/"), "daily_stress"),
    (re.compile(r"/stats/steps/daily/"), "daily_steps"),
    (re.compile(r"/stats/hydration/daily/"), "daily_hydration"),
    (re.compile(r"/v2/dailysummaries$"), "daily_summaries"),
    (re.compile(r"/wellness/dailyHeartRate$"), "daily_heart_rate"),
    (re.compile(r"/heartRateZones/v2$"), "heart_rate_zones"),
    (re.compile(r"/spo2acclimation/"), "daily_spo2"),
    (re.compile(r"/fitnessage/v2/"), "fitness_age"),
    (re.compile(r"/weight-service/weight/range/"), "weight_measurements"),
    (re.compile(r"/weightgoal/"), "effective_weight_goals"),
    (re.compile(r"/nutrition-service/food/logs/"), "daily_food_logs"),
    (re.compile(r"/wellnessactivity-service/"), "wellness_activity_summaries"),
    (re.compile(r"/activities/search/activities$"), "search_activities"),
    (re.compile(r"/activity-service/activity/\d+/workouts$"), "activity_workouts"),
    (re.compile(r"/activity-service/activity/\d+$"), "activity"),
]


def fixture(name: str) -> Any:
    return json.loads((FIXTURES / f"{name}.json").read_text(encoding="utf-8"))


@dataclass
class Scripted:
    status: int
    body: str = '{"error": "secret health payload"}'
    headers: Mapping[str, str] = field(default_factory=dict)


class FakeGarmin(BaseAdapter):
    """Serves fixtures by URL path. ``queue`` holds scripted responses served first."""

    def __init__(self) -> None:
        super().__init__()
        self.queue: list[Scripted | Exception] = []
        self.requests: list[tuple[str, dict[str, list[str]], str]] = []
        self.downloads: dict[int, bytes] = {}  # activity id -> ZIP bytes

    def send(  # type: ignore[override]
        self, request: requests.PreparedRequest, **_: Any
    ) -> requests.Response:
        url = urlsplit(request.url or "")
        auth = str(request.headers.get("Authorization", ""))
        self.requests.append((url.path, parse_qs(url.query), auth))
        if self.queue:
            item = self.queue.pop(0)
            if isinstance(item, Exception):
                raise item
            return self._response(request, item.status, item.body, item.headers)
        download = re.search(r"/download-service/files/activity/(\d+)$", url.path)
        if download:
            content = self.downloads.get(int(download.group(1)))
            if content is None:
                return self._response(request, 404, "", {})
            return self._response(request, 200, content, {})
        for pattern, name in ROUTES:
            if pattern.search(url.path):
                body = (FIXTURES / f"{name}.json").read_text(encoding="utf-8")
                return self._response(request, 200, body, {})
        return self._response(request, 404, "", {})

    def close(self) -> None:
        pass

    @staticmethod
    def _response(
        request: requests.PreparedRequest,
        status: int,
        body: str | bytes,
        headers: Mapping[str, str],
    ) -> requests.Response:
        response = requests.Response()
        response.status_code = status
        response._content = body if isinstance(body, bytes) else body.encode()
        response.headers = CaseInsensitiveDict(headers)
        response.url = request.url or ""
        response.request = request
        return response


@dataclass
class Harness:
    garmin: FakeGarmin
    service: GarminService
    tokens: list[bool]  # force_refresh flag of every token request
    sleeps: list[float]


def make_harness() -> Harness:
    garmin = FakeGarmin()
    tokens: list[bool] = []
    sleeps: list[float] = []

    def token(_key: str, force: bool) -> str:
        tokens.append(force)
        return f"token-{len(tokens)}"

    def session() -> requests.Session:
        s = requests.Session()
        s.mount("https://", garmin)
        return s

    service = GarminService(
        api_key_loader=lambda: "test-api-key",
        token_provider=token,
        session_factory=session,
        sleep=sleeps.append,
    )
    return Harness(garmin=garmin, service=service, tokens=tokens, sleeps=sleeps)


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[Harness]:
    h = make_harness()
    monkeypatch.setenv("GARMIN_HEALTH_MCP_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setattr(server_module, "service", h.service)
    monkeypatch.setenv("GARMIN_HEALTH_MCP_TZ", "Europe/Copenhagen")
    yield h
    h.service.close()
