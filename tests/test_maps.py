from smartrail.maps import coords_from_url, looks_like_maps


def test_coords_from_google_maps_at():
    url = "https://www.google.com/maps/@12.13541,75.17722,17z"
    assert coords_from_url(url) == (12.13541, 75.17722)


def test_plus_code_in_share_url_with_extra_query():
    url = (
        "https://www.google.com/maps?q=36J3+4R7+Kandangali+Railway+Gate,+Keralam+670307"
        "&ftid=0x3ba46d3d1a7d7177:0xd7de20011d646837&entry=gps"
    )
    coords = coords_from_url(url)
    assert coords is not None
    lat, lon = coords
    assert 12.07 < lat < 12.09
    assert 75.19 < lon < 75.22


def test_looks_like_maps():
    assert looks_like_maps("https://maps.app.goo.gl/abc")
    assert not looks_like_maps("hello")
