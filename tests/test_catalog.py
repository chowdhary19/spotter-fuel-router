"""Regression checks against the shipped geographic reference data.

These exercise real city records and the actual US polygons, with no geocoding
or routing requests. Land containment is not a claim of driveway access.
"""

import json

import pytest
from django.test import Client, override_settings
from shapely.geometry import Point

from fuel.catalog import city_index, resolve_location, station_catalog, us_boundary
from fuel.errors import PlanningError
from fuel.geo import meters_between


@pytest.fixture(autouse=True)
def fresh_reference_caches():
    for loader in (city_index, station_catalog, us_boundary):
        loader.cache_clear()
    yield
    for loader in (city_index, station_catalog, us_boundary):
        loader.cache_clear()


def test_san_francisco_uses_populated_place_instead_of_offshore_census_point():
    city = resolve_location("San Francisco, CA", "start")
    assert city["geoid"] == "0667000"
    assert (city["latitude"], city["longitude"]) == (37.77493, -122.41942)
    assert city["source_url"] == "https://www.geonames.org/5391959/"
    assert "GeoNames populated-place point" in city["resolution"]
    assert us_boundary().covers(Point(city["longitude"], city["latitude"]))
    # This is the actual original Census record, near the Farallon Islands.
    # Its proximity to an island does not make it a mainland city endpoint.
    original = (-123.032229, 37.727239)
    corrected = (city["longitude"], city["latitude"])
    assert meters_between(original, corrected) > 50_000


def test_boston_uses_populated_place_and_preserves_census_identity():
    city = resolve_location("Boston, MA", "finish")
    assert city["geoid"] == "2507000"
    assert (city["latitude"], city["longitude"]) == (42.35843, -71.05977)
    assert city["source_url"] == "https://www.geonames.org/4930956/"
    assert us_boundary().covers(Point(city["longitude"], city["latitude"]))


def test_city_legal_name_alias_and_case_resolve_to_the_same_corrected_place():
    normal = resolve_location("San Francisco, CA", "start")
    alias = resolve_location("  san francisco city , ca  ", "start")
    assert alias == normal


def test_unresolved_offshore_place_requests_coordinates_instead_of_using_water():
    with pytest.raises(PlanningError) as caught:
        resolve_location("Yankeetown, FL", "start")
    assert caught.value.status == 400
    assert caught.value.code == "city_point_not_routable"
    assert "latitude/longitude" in caught.value.message


@pytest.mark.parametrize(
    "point",
    [
        {"latitude": 37.77493, "longitude": -122.41942},
        {"latitude": 61.2181, "longitude": -149.9003},  # Anchorage
        {"latitude": 21.3069, "longitude": -157.8583},  # Honolulu
    ],
)
def test_exact_us_coordinates_are_preserved_including_alaska_and_hawaii(point):
    result = resolve_location(point, "finish")
    assert result["latitude"] == point["latitude"]
    assert result["longitude"] == point["longitude"]
    assert "user-supplied coordinates" in result["resolution"]


@pytest.mark.parametrize(
    "point",
    [
        {"latitude": 42.3149, "longitude": -83.0364},  # Windsor, across from Detroit
        {"latitude": 49.2827, "longitude": -123.1207},  # Vancouver
        {"latitude": 37.727239, "longitude": -123.032229},  # Offshore SF record
    ],
)
def test_actual_country_polygons_reject_canada_and_offshore_coordinates(point):
    with pytest.raises(PlanningError) as caught:
        resolve_location(point, "start")
    assert caught.value.status == 400
    assert caught.value.code == "outside_usa"


def test_unknown_city_does_not_silently_resolve_to_a_different_place():
    with pytest.raises(PlanningError) as caught:
        resolve_location("No Such Census City XYZ, TX", "start")
    assert caught.value.status == 400
    assert caught.value.code == "unknown_city"


@pytest.mark.parametrize(
    "missing_setting, expected_error",
    [
        ("CITY_DATA_PATH", "city_data_unavailable"),
        ("COUNTRY_DATA_PATH", "boundary_data_unavailable"),
    ],
)
def test_readiness_fails_when_required_geographic_data_is_missing(
    tmp_path, missing_setting, expected_error
):
    stations = tmp_path / "stations.json"
    stations.write_text(
        json.dumps(
            {
                "metadata": {"source": "synthetic-readiness-fixture"},
                "stations": [
                    {
                        "opis_id": "test-station",
                        "name": "Synthetic readiness station",
                        "latitude": 32.7767,
                        "longitude": -96.797,
                        "price_usd_per_gallon": "3.00",
                    }
                ],
            }
        )
    )
    with override_settings(
        STATION_DATA_PATH=stations, **{missing_setting: tmp_path / "missing.json"}
    ):
        response = Client().get("/health/")
    assert response.status_code == 503
    assert response.json()["status"] == "not_ready"
    assert response.json()["error"] == expected_error


@pytest.mark.parametrize(
    "body, expected_codes",
    [
        (
            '{"start":' + "1" * 5000 + ',"finish":"Houston, TX"}',
            {"invalid_json"},
        ),
        # Parser nesting limits vary with Python/runtime configuration. Either
        # a bounded parse error or rejection of this non-object body is valid.
        ("[" * 7000 + "0" + "]" * 7000, {"invalid_json", "invalid_request"}),
    ],
    ids=["integer_conversion_limit", "excessive_json_nesting"],
)
def test_pathological_json_returns_bounded_json_400_without_routing(body, expected_codes):
    response = Client().post("/api/route/", data=body, content_type="application/json")
    assert response.status_code == 400
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.json()["error"]["code"] in expected_codes
