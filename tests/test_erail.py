import asyncio

from gateahead import erail
from gateahead.erail import parse_train_numbers, parse_train_rows, trains_between


def setup_function() -> None:
    erail._cache.clear()
    erail._names.clear()


def test_parse_caret_payload():
    body = "~~~~~~^16630~MALABAR EXPRESS~MAQ~TVC^16348~MANGALORE EXP~MAQ~TVC"
    assert parse_train_numbers(body) == ["16630", "16348"]


def test_parse_keeps_the_departure_clock():
    body = (
        "^12624~TVC MAS SF MAIL~Thiruvananthapuram Central~TVC~Mgr Chennai Ctr~MAS"
        "~Coimbatore Jn~CBE~Erode Jn~ED~23.45~01.25~01.40~1111111"
    )
    assert parse_train_rows(body) == [("12624", 23 * 60 + 45)]


def test_names_are_parsed_separately_from_numbers_and_stations():
    body = "^16630~ MALABAR   EXPRESS ~MAQ~TVC^16603~MAVELI EXPRESS~MAQ~TVC^16608~16608~X~Y^garbage~Bad^16609~  "
    assert erail.parse_train_names(body) == {"16630": "MALABAR EXPRESS", "16603": "MAVELI EXPRESS"}


def test_discovery_keeps_train_names_without_an_additional_request():
    session = _Session([_Resp(200, "^16630~MALABAR EXPRESS~MAQ~TVC^16603~MAVELI EXPRESS~MAQ~TVC")])
    assert asyncio.run(trains_between(session, "MAQ", "TVC")) == ["16630", "16603"]
    assert asyncio.run(trains_between(session, "maq", "tvc")) == ["16630", "16603"]
    assert erail.known_train_names(["16630", "16603", "00000"]) == {"16630": "MALABAR EXPRESS", "16603": "MAVELI EXPRESS"}
    assert session.calls == 1


_BOARD = '''<html><script>FAKE EXPRESS 99999, 01.00 hr 00:00</script>
<h3>CLT SBC EXPRESS <span>16512</span>, 13.05 hr</h3><p>Daily</p>
<table><tr><td>Payyanur</td><td>17:30</td></tr><tr><td>Ksr Bengaluru</td><td>06:35</td></tr></table>
<h3>MAVELI EXPRESS 16603, 02.43 hr</h3><p>Daily</p><table><tr><td>Payyanur</td><td>19:13</td></tr></table>
</html>'''


def test_station_board_parses_local_departures_instead_of_destination_times():
    rows, names = erail.parse_station_board(_BOARD)
    assert rows == [("16512", 17 * 60 + 30), ("16603", 19 * 60 + 13)]
    assert names == {"16512": "CLT SBC EXPRESS", "16603": "MAVELI EXPRESS"}
    assert erail.parse_station_board("<p>CLT SBC EXPRESS16512, 13.05 hr</p><p>17.30</p>")[0] == [("16512", 1050)]


def test_station_boards_are_cached_and_also_supply_train_names():
    session = _Session([_Resp(200, _BOARD)])
    assert asyncio.run(erail.station_rows(session, "PAY", "Payyanur"))[0] == ("16512", 1050)
    assert asyncio.run(erail.station_rows(session, "pay", "Payyanur"))[0] == ("16512", 1050)
    assert session.calls == 1
    assert erail.known_train_names(["16512"]) == {"16512": "CLT SBC EXPRESS"}


def test_a_failed_or_unreadable_board_is_not_cached_as_no_trains():
    session = _Session([_Resp(500), _Resp(200, "<html>Loading...</html>"), _Resp(200, _BOARD)])
    assert asyncio.run(erail.station_rows(session, "PAY", "Payyanur")) == []
    assert asyncio.run(erail.station_rows(session, "PAY", "Payyanur")) == []
    assert asyncio.run(erail.station_rows(session, "PAY", "Payyanur"))[0][0] == "16512"
    assert session.calls == 3


def test_failed_lookup_is_not_cached():
    session = _Session([_Resp(500), _Resp(200, "~~~~~~^12624~TRICHY EXP~KUY~TUP")])
    assert asyncio.run(trains_between(session, "KUY", "TUP")) == []
    assert asyncio.run(trains_between(session, "kuy", "tup")) == ["12624"]
    assert session.calls == 2


def test_empty_timetable_is_cached():
    session = _Session([_Resp(200, "~~~~~~"), _Resp(200, "~~~~~~^12624~TRICHY EXP~CVM~ANU")])
    assert asyncio.run(trains_between(session, "CVM", "ANU")) == []
    assert asyncio.run(trains_between(session, "CVM", "ANU")) == []
    assert session.calls == 1


class _Session:
    def __init__(self, responses: list["_Resp"]) -> None:
        self.responses = list(responses)
        self.calls = 0

    def get(self, url: str, **kwargs: object) -> "_Resp":
        del url, kwargs
        self.calls += 1
        return self.responses.pop(0)


class _Resp:
    def __init__(self, status: int, body: str = "") -> None:
        self.status = status
        self._body = body

    async def __aenter__(self) -> "_Resp":
        return self

    async def __aexit__(self, *args: object) -> bool:
        del args
        return False

    async def text(self) -> str:
        return self._body
