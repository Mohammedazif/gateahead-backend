from datetime import datetime, timedelta

from gateahead.config import load_settings
from gateahead.geo import km, stitch_rail
from gateahead.models import Crossing, TrainPosition
from gateahead.scene import build_scene

A = (12.00, 75.20)
B = (12.01, 75.20)
C = (12.02, 75.20)


def test_stitch_joins_a_reversed_fragment():
    rail = stitch_rail([[A, B], [C, B]], B, half_km=3)
    assert rail is not None
    assert km(rail[0], A) < 0.2
    assert km(rail[-1], C) < 0.2


def test_stitch_leaves_a_gap_broken():
    far = (12.01, 75.22)
    rail = stitch_rail([[A, B], [far, (12.02, 75.22)]], B, half_km=3)
    assert rail is not None
    assert km(rail[-1], B) < 0.3


def test_scene_places_a_train_on_the_drawn_rail():
    now = datetime(2026, 9, 21, 17, 0)
    crossing = Crossing("g", "Gate", B[0], B[1], "osm")
    train = TrainPosition(
        train_number="16348",
        lat=A[0],
        lon=A[1],
        last_station="CAN",
        next_station="TLY",
        at_station=True,
        delay_minutes=0,
        direction="northbound",
        as_of=now,
        upcoming_codes=("TLY",),
        station_etas=(("TLY", now + timedelta(minutes=20)),),
        name="Express",
        age_minutes=1,
    )
    stations = {"CAN": A, "TLY": C}
    body = build_scene(crossing, [[A, B], [B, C]], [train], stations, load_settings())
    assert body["available"] is True
    assert len(body["rail"]) >= 2
    assert body["trains"][0]["number"] == "16348"
    assert body["trains"][0]["eta_min"] > 0
    assert "railyatri" not in str(body).lower()


def test_scene_without_a_rail_stays_closed():
    crossing = Crossing("g", "Gate", 11.0, 76.0, "osm")
    body = build_scene(crossing, [[A, B]], [], {}, load_settings())
    assert body["available"] is False
    assert body["trains"] == []
