"""Route-planning contract tests with synthetic stations and explicit road legs."""

from copy import deepcopy
from decimal import Decimal

import pytest

from fuel.errors import PlanningError
from fuel.routing import METERS_PER_MILE, RoadRoute
from fuel.service import build_plan

D = Decimal
START = {"label": "Start", "longitude": -100.0, "latitude": 40.0}
FINISH = {"label": "Finish", "longitude": -90.0, "latitude": 40.0}


def fuel_station(identifier, progress, price):
    return {
        "opis_id": identifier,
        "name": f"Station {identifier}",
        "address": f"{identifier} Test Road",
        "city": "Test City",
        "state": "KS",
        "longitude": -100 + float(progress) / 100,
        "latitude": 40.0,
        "price_usd_per_gallon": str(price),
        "coordinate_source": "synthetic-test-fixture",
        "progress_miles": D(str(progress)),
    }


class SequenceRouter:
    """A fake provider with road distances deliberately unlike projections."""

    def __init__(self, *leg_sets, snap_overrides=None):
        self.leg_sets = leg_sets
        self.calls = 0
        self.requests = []
        self.snap_overrides = snap_overrides or {}

    def route(self, points):
        assert self.calls < len(self.leg_sets), "Unexpected extra routing request"
        legs = tuple(D(str(miles)) * METERS_PER_MILE for miles in self.leg_sets[self.calls])
        assert len(legs) == len(points) - 1, "The test fixture must match its waypoints"
        self.requests.append(tuple(points))
        self.calls += 1
        snapped = list(points)
        for index, location in self.snap_overrides.get(self.calls, {}).items():
            snapped[index] = location
        return RoadRoute(
            coordinates=tuple(points),
            distance_meters=sum(legs, D(0)),
            duration_seconds=1000.0,
            leg_distances_meters=legs,
            snapped_waypoints=tuple(snapped),
        )


def synthetic_catalog(stations):
    return {"stations": stations, "metadata": {"source": "synthetic", "version": "test-1"}}


def project_fixture(monkeypatch, stations):
    monkeypatch.setattr(
        "fuel.service.candidates_on_route",
        lambda route, catalog_stations, *, corridor_miles: deepcopy(stations),
    )


def assert_response_balances(result):
    initial = D(result["initial_fuel_gallons"])
    bought = D(result["fuel_purchased_gallons"])
    consumed = D(result["fuel_consumed_gallons"])
    final = D(result["final_fuel_gallons"])
    assert abs(initial + bought - consumed - final) <= D("0.000002")
    assert 0 <= final <= 50
    assert D(result["total_fuel_cost_usd"]) == sum(
        (D(stop["cost_usd"]) for stop in result["fuel_stops"]), D(0)
    )
    assert abs(bought - sum((D(stop["gallons"]) for stop in result["fuel_stops"]), D(0))) <= D(
        "0.000001"
    ) * (len(result["fuel_stops"]) + 1)
    for stop in result["fuel_stops"]:
        assert D(stop["gallons"]) > 0
        assert 0 <= D(stop["arrival_fuel_gallons"]) <= 50
        assert 0 <= D(stop["departure_fuel_gallons"]) <= 50
        assert abs(
            D(stop["arrival_fuel_gallons"]) + D(stop["gallons"]) - D(stop["departure_fuel_gallons"])
        ) <= D("0.000001")


def test_short_trip_uses_one_route_call_and_explains_zero_purchase_cost(monkeypatch):
    def unexpected_projection(*args, **kwargs):
        pytest.fail("A short trip does not need station-candidate selection")

    monkeypatch.setattr("fuel.service.candidates_on_route", unexpected_projection)
    router = SequenceRouter([100])
    result = build_plan(START, FINISH, D(50), D(0), synthetic_catalog([]), router)
    assert router.calls == 1
    assert result["validation"]["initial_route_api_calls"] == 1
    assert result["fuel_stops"] == []
    assert result["visited_station_ids"] == []
    assert result["total_fuel_cost_usd"] == "0.00"
    assert D(result["fuel_consumed_gallons"]) == 10
    assert D(result["final_fuel_gallons"]) == 40
    assert "already in the tank is excluded" in result["cost_basis"]
    assert result["geojson"]["features"][0]["geometry"]["coordinates"] == tuple(router.requests[0])
    assert_response_balances(result)


def test_exact_range_short_trip_is_valid_without_any_priced_station(monkeypatch):
    project_fixture(monkeypatch, [])
    router = SequenceRouter([500])
    result = build_plan(START, FINISH, D(50), D(0), synthetic_catalog([]), router)
    assert router.calls == 1
    assert D(result["final_fuel_gallons"]) == 0
    assert_response_balances(result)


def test_two_call_plan_recomputes_positions_purchases_and_cost_from_real_road_legs(monkeypatch):
    stations = [fuel_station("a", 300, 4), fuel_station("b", 600, 3)]
    project_fixture(monkeypatch, stations)
    router = SequenceRouter([800], [320, 330, 210])
    result = build_plan(START, FINISH, D(50), D(0), synthetic_catalog(stations), router)
    assert router.calls == 2
    assert [len(points) for points in router.requests] == [2, 4]
    assert D(result["distance_miles"]) == 860
    assert D(result["extra_route_miles"]) == 60
    assert [D(s["route_mile"]) for s in result["fuel_stops"]] == [320, 650]
    assert [D(s["gallons"]) for s in result["fuel_stops"]] == [15, 21]
    assert result["total_fuel_cost_usd"] == "123.00"
    assert D(result["fuel_consumed_gallons"]) == 86
    assert result["road_leg_distances_miles"] == ["320.000", "330.000", "210.000"]
    assert_response_balances(result)


