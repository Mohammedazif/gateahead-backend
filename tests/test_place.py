import asyncio

from gateahead.place import clear_place_cache, label_from_address, lookup_place


def setup_function() -> None:
    clear_place_cache()


def test_city_click_names_the_neighbourhood():
    assert (
        label_from_address(
            {
                "suburb": "Mominpura",
                "city": "Nagpur",
                "county": "Nagpur Urban Taluka",
                "state_district": "Nagpur",
                "state": "Maharashtra",
            }
        )
        == "Mominpura, Nagpur"
    )


def test_village_click_names_the_district():
    assert (
        label_from_address(
            {
                "quarter": "Nanthirikkal",
                "suburb": "Kanjirakode",
                "village": "Elampalloor",
                "county": "Kollam",
                "state_district": "Kollam",
                "state": "Kerala",
            }
        )
        == "Nanthirikkal, Kollam"
    )


def test_broad_click_names_the_state():
    assert label_from_address({"state": "Maharashtra", "country": "India"}) == "Maharashtra"
    assert label_from_address({}) is None


def test_lookup_reuses_a_nearby_name():
    session = _Session({"address": {"suburb": "Mominpura", "city": "Nagpur", "state": "Maharashtra"}})
    assert asyncio.run(lookup_place(session, 21.1458, 79.0882)) == "Mominpura, Nagpur"
    assert asyncio.run(lookup_place(session, 21.1459, 79.0883)) == "Mominpura, Nagpur"
    assert session.calls == 1


def test_lookup_keeps_a_miss_from_retrying_immediately():
    session = _Session({"error": "Unable to geocode"})
    assert asyncio.run(lookup_place(session, 8.9, 76.6)) is None
    assert asyncio.run(lookup_place(session, 8.9, 76.6)) is None
    assert session.calls == 1


class _Session:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls = 0

    def get(self, url: str, **kwargs: object) -> "_Body":
        del url, kwargs
        self.calls += 1
        return _Body(self.payload)


class _Body:
    def __init__(self, payload: dict) -> None:
        self.status = 200
        self.payload = payload

    async def __aenter__(self) -> "_Body":
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        return False

    async def json(self, content_type: object = None) -> dict:
        del content_type
        return self.payload
