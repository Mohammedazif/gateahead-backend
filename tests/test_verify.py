from __future__ import annotations

from smartrail.models import Crossing
from smartrail.verify import check_pin


def test_reject_when_far_from_rail():
    rails = [[(12.14, 75.17), (12.15, 75.17)]]
    result = check_pin(12.14, 75.20, crossings=[], rails=rails, has_highway=True)
    assert result.verdict == "reject"


def test_match_existing_osm_crossing():
    rails = [[(12.1415, 75.1758), (12.1354, 75.1772)]]
    existing = Crossing("osm:node:1", "Thrikaripur", 12.1415, 75.1758, "osm")
    result = check_pin(12.1416, 75.1759, crossings=[existing], rails=rails, has_highway=True)
    assert result.verdict == "existing"
    assert result.match is not None


def test_candidate_when_on_track_and_road():
    rails = [[(12.14, 75.17), (12.15, 75.17)]]
    result = check_pin(12.145, 75.17005, crossings=[], rails=rails, has_highway=True)
    assert result.verdict == "candidate"
