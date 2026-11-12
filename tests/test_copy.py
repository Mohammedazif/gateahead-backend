from smartrail.bot.formatters import friendly_error, help_text, predict_text, start_text


def test_errors_are_human():
    assert "429" not in friendly_error("OpenStreetMap error (429)")
    assert "504" not in friendly_error("map_busy")
    assert "try again" in friendly_error("OpenStreetMap busy (504).").lower()


def test_predict_hides_vendor_names():
    text = predict_text(
        {
            "stations": ["PAY"],
            "checked": 10,
            "running": 1,
            "approaching": [],
            "sightings": [{"number": "16610", "name": "Express", "current": "KOZHIKODE", "delay": 0, "age": 191}],
        }
    )
    lower = text.lower()
    assert "osm" not in lower
    assert "railyatri" not in lower
    assert "erail" not in lower
    assert "16610" not in text
    assert "No live trains reported" in text
    closing = predict_text(
        {
            "approaching": [
                {"train": "16348", "name": "Express", "cluster": "A", "eta": 5, "lat": 12.07, "lon": 75.21, "from_you_km": 1.4, "at_station": False, "status": "closing_soon", "current": ""},
                {"train": "16348", "name": "Express", "cluster": "B", "eta": 7, "lat": 12.08, "lon": 75.20, "from_you_km": 0.2, "at_station": False, "status": "closing_soon", "current": ""},
            ],
            "sightings": [{"number": "16348", "name": "Express", "current": "PAYANGADI", "delay": 23, "age": 2}],
        }
    )
    assert "A" in closing and "B" in closing
    assert "closes in 5 min" in closing
    later = predict_text(
        {
            "approaching": [
                {"train": "16649", "name": "Parasuram Express", "cluster": "Trikarpur", "eta": 240, "lat": 12.14, "lon": 75.18, "from_you_km": 0.4, "at_station": False, "status": "later", "current": ""},
            ],
            "sightings": [],
        }
    )
    assert "closes in 4 hr" in later
    assert "maps.google.com" not in closing
    assert "/nearby" in closing
    sample = nearby_sample()
    assert "maps.google.com" in sample


def nearby_sample() -> str:
    from smartrail.bot.formatters import nearby_text
    from smartrail.models import Crossing

    return nearby_text([(Crossing("a", "Gate", 12.08, 75.20, "osm"), 0.2)])


def test_start_has_no_vendor_names():
    lower = start_text().lower()
    assert "openstreetmap" not in lower
    assert "railyatri" not in lower
    assert "/predict" in start_text()
    assert "/subscribe" in start_text()
    assert "—" not in start_text()
    assert "—" not in help_text()


def test_help_lists_every_command():
    text = help_text()
    assert "/help" not in text
    assert "Next," not in text
    for command in (
        "/setlocation",
        "/nearby",
        "/predict",
        "/subscribe",
        "/unsubscribe",
        "/markgate",
    ):
        assert command in text
    lower = text.lower()
    assert "3 minute" not in lower
    assert "10 minute" not in lower