def test_over_range_detour_is_repaired_once_and_revalidated_using_a_third_route(monkeypatch):
    stations = [
        fuel_station("a", 300, 4),
        fuel_station("bridge", 450, 8),
        fuel_station("b", 600, 3),
    ]
    project_fixture(monkeypatch, stations)
    router = SequenceRouter([800], [310, 520, 220], [310, 240, 290, 220])
    result = build_plan(START, FINISH, D(50), D(0), synthetic_catalog(stations), router)
    assert router.calls == 3
    assert [len(points) for points in router.requests] == [2, 4, 5]
    assert result["visited_station_ids"] == ["a", "bridge", "b"]
    assert [s["station_id"] for s in result["fuel_stops"]] == ["a", "bridge", "b"]
    assert [D(s["route_mile"]) for s in result["fuel_stops"]] == [310, 550, 840]
    assert [D(s["gallons"]) for s in result["fuel_stops"]] == [31, 3, 22]
    assert D(result["distance_miles"]) == 1060
    assert result["total_fuel_cost_usd"] == "214.00"
    assert all(D(leg) <= 500 for leg in result["road_leg_distances_miles"])
    assert_response_balances(result)


def test_invalid_third_route_is_not_reported_as_a_valid_plan_or_retried(monkeypatch):
    stations = [
        fuel_station("a", 300, 4),
        fuel_station("bridge", 450, 8),
        fuel_station("b", 600, 3),
    ]
    project_fixture(monkeypatch, stations)
    router = SequenceRouter([800], [310, 520, 220], [310, 501, 29, 220])
    with pytest.raises(PlanningError) as caught:
        build_plan(START, FINISH, D(50), D(0), synthetic_catalog(stations), router)
    assert caught.value.code == "route_not_validated"
    assert router.calls == 3


def test_no_bridge_candidate_returns_coverage_error_instead_of_invented_fuel_stop(monkeypatch):
    stations = [fuel_station("a", 300, 4), fuel_station("b", 600, 3)]
    project_fixture(monkeypatch, stations)
    router = SequenceRouter([800], [310, 520, 220])
    with pytest.raises(PlanningError) as caught:
        build_plan(START, FINISH, D(50), D(0), synthetic_catalog(stations), router)
    assert caught.value.code == "insufficient_verified_stations"
    assert caught.value.details["leg"] == 1
    assert router.calls == 2


def test_empty_initial_tank_can_buy_at_origin_but_not_teleport_to_first_station(monkeypatch):
    stations = [fuel_station("origin", 0, 3), fuel_station("b", 400, 4)]
    project_fixture(monkeypatch, stations)
    router = SequenceRouter([600], [0, 420, 190])
    result = build_plan(START, FINISH, D(0), D(0), synthetic_catalog(stations), router)
    assert [D(s["gallons"]) for s in result["fuel_stops"]] == [50, 11]
    assert result["total_fuel_cost_usd"] == "194.00"
    assert router.calls == 2
    assert_response_balances(result)

    stations = [fuel_station("not_at_origin", 10, 3), fuel_station("b", 400, 4)]
    project_fixture(monkeypatch, stations)
    router = SequenceRouter([600])
    with pytest.raises(PlanningError) as caught:
        build_plan(START, FINISH, D(0), D(0), synthetic_catalog(stations), router)
    assert caught.value.code == "insufficient_verified_stations"
    assert router.calls == 1


def test_destination_reserve_changes_feasibility_and_survives_serialization(monkeypatch):
    project_fixture(monkeypatch, [])
    router = SequenceRouter([450])
    result = build_plan(START, FINISH, D(50), D(5), synthetic_catalog([]), router)
    assert D(result["final_fuel_gallons"]) == 5
    assert D(result["reserve_gallons"]) == 5
    assert_response_balances(result)

    router = SequenceRouter(["450.000001"])
    with pytest.raises(PlanningError) as caught:
        build_plan(START, FINISH, D(50), D(5), synthetic_catalog([]), router)
    assert caught.value.code == "insufficient_verified_stations"


def test_far_snapped_station_cannot_silently_be_counted_as_a_fuel_visit(monkeypatch):
    stations = [fuel_station("a", 300, 4), fuel_station("b", 600, 3)]
    project_fixture(monkeypatch, stations)
    router = SequenceRouter([800], [320, 330, 210], snap_overrides={2: {1: (-96.0, 41.0)}})
    with pytest.raises(PlanningError) as caught:
        build_plan(START, FINISH, D(50), D(0), synthetic_catalog(stations), router)
    assert caught.value.code == "waypoint_too_far_from_road"
    assert caught.value.details["waypoint_index"] == 1
    assert router.calls == 2


def test_displayed_total_reconciles_to_rounded_station_charges(monkeypatch):
    stations = [fuel_station("a", 300, "3.00733333"), fuel_station("b", 600, "3.00111111")]
    project_fixture(monkeypatch, stations)
    router = SequenceRouter([800], [320, 330, 210])
    result = build_plan(START, FINISH, D(50), D(0), synthetic_catalog(stations), router)
    assert result["fuel_stops"][0]["price_usd_per_gallon"] == "3.00733333"
    assert result["total_fuel_cost_usd"] == "108.13"
    assert_response_balances(result)
