from __future__ import annotations

from gateahead.config import Settings
from gateahead.clusters import gate_label
from gateahead.geo import along_route, stitch_rail
from gateahead.models import Crossing, RouteStop, TrainPosition
from gateahead.gatecall import awaiting_report, crossing_call, passed_ago, standing
from gateahead.predict import JUST_PASSED_MIN, REPORT_FRESH_MIN, closes_in
from gateahead.timetable import BEND_HALF_KM, list_status

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
            "name": gate_label(crossing),
            "lat": crossing.lat,
            "lon": crossing.lon,
        },
        "rail": [],
        "trains": [],
        "passed": [],
        "awaiting_update": [],
    }
    bend = stitch_rail(rails, crossing.coords, half_km=BEND_HALF_KM)
    if not bend:
        return body
    trains = []
    gone = []
    for position in positions:
        stops = (routes or {}).get(position.train_number)
        chosen = crossing_call(position, crossing, rails, stations, settings, stops, bend)
        if chosen.awaiting_update:
            body["awaiting_update"].append(awaiting_report(position, [crossing.id]))
            continue
        status = None
        if chosen.eta is not None:
            status = list_status(chosen.eta, chosen.slack, settings, chosen.source == "timetable")
        if status is None:
            _note_passed(gone, position, passed_ago(chosen))
            continue
        eta = chosen.eta
        bracket = chosen.bracket
        row = {
            "number": position.train_number,
            "run_id": position.run_id, "start_date": position.start_date,
            "name": position.name,
            "eta_min": round(eta, 1),
            "closes_in_min": round(closes_in(eta, settings), 1),
            "status": status,
            "basis": chosen.source,
            "age_min": round(position.age_minutes, 1),
            "snapshot_at": position.as_of.timestamp(),
            "movement": "stopped" if standing(position) else "moving",
            "current": position.current_name or position.last_station,
            "last_station": position.last_station,
        }
        motion = _motion(position, bend, stations, stops or [], settings) if chosen.slack <= 3 else None
        if motion is not None:
            row["motion"] = motion
        if bracket:
            row["bracket"] = bracket
        trains.append(row)
    trains.sort(key=lambda row: row["eta_min"])
    gone.sort(key=lambda row: row["ago_min"])
    # A barrier can close five minutes before arrival. Include that approach
    # instead of hiding a fresh moving train just outside a 1.5 km view.
    half_km = _DRAW_KM
    for row in trains:
        if row["status"] == "closed" and row.get("motion"):
            approach_km = abs(row["eta_min"]) * row["motion"]["speed_kmh"] / 60
            half_km = max(half_km, min(BEND_HALF_KM, approach_km + 0.35))
    drawn = stitch_rail(rails, crossing.coords, half_km=half_km)
    if not drawn:
        return body
    positions_by_number = {position.run_id: position for position in positions}
    for row in trains:
        position = positions_by_number[row["run_id"]]
        along, offset = along_route(drawn, position.coords)
        row["along_km"] = round(along, 3)
        row["position_on_segment"] = offset <= 0.15
        if row.get("motion") and abs(row["eta_min"]) <= settings.closing_soon_eta_min:
            candidate = stitch_rail(rails, position.coords, half_km=BEND_HALF_KM)
            # Use a train's nearby connected line only when it reaches this
            # crossing corridor. This remains an estimate, not track occupancy.
            if candidate and along_route(candidate, crossing.coords)[1] <= 0.04:
                track_rail = stitch_rail([candidate], crossing.coords, half_km=half_km)
                stops = (routes or {}).get(position.train_number) or []
                motion = _motion(position, candidate, stations, stops, settings)
                if track_rail and motion:
                    track_along, track_off = along_route(track_rail, position.coords)
                    row["track"] = {
                        "estimated": True, "rail": [[lat, lon] for lat, lon in track_rail],
                        "along_km": round(track_along, 3), "position_on_segment": track_off <= 0.15,
                        "motion": motion,
                    }
    body["available"] = True
    body["rail"] = [[lat, lon] for lat, lon in drawn]
    body["trains"] = trains
    body["passed"] = gone
    return body


def _note_passed(gone: list[dict], position: TrainPosition, ago: float | None) -> None:
    if ago is None or ago > JUST_PASSED_MIN:
        return
    gone.append({"number": position.train_number, "run_id": position.run_id, "start_date": position.start_date,
                 "name": position.name, "ago_min": round(ago, 1)})


def _motion(
    position: TrainPosition,
    rail: list[tuple[float, float]],
    stations: dict[str, tuple[float, float]],
    stops: list[RouteStop],
    settings: Settings,
) -> dict | None:
    """Direction on this rail; the client advances the crossing ETA in real time.

    This is an estimated position, not a replay or a GPS observation. A short
    validity window prevents a cached scene from drifting indefinitely.
    """
    if standing(position) or position.age_minutes >= REPORT_FRESH_MIN or settings.default_speed_kmh <= 0:
        return None
    points = {stop.code.upper(): (stop.lat, stop.lon) for stop in stops if stop.lat is not None and stop.lon is not None}
    points.update(stations)
    last = points.get((position.last_station or "").upper())
    nxt = points.get((position.next_station or "").upper())
    direction = None
    if last is not None and nxt is not None:
        before, off_before = along_route(rail, last)
        after, off_after = along_route(rail, nxt)
        if max(off_before, off_after) <= 8 and abs(after - before) > 0.05:
            direction = 1 if after > before else -1
    if direction is None:
        # Only use the reported compass direction on a clearly north/south line.
        north = rail[-1][0] - rail[0][0]
        east = rail[-1][1] - rail[0][1]
        if abs(north) > abs(east) and position.direction != "unknown":
            direction = (1 if north > 0 else -1) * (1 if position.direction == "northbound" else -1)
    if direction is None:
        return None
    return {
        "direction": direction,
        "speed_kmh": settings.default_speed_kmh,
        "valid_for_sec": (REPORT_FRESH_MIN - max(0, position.age_minutes)) * 60,
    }
