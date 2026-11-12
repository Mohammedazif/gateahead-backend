from gateahead.config import database_path


def test_an_existing_database_keeps_its_rows_under_the_new_name(tmp_path):
    legacy = tmp_path / "smartrail.db"
    legacy.write_bytes(b"rows")
    wal = tmp_path / "smartrail.db-wal"
    wal.write_bytes(b"wal")

    path = database_path(tmp_path)

    assert path == tmp_path / "gateahead.db"
    assert path.read_bytes() == b"rows"
    assert (tmp_path / "gateahead.db-wal").read_bytes() == b"wal"
    assert not legacy.exists()
    assert database_path(tmp_path) == path
