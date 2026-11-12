from __future__ import annotations

from gateahead.geo import distance_to_polyline, meters
from gateahead.models import Crossing, PinCheck


def check_pin(
    lat: float,
    lon: float,
    *,
    crossings: list[Crossing],
    rails: list[list[tuple[float, float]]],
    has_highway: bool,
    rail_max_m: float = 40.0,
    duplicate_m: float = 40.0,
) -> PinCheck:
    point = (lat, lon)
    if not rails:
        return PinCheck("reject", "No railway line found at that pin. Move it onto the tracks and send again.", None)
    rail_m = min(distance_to_polyline(point, line) for line in rails) * 1000.0
    if rail_m > rail_max_m:
        return PinCheck(
            "reject",
            f"That pin is {rail_m:.0f} m from the railway. Move it onto the crossing and send again.",
            rail_m,
        )
    for crossing in crossings:
        if meters(point, crossing.coords) <= duplicate_m:
            return PinCheck("existing", "This crossing is already listed.", rail_m, crossing)
    if not has_highway:
        return PinCheck(
            "candidate",
            "Saved. It sits on the railway; we will treat it as a candidate crossing.",
            rail_m,
        )
    return PinCheck("candidate", "Saved as a crossing near the railway.", rail_m)
