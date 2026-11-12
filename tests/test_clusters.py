from smartrail.clusters import cluster_crossings, gate_label
from smartrail.models import Crossing


def test_cluster_uses_road_and_station():
    gates = [
        Crossing("a", "Level crossing", 12.080, 75.205, "osm", nearest_station="PAY", road="Kandangali Road"),
        Crossing("b", "Level crossing", 12.0801, 75.2051, "osm", nearest_station="PAY", road="Kandangali Road"),
    ]
    clusters = cluster_crossings(gates)
    assert "Kandangali Road" in clusters[0].name


def test_unnamed_crossing_uses_the_nearest_place():
    gate = Crossing("a", "Level crossing", 8.5, 76.9, "osm", area="Perinad")
    assert gate_label(gate) == "Near Perinad"


def test_named_crossing_keeps_its_name():
    gate = Crossing("a", "NH66", 8.5, 76.9, "osm", area="Perinad")
    assert gate_label(gate) == "NH66 · Perinad"


def test_does_not_merge_gates_kilometres_apart():
    gates = [
        Crossing("a", "A", 12.080, 75.205, "osm", nearest_station="PAY"),
        Crossing("b", "B", 12.099, 75.199, "osm", nearest_station="PAY"),
        Crossing("c", "C", 12.112, 75.217, "osm", nearest_station="PAY"),
    ]
    clusters = cluster_crossings(gates)
    assert len(clusters) >= 2
