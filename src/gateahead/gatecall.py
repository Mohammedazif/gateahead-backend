"""One train at one crossing: when it gets there, or how long ago it went over.

The train list (/predict) and the tapped-gate view (/scene) both use this, so they always agree.
"""

from __future__ import annotations

from gateahead.config import Settings
from gateahead.geo import stitch_rail
from gateahead.models import Crossing, RouteStop, TrainPosition
from gateahead.predict import PASS_GRACE_MIN, crossing_is_ahead, eta_minutes, rail_passed_ago, route_eta
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
    return merge_call(local_on, local_eta, local_bracket, timed, standing(position))


def standing(position: TrainPosition) -> bool:
    return position.at_station or position.run == "at"


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
