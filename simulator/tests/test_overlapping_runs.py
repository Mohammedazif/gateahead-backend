import asyncio
from dataclasses import replace

import pytest

from gatesim.harness import DAY, at_gate, run, simulated
from gatesim.scenarios import express
from gatesim.world import Feed, GATE_BY_NODE


@pytest.mark.parametrize("cache_live", [False, True])
def test_one_origin_date_can_clear_the_gate_while_the_other_still_approaches(cache_live):
    today = express()
    yesterday = replace(today, start_offset_days=-1, start_delay=1437)
    recent_id = today.run_id(DAY.date())
    older_id = yesterday.run_id(DAY.date())
    gate = GATE_BY_NODE[9004]
    assert today.gate_time(gate) - yesterday.gate_time(gate) == pytest.approx(3)

    async def check():
        with simulated([today, yesterday], {today.number: Feed(lag=0, refresh=1)}, cache_live=cache_live) as (app, clock):
            app.store.save_route(today.number, today.timetable_rows(), today.run_days)
            clock.minute = 624
            prediction = await app.predict_here(*gate.coords)
            scene = await app.scene_here(*gate.coords, gate.id)
            predicted = {row["run_id"]: row["eta"] for row in prediction["gate_predictions"] if row["id"] == gate.id}
            assert predicted == {row["run_id"]: row["eta_min"] for row in scene["trains"]}
            assert set(predicted) == {recent_id, older_id}
            assert predicted[older_id] < predicted[recent_id]
            assert predicted[older_id] == pytest.approx(yesterday.gate_time(gate) - clock.minute, abs=1)
            assert predicted[recent_id] == pytest.approx(today.gate_time(gate) - clock.minute, abs=1)

            clock.minute = 629
            prediction = await app.predict_here(*gate.coords)
            scene = await app.scene_here(*gate.coords, gate.id)
            assert [row["run_id"] for row in prediction["passed"] if gate.id in row["ids"]] == [older_id]
            assert [row["run_id"] for row in scene["passed"]] == [older_id]
            assert [row["run_id"] for row in scene["trains"]] == [recent_id]
            assert scene["trains"][0]["status"] == "closed"

    asyncio.run(check())


def test_harness_compares_the_exact_origin_date_even_for_one_train_number():
    today = express()
    yesterday = replace(today, start_offset_days=-1, start_delay=1437)
    gate = GATE_BY_NODE[9004]
    with pytest.raises(ValueError, match="origin date"):
        run([today, yesterday], {}, today.number, gate.node, at_gate(gate), [624])
    observations = run([today, yesterday], {today.number: Feed(lag=0, refresh=1)}, yesterday.run_id(DAY.date()),
                       gate.node, at_gate(gate), [622, 624])
    final = observations[-1]
    assert final.run_id == yesterday.run_id(DAY.date())
    assert final.rest[0].run_id == today.run_id(DAY.date())
    assert final.app_eta is not None and final.rest[0].app_eta is not None
    assert final.app_eta < final.rest[0].app_eta
