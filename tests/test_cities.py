from gateahead.cities import _CITIES, search_cities


def test_the_catalog_covers_indian_cities():
    assert len(_CITIES) > 1000
    pune = search_cities("pune")[0]
    assert (pune.name, pune.state) == ("Pune", "Maharashtra")


def test_older_names_find_the_current_city():
    assert search_cities("bangalore")[0].name == "Bengaluru"
    assert search_cities("bombay")[0].name == "Mumbai"
    assert search_cities("madras")[0].name == "Chennai"
    assert search_cities("calcutta")[0].name == "Kolkata"
    assert search_cities("baroda")[0].name == "Vadodara"
    assert search_cities("gurgaon")[0].name == "Gurugram"
    assert search_cities("allahabad")[0].name == "Prayagraj"
    assert search_cities("trivandrum")[0].name == "Thiruvananthapuram"
    assert search_cities("mysore")[0].name == "Mysuru"
    assert search_cities("mangalore")[0].name == "Mangaluru"


def test_delhi_prefers_the_larger_city():
    assert search_cities("delhi")[0].name == "Delhi"
    assert search_cities("new delhi")[0].name == "New Delhi"


def test_a_short_or_unknown_query_returns_nothing():
    assert search_cities("a") == []
    assert search_cities("  ") == []
    assert search_cities("zzqq") == []
