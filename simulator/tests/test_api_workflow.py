"""Exercise real HTTP serialization, prediction, scene, snapshots, and reports without sockets."""

import asyncio
from collections import defaultdict

import httpx

from gateahead.reports import Reports
from gatesim.harness import at_gate, simulated
from gatesim.scenarios import express, later_express
from gatesim.world import Feed, GATE_BY_NODE


def test_cached_provider_snapshot_reuses_fetch_time_and_ages(monkeypatch):
    import gateahead.live as live

    async def exercise():
        gate = GATE_BY_NODE[9004]
        with simulated([express()], {"16601": Feed(lag=1, refresh=1, stall_from=615, stall_to=700)},
                       cache_live=True) as (service, clock), monkeypatch.context() as scoped:
            calls = []
            fetch = live._fetch_live_data

            async def counted_fetch(session, number):
                calls.append((number, clock.minute))
                return await fetch(session, number)

            scoped.setattr(live, "_fetch_live_data", counted_fetch)
            clock.minute = 621
            first = await service.predict_here(*gate.coords)
            snapshot = service.snapshots.peek("live:16601")
            assert first["approaching"][0]["age_min"] == 6
            clock.minute = 622
            second = await service.scene_here(*gate.coords, gate.id)
            assert second["trains"][0]["age_min"] == 7
            assert service.snapshots.peek("live:16601")["fetched_at"] == snapshot["fetched_at"]
            assert calls == [("16601", 621)]
            clock.minute = 623
            third = await service.scene_here(*gate.coords, gate.id)
            assert [row["number"] for row in third["awaiting_update"]] == ["16601"]
            assert calls == [("16601", 621), ("16601", 623)]
            assert third["trains"] == third["passed"] == []

    asyncio.run(exercise())


def test_http_gate_workflow_keeps_stopped_train_and_other_services_separate(tmp_path, monkeypatch):
    # Set before importing the API: even its module-level stores stay in test storage.
    monkeypatch.setenv("GATEAHEAD_DATA", str(tmp_path))
    import gateahead.api.app as api

    async def exercise():
        gate = GATE_BY_NODE[9004]
        reports = Reports(tmp_path / "workflow.db")
        monkeypatch.setattr(api, "_reports", reports)
        monkeypatch.setattr(api, "_HITS", defaultdict(list))
        try:
            with simulated([express(), later_express()],
                           {"16601": Feed(lag=1, refresh=1, stall_from=615, stall_to=630)},
                           cache_live=True) as (service, clock):
                monkeypatch.setattr(api, "_service", service)
                clock.minute = 620
                params = dict(zip(("lat", "lon"), at_gate(gate)(620)))
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api.app), base_url="http://test") as client:
                    nearby = await client.get("/gates", params=params)
                    assert nearby.status_code == 200
                    assert gate.id in {row["id"] for row in nearby.json()["gates"]}
                    prediction = (await client.get("/predict", params=params)).json()
                    scene = (await client.get("/scene", params={**params, "crossing_id": gate.id})).json()
                    per_gate = next(row for row in prediction["gate_predictions"]
                                    if row["id"] == gate.id and row["train"] == "16601")
                    in_scene = next(row for row in scene["trains"] if row["number"] == "16601")
                    assert per_gate["eta"] == in_scene["eta_min"]
                    assert per_gate["status"] == in_scene["status"]
                    report = await client.post("/reports", json={**params, "crossing_id": gate.id,
                        "installation_id": "simulation-device-12345", "state": "open"})
                    assert report.status_code == 200
                    community = (await client.get("/reports", params={"crossing_id": gate.id})).json()
                    assert community["verified"] is False
                    assert community["reports"][0]["state"] == "open"
                    assert "simulation-device-12345" not in str(community)
                    clock.minute = 624
                    prediction = (await client.get("/predict", params=params)).json()
                    scene = (await client.get("/scene", params={**params, "crossing_id": gate.id})).json()
                    assert next(row for row in prediction["awaiting_update"]
                                if row["number"] == "16601")["age_min"] == 9
                    assert [row["number"] for row in scene["awaiting_update"]] == ["16601"]
                    assert any(row["number"] == "16603" for row in scene["trains"])
                    assert not any(row["train"] == "16601" for row in prediction["passed"])
                    assert not any(row["number"] == "16601" for row in scene["passed"])
                    # Community "open" observations cannot resolve the missing train position.
                    assert gate.id in prediction["awaiting_update"][0]["ids"]
                    clock.minute = 632
                    scene = (await client.get("/scene", params={**params, "crossing_id": gate.id})).json()
                    assert not any(row["number"] == "16601" for row in scene["awaiting_update"])
                    assert any(row["number"] == "16601" for row in scene["passed"])
        finally:
            reports.db.close()

    asyncio.run(exercise())
