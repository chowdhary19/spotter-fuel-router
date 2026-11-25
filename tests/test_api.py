"""HTTP contract, input validation, cached maps and error handling."""

import json
from decimal import Decimal
from unittest.mock import Mock

import pytest
from django.core.cache import cache
from django.test import Client, override_settings
from shapely.geometry import box

from fuel.errors import PlanningError
from fuel.routing_client import METERS_PER_MILE, RoadRoute


@pytest.fixture
def api(monkeypatch):
    cache.clear()
    metadata = {"source": "tiny-test-fixture", "version": "1"}
    catalog = {"metadata": metadata, "stations": []}
    monkeypatch.setattr("fuel.route_planner.station_catalog", lambda: catalog)
    monkeypatch.setattr("fuel.views.station_catalog", lambda: catalog)
    monkeypatch.setattr("fuel.location_catalog.us_boundary", lambda: box(-125, 24, -65, 50))
    # This fixture is deliberately tiny; country-boundary data quality is tested
    # separately from HTTP behavior. Paris is unambiguously outside this box.
    cities = {
        ("austin", "TX"): {
            "austin": {
                "name": "Austin",
                "official_name": "Austin city",
                "state": "TX",
                "geoid": "austin",
                "longitude": -97.7431,
                "latitude": 30.2672,
            }
        },
        ("dallas", "TX"): {
            "dallas": {
                "name": "Dallas",
                "official_name": "Dallas city",
                "state": "TX",
                "geoid": "dallas",
                "longitude": -96.7970,
                "latitude": 32.7767,
            }
        },
        ("springfield", "IL"): {
            "one": {
                "name": "Springfield",
                "official_name": "Springfield city",
                "state": "IL",
                "geoid": "one",
                "longitude": -89.65,
                "latitude": 39.78,
            },
            "two": {
                "name": "Springfield",
                "official_name": "Springfield township",
                "state": "IL",
                "geoid": "two",
                "longitude": -89.7,
                "latitude": 39.8,
            },
        },
    }
    monkeypatch.setattr("fuel.location_catalog.city_index", lambda: cities)
    routed = []

    class ShortTripRouter:
        def __init__(self):
            self.calls = 0

        def route(self, points, *, radiuses=None):
            self.calls += 1
            routed.append(tuple(points))
            meters = Decimal(100) * METERS_PER_MILE
            return RoadRoute(tuple(points), meters, 3600.0, (meters,), tuple(points))

    monkeypatch.setattr("fuel.route_planner.OSRMClient", ShortTripRouter)
    yield Client(), routed, catalog
    cache.clear()


def post(client, value):
    return client.post("/api/route/", data=json.dumps(value), content_type="application/json")


def request_body():
    return {"start": "Austin, TX", "finish": "Dallas, TX"}


def test_map_page_sends_origin_referrer_for_compliant_osm_tile_requests(api):
    client, _, _ = api
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"


def assert_error(response, status, code):
    assert response.status_code == status
    assert response.headers["Content-Type"].startswith("application/json")
    assert response.json()["error"]["code"] == code


def test_post_returns_route_geojson_fuel_accounting_and_openable_map(api):
    client, routed, _ = api
    response = post(client, request_body())
    assert response.status_code == 200
    body = response.json()
    assert body["distance_miles"] == "100.000"
    assert body["fuel_stops"] == []
    assert body["total_fuel_cost_usd"] == "0.00"
    assert body["fuel_consumed_gallons"] == "10.000000"
    assert body["final_fuel_gallons"] == "40.000000"
    assert "already in the tank is excluded" in body["cost_basis"]
    assert body["performance"]["route_api_calls"] == 1
    assert body["performance"]["cache_hit"] is False
    assert len(routed) == 1
    assert body["geojson"]["type"] == "FeatureCollection"
    assert [f["properties"]["kind"] for f in body["geojson"]["features"]] == [
        "route",
        "start",
        "finish",
    ]
    assert body["map_url"] == f"http://testserver/routes/{body['route_id']}/map/"
    map_response = client.get(body["map_url"])
    assert map_response.status_code == 200
    assert b'id="saved-route"' in map_response.content
    assert body["route_id"].encode() in map_response.content


def test_identical_request_is_cached_and_cached_result_is_not_mutated(api):
    client, routed, _ = api
    first = post(client, request_body()).json()
    second = post(client, request_body()).json()
    assert len(routed) == 1
    assert second["route_id"] == first["route_id"]
    assert first["performance"]["route_api_calls"] == 1
    assert second["performance"]["route_api_calls"] == 0
    assert second["performance"]["cache_hit"] is True
    assert cache.get("route:" + first["route_id"])["map_url"].startswith("/routes/")
    assert "performance" not in cache.get("route:" + first["route_id"])


def test_cache_hit_restores_a_map_that_was_evicted_independently(api):
    client, routed, _ = api
    first = post(client, request_body()).json()
    cache.delete("route:" + first["route_id"])
    second = post(client, request_body()).json()
    assert second["performance"]["cache_hit"] is True
    assert len(routed) == 1
    assert client.get(second["map_url"]).status_code == 200


def test_changed_catalog_version_invalidates_cached_plan(api):
    client, routed, catalog = api
    first = post(client, request_body()).json()
    catalog["metadata"]["version"] = "2"
    second = post(client, request_body()).json()
    assert len(routed) == 2
    assert second["route_id"] != first["route_id"]
    assert second["performance"]["cache_hit"] is False


def test_changed_fuel_inventory_is_not_served_an_incompatible_cached_plan(api):
    client, routed, _ = api
    first = post(client, request_body()).json()
    second = post(client, request_body() | {"initial_fuel_gallons": 20}).json()
    assert len(routed) == 2
    assert second["route_id"] != first["route_id"]
    assert second["final_fuel_gallons"] == "10.000000"


