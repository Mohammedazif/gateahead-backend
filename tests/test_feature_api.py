import asyncio
from collections import defaultdict
from types import SimpleNamespace

import httpx
import pytest

from gateahead.models import Crossing
from gateahead.reports import Reports


@pytest.fixture
def feature_api(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEAHEAD_DATA", str(tmp_path))
    import gateahead.api.app as api

    gate = Crossing("osm:1", "My gate", 12.1, 75.2, "osm")

    class Service:
        prediction_calls = 0

        async def map_around(self, lat, lon):
            return SimpleNamespace(error=None, crossings=[gate])

        async def predict_here(self, lat, lon):
            self.prediction_calls += 1
            return {"approaching": []}

        async def scene_here(self, lat, lon, crossing_id):
            return {"available": False}

    service = Service()
    monkeypatch.setattr(api, "_service", service)
    monkeypatch.setattr(api, "_reports", Reports(tmp_path / "reports.db"))
    monkeypatch.setattr(api, "_HITS", defaultdict(list))
    return api, service


def test_community_reports_use_no_live_train_lookup_and_validate_input(feature_api):
    api, service = feature_api

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            body = {"crossing_id": "osm:1", "lat": 12.1, "lon": 75.2,
                    "installation_id": "installation-123456", "state": "closed"}
            response = await client.post("/reports", json=body)
            assert response.status_code == 200
            assert response.json()["verified"] is False
            assert response.json()["reports"][0]["state"] == "closed"
            assert (await client.get("/reports", params={"crossing_id": "osm:1"})).json()["reports"]
            assert (await client.post("/reports", json={**body, "state": "confirmed"})).status_code == 422
            assert (await client.post("/reports", json={**body, "lat": 100})).status_code == 422
            assert (await client.post("/reports", json={**body, "crossing_id": "missing"})).status_code == 404
            assert service.prediction_calls == 0

    asyncio.run(run())


def test_predictions_and_scenes_share_a_limit_and_return_retry_after(feature_api):
    api, _service = feature_api

    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
            for _ in range(20):
                assert (await client.get("/predict", params={"lat": 12.1, "lon": 75.2})).status_code == 200
            limited = await client.get("/scene", params={"lat": 12.1, "lon": 75.2, "crossing_id": "osm:1"})
            assert limited.status_code == 429
            assert 1 <= int(limited.headers["retry-after"]) <= 600
            # Reading reports does not consume or bypass the prediction limit.
            assert (await client.get("/reports", params={"crossing_id": "osm:1"})).status_code == 200

    asyncio.run(run())
