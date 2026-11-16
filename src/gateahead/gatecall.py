"""One train at one crossing: when it gets there, or how long ago it went over.

The train list (/predict) and the tapped-gate view (/scene) both use this, so they always agree.
"""

from __future__ import annotations

from gateahead.config import Settings
from gateahead.geo import distance_to_polyline, km, nearest_rail, stitch_rail
from gateahead.models import Crossing, RouteStop, TrainPosition
from gateahead.predict import PASS_GRACE_MIN, REPORT_FRESH_MIN, crossing_is_ahead, eta_minutes, rail_passed_ago, route_eta
from gateahead.timetable import BEND_HALF_KM, Call, merge_call, timetable_call


def crossing_call(
    position: TrainPosition,
    crossing: Crossing,
    rails: list[list[tuple[float, float]]],
    stations: dict[str, tuple[float, float]],
    settings: Settings,
    stops: list[RouteStop] | None = None,
    chain: list[tuple[float, float]] | None = None,
) -> Call:
    local_on = False
    local_eta = None
    local_bracket = None
    if chain is None and rails:
        chain = stitch_rail(rails, crossing.coords, BEND_HALF_KM)
    if chain is not None:
        on_route, eta, bracket = route_eta(position, crossing, chain, stations, settings)
        if on_route and eta is None:
            return Call(None, None, "rail", 0, _rail_passed(position, crossing, chain, stations, settings, stops))
        if on_route:
            local_on = True
            local_eta = eta
            local_bracket = bracket
    if not local_on and crossing_is_ahead(position, crossing):
        chord = eta_minutes(position, crossing, settings)
        if chord is not None:
            local_on = True
            local_eta = chord
    timed = timetable_call(stops, position, crossing, chain) if stops else None
    if timed is not None and timed.inferred_route and _on_separate_mapped_line(position, crossing, rails):
        return Call(None, None, "", 0)
    chosen = merge_call(local_on, local_eta, local_bracket, timed, standing(position))
    if standing(position) and position.age_minutes >= REPORT_FRESH_MIN and chosen.eta is not None:
        return Call(None, chosen.bracket, chosen.source, chosen.slack, awaiting_update=True)
    return chosen


def _on_separate_mapped_line(
    position: TrainPosition, crossing: Crossing, rails: list[list[tuple[float, float]]]
) -> bool:
    """Reject a bend inference contradicted by the train's mapped component.

    Walk the original ways, including forks, so clipped scene segments cannot
    make two pieces of the same long railway appear disconnected.
    """
    seed = nearest_rail(position.coords, rails, max_km=0.15)
    if seed is None:
        return False
    remaining = [(line, set(line)) for line in rails if line and line is not seed]
    pending = [seed]
    while pending:
        line = pending.pop()
        if distance_to_polyline(crossing.coords, line) <= 0.2:
            return False
        points = set(line)
        disconnected = []
        for other, other_points in remaining:
            if points.intersection(other_points) or any(
                km(first, second) <= 0.04
                for first in (line[0], line[-1]) for second in (other[0], other[-1])
            ):
                pending.append(other)
            else:
                disconnected.append((other, other_points))
        remaining = disconnected
    return True


def standing(position: TrainPosition) -> bool:
    return position.at_station or position.run == "at"


def awaiting_report(position: TrainPosition, ids: list[str]) -> dict:
    return {
        "number": position.train_number, "name": position.name,
        "run_id": position.run_id, "start_date": position.start_date,
        "current": position.current_name or position.last_station or "the station",
        "last_station": position.last_station, "age_min": round(position.age_minutes, 1),
        "reason": "stopped_report", "ids": ids,
    }


def passed_ago(call: Call) -> float | None:
    """Minutes since the train went over the gate, if this call says it already has."""
    if call.ago is not None:
        return call.ago
    if call.eta is not None and call.eta < -PASS_GRACE_MIN:
        return -call.eta
    return None


def _rail_passed(position, crossing, chain, stations, settings, stops) -> float | None:
    if stops:
        timed = timetable_call(stops, position, crossing, chain)
        if timed.ago is not None:
            return timed.ago
    return rail_passed_ago(position, crossing, chain, stations, settings)