def test_non_post_method_and_non_json_content_type_are_explicit(api):
    client, routed, _ = api
    response = client.get("/api/route/")
    assert_error(response, 405, "method_not_allowed")
    assert response.headers["Allow"] == "POST"
    assert_error(client.post("/api/route/", data=request_body()), 415, "unsupported_media_type")
    assert routed == []


@pytest.mark.parametrize("raw", [b"{not JSON", b"", b"\xff\xfe"])
def test_malformed_json_is_rejected_before_routing(api, raw):
    client, routed, _ = api
    response = client.post(
        "/api/route/",
        data=raw,
        content_type="application/json",
        headers={"Content-Type": "application/json"},
    )
    assert_error(response, 400, "invalid_json")
    assert routed == []


@pytest.mark.parametrize("body", [None, [], "hello", {}, {"start": "Austin, TX"}])
def test_request_requires_an_object_with_both_endpoints(api, body):
    client, routed, _ = api
    assert_error(post(client, body), 400, "invalid_request")
    assert routed == []


def test_unknown_fields_are_rejected_instead_of_ignored(api):
    client, routed, _ = api
    response = post(client, request_body() | {"initial_fuel_gallon": 0})
    assert_error(response, 400, "unknown_fields")
    assert response.json()["error"]["details"]["fields"] == ["initial_fuel_gallon"]
    assert routed == []


@pytest.mark.parametrize("value", [-1, 51, True, None, "NaN", "Infinity", "not a number"])
@pytest.mark.parametrize("field", ["initial_fuel_gallons", "reserve_gallons"])
def test_invalid_fuel_quantities_have_a_json_error_without_network(api, field, value):
    client, routed, _ = api
    assert_error(post(client, request_body() | {field: value}), 400, "invalid_fuel_quantity")
    assert routed == []


def test_explicit_coordinates_inside_us_boundary_are_accepted(api):
    client, routed, _ = api
    body = {
        "start": {"longitude": -97.7431, "latitude": 30.2672},
        "finish": {"longitude": -96.7970, "latitude": 32.7767},
    }
    response = post(client, body)
    assert response.status_code == 200
    assert len(routed) == 1
    assert "user-supplied coordinates" in response.json()["start"]["resolution"]


def test_foreign_coordinates_are_rejected_before_any_routing_call(api):
    client, routed, _ = api
    body = request_body() | {"start": {"longitude": 2.3522, "latitude": 48.8566}}
    assert_error(post(client, body), 400, "outside_usa")
    assert routed == []


@pytest.mark.parametrize(
    "coordinates",
    [
        {"latitude": 91, "longitude": -100},
        {"latitude": 40, "longitude": -181},
        {"latitude": True, "longitude": -100},
        {"latitude": "40", "longitude": -100},
        {"latitude": 10**400, "longitude": -100},
    ],
)
def test_invalid_coordinates_are_a_json_client_error(api, coordinates):
    client, routed, _ = api
    assert_error(post(client, request_body() | {"start": coordinates}), 400, "invalid_coordinates")
    assert routed == []


def test_ambiguous_city_requires_coordinates_and_exposes_the_choices(api):
    client, routed, _ = api
    response = post(client, request_body() | {"start": "Springfield, IL"})
    assert_error(response, 400, "ambiguous_city")
    assert len(response.json()["error"]["details"]["matches"]) == 2
    assert routed == []


def test_unknown_or_unqualified_city_does_not_get_a_guessed_location(api):
    client, routed, _ = api
    assert_error(post(client, request_body() | {"start": "Nowhere, TX"}), 400, "unknown_city")
    assert_error(post(client, request_body() | {"start": "Austin"}), 400, "invalid_location")
    assert routed == []


def test_oversized_body_has_json_413_even_when_django_body_limit_triggers(api):
    client, routed, _ = api
    body = json.dumps(request_body() | {"padding": "x" * 17000})
    with override_settings(DATA_UPLOAD_MAX_MEMORY_SIZE=16384):
        response = client.post("/api/route/", data=body, content_type="application/json")
    assert_error(response, 413, "request_too_large")
    assert routed == []


def test_provider_failure_remains_typed_and_does_not_poison_the_cache(api, monkeypatch):
    client, routed, _ = api
    failing_router = Mock()
    failing_router.route.side_effect = PlanningError(
        "routing_unavailable", "Provider failed", status=502
    )
    monkeypatch.setattr("fuel.route_planner.OSRMClient", lambda: failing_router)
    for _ in range(2):
        assert_error(post(client, request_body()), 502, "routing_unavailable")
    assert failing_router.route.call_count == 2
    assert routed == []


def test_busy_planner_returns_retry_after_without_starting_a_route(api, monkeypatch):
    client, routed, _ = api
    lock = Mock()
    lock.acquire.return_value = False
    monkeypatch.setattr("fuel.route_planner._planning_lock", lock)
    response = post(client, request_body())
    assert_error(response, 503, "planner_busy")
    assert response.headers["Retry-After"] == "3"
    lock.release.assert_not_called()
    assert routed == []


def test_unknown_map_id_and_missing_map_are_distinct_json_errors(api):
    client, _, _ = api
    assert_error(client.get("/routes/not-an-id/map/"), 404, "invalid_route_id")
    assert_error(client.get("/routes/" + "a" * 32 + "/map/"), 404, "map_expired")


def test_health_does_not_claim_ready_when_no_station_coordinates_loaded(api):
    client, _, _ = api
    response = client.get("/health/")
    assert response.status_code == 503
    assert response.json() == {"status": "missing_station_coordinates", "routable_stations": 0}
