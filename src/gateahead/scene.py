from __future__ import annotations

from gateahead.config import Settings
from gateahead.geo import along_route, stitch_rail
from gateahead.models import Crossing, TrainPosition
from gateahead.predict import route_eta
from gateahead.timetable import BEND_HALF_KM, list_status, timetable_call, merge_call

_CHAIN_KM = 12.0
_DRAW_KM = 1.5


def build_scene(
    crossing: Crossing,
    rails: list[list[tuple[float, float]]],
    positions: list[TrainPosition],
    stations: dict[str, tuple[float, float]],
    settings: Settings,
    routes: dict[str, list] | None = None,
) -> dict:
    body = {
        "available": False,
        "crossing": {
            "id": crossing.id,
            "name": crossing.name,
            "lat": crossing.lat,
            "lon": crossing.lon,
        },
        "rail": [],
        "trains": [],
    }
    chain = stitch_rail(rails, crossing.coords, half_km=_CHAIN_KM)
    drawn = stitch_rail(rails, crossing.coords, half_km=_DRAW_KM)
    if not chain or not drawn:
        return body
    bend = stitch_rail(rails, crossing.coords, half_km=BEND_HALF_KM)
    trains = []
    for position in positions:
        on_route, eta, bracket = route_eta(position, crossing, chain, stations, settings)
        if on_route and eta is None:
            continue
        stops = (routes or {}).get(position.train_number)
        timed = timetable_call(stops, position, crossing, bend) if stops else None
        chosen = merge_call(on_route, eta, bracket, timed)
        if not on_route or chosen.eta is None:
            continue
        status = list_status(chosen.eta, chosen.slack, settings, chosen.source == "timetable")
        if status is None or status == "later":
            continue
        eta = chosen.eta
        bracket = chosen.bracket
        along, _offset = along_route(drawn, position.coords)
        row = {
            "number": position.train_number,
            "name": position.name,
            "eta_min": round(eta, 1),
            "status": status,
            "along_km": round(along, 3),
            "age_min": round(position.age_minutes, 1),
        }
        if bracket:
            row["bracket"] = bracket
        trains.append(row)
    trains.sort(key=lambda row: row["eta_min"])
    body["available"] = True
    body["rail"] = [[lat, lon] for lat, lon in drawn]
    body["trains"] = trains
    return body
