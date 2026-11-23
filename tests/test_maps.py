import asyncio
from unittest.mock import AsyncMock

from yarl import URL

from gateahead.maps import _follow_map_link, coords_from_url, looks_like_maps, resolve_maps_url


def test_coords_from_google_maps_at():
    url = "https://www.google.com/maps/@12.13541,75.17722,17z"
    assert coords_from_url(url) == (12.13541, 75.17722)


def test_plus_code_in_share_url_with_extra_query(monkeypatch):
    monkeypatch.setattr("gateahead.maps._place_hint", AsyncMock(return_value=(12.085, 75.20)))
    url = (
        "https://www.google.com/maps?q=36J3+4R7+Kandangali+Railway+Gate,+Keralam+670307"
        "&ftid=0x3ba46d3d1a7d7177:0xd7de20011d646837&entry=gps"
    )
    coords = asyncio.run(resolve_maps_url(url))
    assert coords is not None
    lat, lon = coords
    assert 12.07 < lat < 12.09
    assert 75.19 < lon < 75.22


def test_looks_like_maps():
    assert looks_like_maps("https://maps.app.goo.gl/abc")
    assert not looks_like_maps("hello")


def test_public_short_map_link_can_resolve_via_an_approved_redirect():
    requests = []

    class Response:
        status = 302
        headers = {"Location": "https://www.google.com/maps/@12.13541,75.17722,17z"}

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

    class Session:
        def get(self, url, *, allow_redirects, headers):
            requests.append((url.host, allow_redirects))
            return Response()

    result = asyncio.run(_follow_map_link(Session(), URL("https://maps.app.goo.gl/example")))
    assert result == (12.13541, 75.17722)
    assert requests == [("maps.app.goo.gl", False)]
