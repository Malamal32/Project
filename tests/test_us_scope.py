from pipeline.us_scope import classify_location


def test_city_state_pattern_wins_even_for_a_name_shared_with_a_non_us_place():
    result = classify_location("Dublin, CA")
    assert result.is_us
    assert result.location_state == "CA"
    assert result.reason.startswith("city_state_pattern")


def test_dublin_ireland_is_excluded():
    result = classify_location("Dublin, Ireland")
    assert not result.is_us
    assert "non_us_location_match" in result.reason


def test_us_remote_prefix_is_included():
    result = classify_location("US-Remote")
    assert result.is_us
    assert result.is_remote


def test_remote_dash_us_colon_pattern_is_included():
    result = classify_location("Remote - US: Select locations")
    assert result.is_us
    assert result.is_remote


def test_remote_canada_is_excluded():
    result = classify_location("Remote - Canada: Select locations")
    assert not result.is_us
    assert "canada" in result.reason


def test_bare_remote_with_no_country_cue_is_excluded_not_guessed():
    result = classify_location("Remote")
    assert not result.is_us
    assert result.reason == "ambiguous_remote_no_country_cue"


def test_missing_location_is_excluded():
    result = classify_location(None)
    assert not result.is_us
    assert result.reason == "no_location_data"


def test_unambiguous_city_without_state_is_included():
    result = classify_location("San Francisco")
    assert result.is_us
    assert result.location_state == "CA"


def test_multi_city_string_with_us_prefix_is_included():
    result = classify_location("US-New York, US-Chicago, US-Atlanta")
    assert result.is_us
    assert result.reason == "us_prefix_segment"


def test_canada_country_code_prefix_list_is_excluded_not_read_as_california():
    # Regression: "CA-Toronto, CA-Montreal" must not be misread as "..., CA" (a
    # trailing California state suffix) by the city_state_pattern regex.
    result = classify_location("CA-Toronto, CA-Montreal, CA-Vancouver")
    assert not result.is_us
    assert result.reason == "non_us_prefix_segment:ca"


def test_california_prefix_segment_with_us_city_is_included():
    result = classify_location("CA-San Francisco, CA-Oakland")
    assert result.is_us
    assert result.location_state == "CA"


def test_in_office_is_not_read_as_an_indiana_prefix_segment():
    # Regression: "In-Office" describes a work arrangement, not a place — it must
    # not match the "XX-City" prefix convention just because it fits the shape.
    result = classify_location("In-Office")
    assert result.reason != "state_prefix_segment:IN"


def test_on_site_is_not_read_as_a_prefix_segment():
    result = classify_location("On-Site")
    assert not result.reason.startswith("state_prefix_segment") and not result.reason.startswith("non_us_prefix_segment")
