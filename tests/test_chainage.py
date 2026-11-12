from datetime import datetime, timedelta

from smartrail.config import load_settings
from smartrail.geo import along_route
from smartrail.models import Crossing, TrainPosition
from smartrail.predict import route_eta

ROUTE = [(12.00, 75.20), (12.00, 75.30), (12.08, 75.30)]
STATIONS = {"CAN": (12.00, 75.20), "TLY": (12.08, 75.30)}


def _pos(**kwargs) -> TrainPosition:
    now = datetime(2026, 9, 21, 17, 0)
    data = dict(
        train_number="16348",
        lat=12.00,
        lon=75.20,
        last_station="CAN",
        next_station="TLY",
        at_station=True,
        delay_minutes=0,
        direction="southbound",
        as_of=now,
        upcoming_codes=("TLY",),
        current_name="KANNUR",
        station_etas=(("TLY", now + timedelta(minutes=30)),),
        name="Express",
    )
    data.update(kwargs)
    return TrainPosition(**data)


def test_eta_follows_the_rail_not_the_straight_line():
    crossing = Crossing("g", "Gate", 12.04, 75.30, "osm")
    on_route, eta, bracket = route_eta(_pos(), crossing, ROUTE, STATIONS, load_settings())
    train_km, _ = along_route(ROUTE, (12.00, 75.20))
    cross_km, _ = along_route(ROUTE, crossing.coords)
    next_km, _ = along_route(ROUTE, STATIONS["TLY"])
    expected = 30.0 * (cross_km - train_km) / (next_km - train_km)
    assert on_route is True
    assert eta is not None
    assert abs(eta - expected) < 0.05
    assert bracket == "between CAN and TLY"
    assert eta < 30


def test_crossing_behind_the_train_is_not_given_an_eta():
    train = _pos(lat=12.04, lon=75.30, at_station=False)
    behind = Crossing("g", "Gate", 12.00, 75.20, "osm", nearest_station="TLY")
    on_route, eta, _bracket = route_eta(train, behind, ROUTE, STATIONS, load_settings())
    assert on_route is True
    assert eta is None


def test_a_train_past_the_end_of_the_fetched_rail_is_not_at_the_gate():
    route = [(12.00, 75.20), (12.04, 75.20)]
    stations = {"CAN": (12.00, 75.20), "TLY": (12.10, 75.20)}
    crossing = Crossing("g", "Gate", 12.04, 75.20, "osm")
    train = _pos(lat=12.08, lon=75.20, at_station=False)
    on_route, eta, _bracket = route_eta(train, crossing, route, stations, load_settings())
    assert on_route is False
    assert eta is None


def test_a_station_pin_beside_the_train_does_not_reverse_it():
    south = (11.99, 75.20)
    station = (12.00, 75.20)
    gate = (12.02, 75.20)
    route = [south, station, gate]
    train = _pos(
        lat=11.9990,
        lon=75.20,
        last_station="CAN",
        next_station="KPQ",
        at_station=True,
        station_etas=(),
    )
    crossing = Crossing("g", "North", gate[0], gate[1], "osm")
    on_route, eta, _bracket = route_eta(train, crossing, route, {"CAN": (12.0010, 75.20)}, load_settings())
    assert not (on_route and eta is None)


def test_the_joined_rail_keeps_the_next_gate_ahead_of_the_station():
    from smartrail.service import _crossing_eta

    south = (11.99, 75.20)
    station = (12.00, 75.20)
    gate = (12.02, 75.20)
    nxt = (12.05, 75.20)
    local = [south, station, gate]
    onward = [gate, nxt]
    now = datetime(2026, 9, 21, 17, 0)
    train = _pos(
        lat=11.9990,
        lon=75.20,
        last_station="CAN",
        next_station="KPQ",
        at_station=True,
        station_etas=(("KPQ", now + timedelta(minutes=12)),),
    )
    crossing = Crossing("g", "North", gate[0], gate[1], "osm")
    call = _crossing_eta(
        train,
        crossing,
        [local, onward],
        {"CAN": (12.0010, 75.20), "KPQ": nxt},
        load_settings(),
    )
    assert call.eta is not None
    assert call.eta > 0


def test_crossing_off_the_rail_falls_through():
    crossing = Crossing("g", "Gate", 12.20, 75.50, "osm", nearest_station="TLY")
    on_route, eta, _bracket = route_eta(_pos(), crossing, ROUTE, STATIONS, load_settings())
    assert on_route is False
    assert eta is None
