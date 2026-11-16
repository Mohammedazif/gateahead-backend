"""Every simulated situation at a level crossing, checked minute by minute against the true train."""

from dataclasses import replace
from datetime import datetime

import pytest

from gatesim.harness import run
from gatesim.run import judge, score, timeline
from gatesim.scenarios import express, scenarios
from gatesim.world import BY_CODE, Feed, live_payload

_ALL = scenarios()


def _looks(name: str, **kwargs):
    scenario = next(item for item in _ALL if item.name == name)
    minutes = kwargs.pop("minutes", scenario.minutes)
    return scenario, run(scenario.trains, scenario.feeds, scenario.watch, scenario.gate, scenario.where, minutes, **kwargs)


@pytest.mark.parametrize("scenario", _ALL, ids=[item.name for item in _ALL])
@pytest.mark.parametrize("cache_live", [False, True], ids=["uncached", "shared_snapshot"])
def test_scenario(scenario, cache_live):
    looks = run(scenario.trains, scenario.feeds, scenario.watch, scenario.gate, scenario.where, scenario.minutes,
                cache_live=cache_live)
    assert score(looks, scenario.tolerance).ok, timeline(scenario, looks)


def test_riding_over_the_gate_keeps_it_closed_then_says_passed():
    """The field report: on board, at the gate, the app dropped the train and showed one two hours away."""
    scenario, looks = _looks("rider_on_express")
    on_gate = [look for look in looks if look.true_eta is not None and -1 <= look.true_eta <= 1]
    assert on_gate
    for look in on_gate:
        assert look.app_status == "closed", timeline(scenario, looks)
        assert look.scene_status == "closed", timeline(scenario, looks)
    after = [look for look in looks if look.true_eta is not None and -4.5 <= look.true_eta <= -2.5]
    assert after and all(look.app_passed is not None for look in after), timeline(scenario, looks)


def test_the_barrier_counts_down_to_the_lead_time():
    scenario, looks = _looks("waiting_at_gate")
    for look in looks:
        if look.app_eta is not None:
            assert abs(look.app_closes - (look.app_eta - 5)) < 0.15


def test_the_feed_says_ahead_of_the_last_station_like_railyatri():
    # A minute after leaving Elamkulam, RailYatri would say "1 kms ahead of ELAMKULAM", status D.
    report = live_payload(express(), 627, Feed(lag=0, refresh=1), datetime(2026, 11, 16))
    assert report["status"] == "D"
    assert report["current_station_code"] == "ELM"
    assert report["ahead_distance"] == 1
    assert report["upcoming_stations"][0]["station_code"] == ""
    assert report["upcoming_stations"][1]["station_code"] == "VLR"
    assert report["distance_from_source"] == round(BY_CODE["ELM"].s + 1)


def test_the_judge_catches_a_silent_app_and_an_early_passed_note():
    scenario, looks = _looks("waiting_at_gate", minutes=[627.0])
    look = looks[0]
    assert look.true_status == "closed"
    assert judge(look, scenario.tolerance) == []
    silent = replace(look, app_status=None, app_eta=None, scene_status=None)
    assert {"MISSED", "VIEW_MISSED"} <= set(judge(silent, scenario.tolerance))
    early = replace(look, app_passed=1.0)
    assert "EARLY_PASSED" in judge(early, scenario.tolerance)
    forever_unresolved = replace(look, app_status="awaiting_update", scene_status="awaiting_update",
                                 app_eta=None, scene_eta=None, app_closes=None)
    assert "UNEXPECTED_UNRESOLVED" in judge(forever_unresolved, scenario.tolerance)
    assert "INVALID_ESTIMATE" in judge(replace(look, app_eta=None), scenario.tolerance)
    assert "INVALID_ESTIMATE" in judge(replace(look, scene_eta=float("nan")), scenario.tolerance)


def test_frozen_stopped_report_stays_visible_and_recovers_without_a_false_passage():
    scenario, looks = _looks("stopped_feed_recovers", cache_live=True)
    unresolved = [look for look in looks if 623 <= look.minute <= 630]
    assert unresolved
    for look in unresolved:
        assert look.app_status == look.scene_status == "awaiting_update", timeline(scenario, looks)
        assert look.app_eta is None and look.scene_eta is None
        assert look.app_passed is None and look.scene_passed is None
        assert any(train == "16603" for train, _eta, _status in look.others)
    recovered = next(look for look in looks if look.minute == 632)
    assert recovered.app_status != "awaiting_update"
    assert recovered.app_passed is not None


def test_the_judge_rejects_silent_or_counting_down_stale_stopped_reports():
    scenario, looks = _looks("stale_feed", minutes=[624.0])
    look = looks[0]
    assert judge(look, scenario.tolerance) == []
    assert "STALE_COUNTDOWN" in judge(replace(look, app_status=None), scenario.tolerance)
    assert "STALE_VIEW" in judge(replace(look, scene_status=None), scenario.tolerance)
    assert "STALE_COUNTDOWN" in judge(replace(look, app_eta=0, app_status="closed"), scenario.tolerance)
    assert "STALE_COUNTDOWN" in judge(replace(look, app_passed=1), scenario.tolerance)


def test_simulator_rejects_nonpositive_time_steps():
    from gatesim.harness import minutes_between

    for step in (0, -1, float("nan"), float("inf")):
        with pytest.raises(ValueError):
            minutes_between("10:00", "10:10", step)


def test_unknown_scenario_cannot_silently_report_success():
    from gatesim.run import main

    with pytest.raises(SystemExit) as error:
        main(["misspelled_scenario", "--summary"])
    assert error.value.code == 2


def test_simulation_clock_preserves_the_epoch_across_timezones():
    from datetime import timezone
    from gatesim.harness import DAY, _Clock, _fake_datetime
    from gateahead.clock import INDIA

    clock = _Clock(minute=600)
    fake = _fake_datetime(clock)
    local = fake.now(INDIA)
    utc = fake.now(timezone.utc)
    assert local.hour == 10 and local.minute == 0
    assert utc.hour == 4 and utc.minute == 30
    assert local.timestamp() == utc.timestamp() == DAY.timestamp() + 600 * 60


def test_held_stopped_estimate_cannot_hide_an_arbitrarily_wrong_eta():
    scenario, looks = _looks("stale_feed", minutes=[621.0])
    look = looks[0]
    assert look.stopped_report_ahead and look.report_age < 8
    impossible = replace(look, app_eta=999, scene_eta=999, app_status="later", scene_status="later")
    assert "SAYS_OPEN" in judge(impossible, scenario.tolerance)
    assert any(issue.startswith("OFF") for issue in judge(impossible, scenario.tolerance))
