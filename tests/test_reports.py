from gateahead.reports import Reports


def test_reports_replace_a_devices_previous_observation_and_expire(tmp_path, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("gateahead.reports.time.time", lambda: clock[0])
    reports = Reports(tmp_path / "reports.db")
    reports.submit("gate1", "installation-123456", "closed")
    clock[0] += 60
    latest = reports.submit("gate1", "installation-123456", "open")
    assert latest["verified"] is False
    assert [row["state"] for row in latest["reports"]] == ["open"]
    assert latest["reports"][0]["expires_at"] == 1660
    reports.submit("gate1", "installation-654321", "closed")
    assert len(reports.recent("gate1")["reports"]) == 2
    assert reports.recent("gate2")["reports"] == []
    clock[0] += 600
    assert reports.recent("gate1")["reports"] == []


def test_reporter_identifiers_are_not_returned_or_stored_verbatim(tmp_path):
    reports = Reports(tmp_path / "reports.db")
    result = reports.submit("gate1", "installation-123456", "closed")
    assert set(result["reports"][0]) == {"state", "at", "expires_at"}
    reporter = reports.db.execute("SELECT reporter FROM gate_reports").fetchone()[0]
    assert reporter != "installation-123456"
