from gateahead.live import (
    _choose_lts,
    _keep_notice,
    _normalize_lts,
    _on_the_move,
    _usable,
    classify_live,
)


def test_normalize_dict():
    raw = {"success": True, "current_station_code": "PAY", "at_src": False}
    assert _normalize_lts(raw)["current_station_code"] == "PAY"


def test_normalize_list_picks_running_instance():
    raw = [
        {"success": True, "at_src": True, "train_start_date": "2026-09-21"},
        {"success": True, "at_src": False, "current_station_code": "CAN", "delay": 46},
    ]
    assert _normalize_lts(raw)["current_station_code"] == "CAN"


def test_a_named_origin_is_kept_before_departure():
    waiting = {
        "success": True,
        "at_src": True,
        "at_dstn": False,
        "current_station_code": "MAQ",
        "current_station_name": "MANGALORE CNTL",
        "status": "A",
        "delay": 0,
    }
    assert _on_the_move(waiting) is True
    assert _choose_lts(waiting, None)["current_station_code"] == "MAQ"


def test_an_unnamed_not_started_report_is_dropped():
    today = {"success": True, "at_src": True, "source": "MAQ", "new_message": "Train hasn't started yet."}
    yesterday = {"success": True, "at_dstn": True, "current_station_code": "CAPE"}
    assert _on_the_move(today) is False
    assert _choose_lts(today, yesterday) is None
    look = classify_live(today, yesterday)
    assert look.moving is None
    assert look.finished is False
    assert look.notice


def test_an_expired_payload_is_kept_for_the_timetable():
    raw = {"success": False, "new_message": "This train has already expired"}
    assert _keep_notice(raw)["new_message"] == "This train has already expired"
    assert _normalize_lts(raw) is None
    look = classify_live(raw, None)
    assert look.moving is None
    assert look.finished is False
    assert "expired" in look.notice.lower()
    running = {
        "success": True,
        "at_dstn": False,
        "current_station_code": "KUY",
        "current_station_name": "Tiruppur",
        "status": "T",
    }
    assert classify_live(raw, running).moving["current_station_code"] == "KUY"


def test_not_started_today_uses_yesterdays_running_report():
    today = {"success": True, "at_src": True, "new_message": "Train hasn't started yet."}
    yesterday = {
        "success": True,
        "at_src": False,
        "at_dstn": False,
        "current_station_code": "KUY",
        "current_station_name": "Tiruppur",
        "status": "T",
    }
    assert _on_the_move(today) is False
    assert _choose_lts(today, yesterday)["current_station_code"] == "KUY"


def test_finished_run_is_not_treated_as_live():
    finished = {"success": True, "at_dstn": True, "current_station_code": "MAS"}
    assert _choose_lts(None, finished) is None


def test_late_train_with_current_station_is_usable():
    lts = {"success": True, "at_src": False, "current_station_name": "PAYYANUR", "delay": 46}
    assert _usable(lts) is True


def test_position_keeps_the_run_detail():
    from gateahead.live import position_from_lts

    position = position_from_lts(
        "16649",
        {
            "success": True,
            "current_station_code": "NCJ",
            "current_station_name": "NAGERCOIL JN~",
            "status": "D",
            "delay": 38,
            "etd": "21:01",
            "cur_stn_std": "20:23",
            "platform_number": 1,
            "source_stn_name": "MANGALORE CNTL",
            "dest_stn_name": "KANYAKUMARI",
            "next_stoppage_info": {"next_stoppage": "KANYAKUMARI", "next_stoppage_time_diff": "in 44 mins"},
            "total_distance": 721,
            "distance_from_source": 709,
            "train_name": "Parasuram Express",
        },
    )
    assert position is not None
    assert position.current_name == "Nagercoil Jn"
    assert position.origin_name == "Mangalore Central"
    assert position.dest_name == "Kanyakumari"
    assert position.next_name == "Kanyakumari"
    assert position.next_in == "in 44 min"
    assert position.departed_at == "21:01"
    assert position.scheduled_at == "20:23"
    assert position.platform == "1"
    assert position.left_km == 12
    assert position.covered_km == 709
    assert position.run == "departed"


def test_html_fallback_reads_current_station():
    from gateahead.live import _lts_from_html

    html = 'https://www.railyatri.in/live-train-status-user-feedback?train_no=12617&amp;eta=17:30&amp;start_date=2026-09-21 10:30&amp;current_station=PAY&amp;status=false'
    lts = _lts_from_html(html, "12617")
    assert lts is not None
    assert lts["current_station_code"] == "PAY"
