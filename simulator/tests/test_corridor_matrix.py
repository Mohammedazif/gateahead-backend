"""Check both travel directions at every gate, including gates far from the default view."""

import math

import pytest

from gatesim.harness import at_gate, run
from gatesim.run import score, timeline
from gatesim.scenarios import Scenario, express
from gatesim.world import Call, Feed, GATES, STATIONS, SimTrain


def northbound():
    calls = []
    for index, station in enumerate(reversed(STATIONS)):
        minute = round(600 + STATIONS[-1].s - station.s + index)
        calls.append(Call(station.code, minute, minute + 1))
    return SimTrain("66604", "Full corridor northbound MEMU", calls, southbound=False)


@pytest.mark.parametrize("gate", GATES, ids=[gate.road for gate in GATES])
@pytest.mark.parametrize("reverse", [False, True], ids=["southbound", "northbound"])
def test_both_directions_at_every_crossing(gate, reverse):
    train = northbound() if reverse else express()
    arrival = train.gate_time(gate)
    assert arrival is not None
    minutes = [math.floor(arrival) + offset for offset in (-10, -5, -1, 0, 1, 3, 4)]
    feeds = {train.number: Feed(lag=1, refresh=1)}
    scenario = Scenario(f"{train.number}_{gate.node}", "Every crossing before, during, and after passage",
                        [train], train.number, gate.node, at_gate(gate), minutes, feeds)
    looks = run(scenario.trains, feeds, train.number, gate.node, scenario.where, minutes, cache_live=True)
    assert score(looks, scenario.tolerance).ok, timeline(scenario, looks)
