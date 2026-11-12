from datetime import datetime

from smartrail.models import Crossing, TrainPosition
from smartrail.predict import crossing_is_ahead


def _pos(**kwargs) -> TrainPosition:
    data = dict(
        train_number="16348",
        lat=11.8745,
        lon=75.3704,
        last_station="CAN",
        next_station="TLY",
        at_station=True,
        delay_minutes=19,
        direction="southbound",
        as_of=datetime(2026, 9, 21, 17, 19),
        upcoming_codes=("TLY", "MAHE", "BDJ", "CLT"),
        current_name="KANNUR",
    )
    data.update(kwargs)
    return TrainPosition(**data)


def test_southbound_at_kannur_does_not_close_payyanur_behind():
    train = _pos()
    pay = Crossing("p", "Payyanur", 12.084, 75.200, "osm", nearest_station="PAY")
    assert crossing_is_ahead(train, pay) is False


def test_upcoming_station_is_ahead():
    train = _pos()
    tly = Crossing("t", "Thalassery", 11.75, 75.49, "osm", nearest_station="TLY")
    assert crossing_is_ahead(train, tly) is True


def test_far_away_train_is_not_ahead_even_if_pay_is_later_on_the_route():
    train = _pos(
        lat=22.8,
        lon=77.9,
        last_station="BNI",
        next_station="ET",
        upcoming_codes=("ET", "KNW", "BSL", "KYN", "PNVL", "MAO", "KAWR", "CAN", "PAY", "CLT"),
        direction="southbound",
        at_station=False,
    )
    pay = Crossing("p", "Payyanur", 12.084, 75.200, "osm", nearest_station="PAY")
    assert crossing_is_ahead(train, pay) is False


def test_crossing_beside_the_train_counts():
    train = _pos(lat=12.084, lon=75.200, last_station="PAY", upcoming_codes=("ELM", "CAN"))
    pay = Crossing("p", "Payyanur", 12.0841, 75.2001, "osm", nearest_station="PAY")
    assert crossing_is_ahead(train, pay) is True
