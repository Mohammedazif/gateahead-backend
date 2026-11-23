"""One train at one crossing: when it gets there, or how long ago it went over.

The train list (/predict) and the tapped-gate view (/scene) both use this, so they always agree.
"""

from __future__ import annotations

from dataclasses import replace

from gateahead.config import Settings
from gateahead.geo import nearest_rail, rail_ends_connect, stitch_rail
from gateahead.models import Crossing, RouteStop, TrainPosition
from gateahead.predict import PASS_GRACE_MIN, REPORT_FRESH_MIN, crossing_is_ahead, eta_minutes, rail_passed_ago, route_eta
from gateahead.timetable import BEND_HALF_KM, UNCERTAIN_ROUTE_SLACK_MIN, Call, merge_call, timetable_call


def crossing_call(
    position: TrainPosition,
    crossing: Crossing,
    rails: list[list[tuple[float, float]]],
    stations: dict[str, tuple[float, float]],
    settings: Settings,
    stops: list[RouteStop] | None = None,
    chain: list[tuple[float, float]] | None = None,
    topology: RailTopology | None = None,
) -> Call:
    if position.observed and (topology or RailTopology(rails)).separate(position, crossing):
        return Call(None, None, "", 0)
    if chain is None and rails:
        chain = stitch_rail(rails, crossing.coords, BEND_HALF_KM)
    if not position.observed:
        timed = timetable_call(stops, position, crossing, chain) if stops else None
        # A published schedule can suggest an approach, but it does not establish passage or closure.
        if timed is None:
            return Call(None, None, "", 0)
        return Call(timed.eta, timed.bracket, "timetable", max(UNCERTAIN_ROUTE_SLACK_MIN, timed.slack))

    reason = _unresolved_reason(position)
    # Use the reported position to test relevance, without simulating a stale train past the gate.
    current = replace(position, age_minutes=0) if reason else position
    local_on = False
    local_eta = None
    local_bracket = None
    coarse = False
    if chain is not None:
        on_route, eta, bracket = route_eta(current, crossing, chain, stations, settings)
        if on_route and eta is None:
            ago = _rail_passed(current, crossing, chain, stations, settings, stops)
            return _final_call(Call(None, None, "rail", 0, ago), reason)
        if on_route:
            local_on = True
            local_eta = eta
            local_bracket = bracket
    if not local_on and crossing_is_ahead(current, crossing):
        chord = eta_minutes(current, crossing, settings)
        if chord is not None:
            local_on = True
            local_eta = chord
            coarse = True
    timed = timetable_call(stops, current, crossing, chain) if stops else None
    chosen = merge_call(local_on, local_eta, local_bracket, timed, standing(current))
    if coarse and chosen.source == "rail":
        chosen = replace(chosen, slack=UNCERTAIN_ROUTE_SLACK_MIN)
    return _final_call(chosen, reason)


def _final_call(call: Call, unresolved_reason: str) -> Call:
    if call.ago is not None:
        if unresolved_reason:
            return replace(call, ago=None)
        if round(call.ago, 6) <= PASS_GRACE_MIN:
            return replace(call, eta=-min(call.ago, PASS_GRACE_MIN), ago=None)
        return call
    if call.eta is not None and (unresolved_reason or call.eta < -PASS_GRACE_MIN):
        return replace(call, eta=None, awaiting_update=True)
    return call


class RailTopology:
    """Use original ways, including forks outside the drawn scene."""

    def __init__(self, rails: list[list[tuple[float, float]]]):
        self._rails = [line for line in rails if len(line) >= 2]
        self._points = {id(line): set(line) for line in self._rails}
        self._nearest: dict[tuple[tuple[float, float], float], list[tuple[float, float]] | None] = {}
        self._components: dict[int, int] = {}

    def separate(self, position: TrainPosition, crossing: Crossing) -> bool:
        seed = self._rail_at(position.coords, 0.15)
        target = self._rail_at(crossing.coords, 0.2)
        if seed is None or target is None or seed is target:
            return False
        root = self._component(seed)
        return self._components.get(id(target)) != root

    def _rail_at(self, point: tuple[float, float], limit: float) -> list[tuple[float, float]] | None:
        key = (point, limit)
        if key not in self._nearest:
            self._nearest[key] = nearest_rail(point, self._rails, max_km=limit)
        return self._nearest[key]

    def _component(self, seed: list[tuple[float, float]]) -> int:
        root = id(seed)
        if root in self._components:
            return self._components[root]
        remaining = [line for line in self._rails if line is not seed and id(line) not in self._components]
        pending = [seed]
        while pending:
            line = pending.pop()
            self._components[id(line)] = root
            points = self._points[id(line)]
            disconnected = []
            for other in remaining:
                if (points.intersection(self._points[id(other)]) or rail_ends_connect(line, other)
                        or rail_ends_connect(other, line)):
                    pending.append(other)
                else:
                    disconnected.append(other)
            remaining = disconnected
        return root


def _on_separate_mapped_line(
    position: TrainPosition, crossing: Crossing, rails: list[list[tuple[float, float]]]
) -> bool:
    return RailTopology(rails).separate(position, crossing)


def standing(position: TrainPosition) -> bool:
    return position.at_station or position.run == "at"


def _unresolved_reason(position: TrainPosition) -> str:
    if not position.report_age_known:
        return "unknown_report_age"
    if position.age_minutes >= REPORT_FRESH_MIN:
        return "stopped_report" if standing(position) else "stale_report"
    return ""


def awaiting_report(position: TrainPosition, ids: list[str]) -> dict:
    return {
        "number": position.train_number, "name": position.name,
        "run_id": position.run_id, "start_date": position.start_date,
        "current": position.current_name or position.last_station or "the station",
        "last_station": position.last_station, "age_min": round(position.age_minutes, 1),
        "reason": _unresolved_reason(position) or "estimate_elapsed", "ids": ids,
    }


def passed_ago(call: Call) -> float | None:
    """Minutes since the train went over the gate, if this call says it already has."""
    return call.ago


def _rail_passed(position, crossing, chain, stations, settings, stops) -> float | None:
    if stops:
        timed = timetable_call(stops, position, crossing, chain)
        if timed.ago is not None:
            return timed.ago
    return rail_passed_ago(position, crossing, chain, stations, settings)
