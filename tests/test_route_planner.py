"""Consumer-level synthetic regressions with independently specified road edges."""

from copy import deepcopy
from decimal import Decimal as D

import pytest

from fuel.errors import PlanningError
from fuel.route_planner import build_plan
from fuel.routing_client import METERS_PER_MILE, RoadDistanceMatrix, RoadRoute

START = {"label": "Start", "longitude": -100.0, "latitude": 40.0}
FINISH = {"label": "Finish", "longitude": -90.0, "latitude": 40.0}


def station(name, progress, price):
    return dict(
        opis_id=name,
        name=name,
        address="Test road",
        city="Test",
        state="KS",
        longitude=-100 + progress / 100,
        latitude=40.0,
        price_usd_per_gallon=str(price),
        coordinate_source="synthetic",
        progress_miles=D(progress),
    )


class RoadFixture:
    """Road paths are specified independently of geographic projections."""

    def __init__(self, base, matrix=None, *, table_snap=None, final_delta=0, final_snap=None):
        self.base = D(str(base))
        self.matrix = matrix
        self.table_snap = table_snap or {}
        self.final_snap = final_snap or {}
        self.final_delta = D(str(final_delta))
        self.calls = 0
        self.points = []

    def route(self, points, *, radiuses=None):
        self.calls += 1
        if self.calls == 1:
            legs = (self.base * METERS_PER_MILE,)
        else:
            assert self.calls == 3
            indices = [self.points.index(p) for p in points]
            legs = tuple(
                D(str(self.matrix[a][b])) * METERS_PER_MILE
                for a, b in zip(indices[:-1], indices[1:], strict=True)
            )
            legs = (legs[0] + self.final_delta, *legs[1:])
        snapped = [
            self.final_snap.get(i, p) if self.calls == 3 else p for i, p in enumerate(points)
        ]
        return RoadRoute(tuple(points), sum(legs, D(0)), 1000.0, legs, tuple(snapped))

    def table(self, points, *, radiuses=None):
        self.calls += 1
        assert self.calls == 2
        self.points = points
        snaps = tuple(self.table_snap.get(i, p) for i, p in enumerate(points))
        meters = tuple(
            tuple(None if d is None else D(str(d)) * METERS_PER_MILE for d in row)
            for row in self.matrix
        )
        return RoadDistanceMatrix(meters, tuple(points), snaps, snaps)


def plan(monkeypatch, stations, router, *, initial=50, reserve=0):
    monkeypatch.setattr(
        "fuel.route_planner.select_station_candidates",
        lambda *args: (
            deepcopy(stations),
            {"corridor_miles": 75, "matrix_station_count": len(stations)},
        ),
    )
    return build_plan(
        START,
        FINISH,
        D(initial),
        D(reserve),
        {"stations": stations, "metadata": {"source": "synthetic"}},
        router,
    )


def assert_balances(body):
    initial = D(body["initial_fuel_gallons"])
    bought = D(body["fuel_purchased_gallons"])
    consumed = D(body["fuel_consumed_gallons"])
    final = D(body["final_fuel_gallons"])
    assert abs(initial + bought - consumed - final) <= D("0.000002")
    assert D(body["reserve_gallons"]) <= final <= 50
    assert D(body["total_fuel_cost_usd"]) == sum(
        (D(s["cost_usd"]) for s in body["fuel_stops"]), D(0)
    )
    for s in body["fuel_stops"]:
        assert D(s["gallons"]) > 0
        assert 0 <= D(s["arrival_fuel_gallons"]) <= D(s["departure_fuel_gallons"]) <= 50


def test_zero_purchase_trip_uses_only_one_call(monkeypatch):
    router = RoadFixture(450)
    body = plan(monkeypatch, [], router, reserve=5)
    assert body["total_fuel_cost_usd"] == "0.00"
    assert body["fuel_stops"] == []
    assert router.calls == 1
    assert_balances(body)


def test_cheaper_pump_with_costly_detour_loses_to_cheaper_whole_trip(monkeypatch):
    # A costs $3 but makes the trip 700mi ($60); B costs $3.10,
    # keeps it 600mi and costs $31. Matrix includes asymmetric travel.
    stations = [station("A", 390, 3), station("B", 400, 3.1)]
    router = RoadFixture(
        600, [[0, 450, 400, 600], [None, 0, None, 250], [None, None, 0, 200], [None, None, None, 0]]
    )
    body = plan(monkeypatch, stations, router)
    assert body["visited_station_ids"] == ["B"]
    assert body["total_fuel_cost_usd"] == "31.00"
    assert body["distance_miles"] == "600.000"
    assert router.calls == 3
    assert body["validation"]["matrix_and_final_route_consistent"]
    assert_balances(body)


def test_distant_station_snap_is_removed_before_purchase_optimization(monkeypatch):
    stations = [station("A", 400, 3), station("B", 410, 4)]
    router = RoadFixture(
        600,
        [[0, 400, 410, 600], [None, 0, None, 200], [None, None, 0, 190], [None, None, None, 0]],
        table_snap={1: (-96.0, 40.003)},
    )
    body = plan(monkeypatch, stations, router)
    assert body["visited_station_ids"] == ["B"]
    assert body["total_fuel_cost_usd"] == "40.00"
    excluded = body["validation"]["station_selection"]["excluded_station_snaps"]
    assert excluded[0]["station_id"] == "A"
    assert excluded[0]["snap_distance_meters"] > 333


def test_inaccessible_only_pump_is_not_fuel_at_500_mile_boundary(monkeypatch):
    stations = [station("A", 500, 3)]
    router = RoadFixture(
        600, [[0, 500, 600], [None, 0, 100], [None, None, 0]], table_snap={1: (-95.0, 40.003)}
    )
    with pytest.raises(PlanningError, match="graph cannot cover") as error:
        plan(monkeypatch, stations, router)
    assert error.value.code == "insufficient_verified_stations"
    assert router.calls == 2


def test_unreachable_directed_edges_are_not_treated_as_euclidean_roads(monkeypatch):
    stations = [station("A", 400, 3)]
    router = RoadFixture(600, [[0, None, 600], [400, 0, 200], [600, 200, 0]])
    with pytest.raises(PlanningError) as error:
        plan(monkeypatch, stations, router)
    assert error.value.code == "insufficient_verified_stations"
    assert router.calls == 2


def test_changed_final_road_leg_is_rejected_without_fourth_call(monkeypatch):
    stations = [station("A", 400, 3)]
    router = RoadFixture(600, [[0, 400, 600], [None, 0, 200], [None, None, 0]], final_delta=2)
    with pytest.raises(PlanningError) as error:
        plan(monkeypatch, stations, router)
    assert error.value.code == "route_changed_after_optimization"
    assert router.calls == 3


def test_final_station_snap_cannot_move_to_other_road_node(monkeypatch):
    stations = [station("A", 400, 3)]
    router = RoadFixture(
        600, [[0, 400, 600], [None, 0, 200], [None, None, 0]], final_snap={1: (-96.0, 40.0001)}
    )
    with pytest.raises(PlanningError) as error:
        plan(monkeypatch, stations, router)
    assert error.value.code == "route_changed_after_optimization"


def test_zero_initial_fuel_at_a_real_origin_pump_and_destination_reserve(monkeypatch):
    stations = [station("origin", 0, 3), station("B", 400, 4)]
    matrix = [[0, 0, 400, 600], [0, 0, 400, 600], [None, None, 0, 200], [None, None, None, 0]]
    router = RoadFixture(600, matrix)
    body = plan(monkeypatch, stations, router, initial=0, reserve=5)
    assert body["total_fuel_cost_usd"] == "210.00"
    assert D(body["final_fuel_gallons"]) == 5
    assert_balances(body)


def test_closure_transit_point_is_preserved_even_without_purchase(monkeypatch):
    stations = [station("transit", 100, 10), station("buy", 400, 3)]
    matrix = [[0, 100, 550, 700], [None, 0, 300, 600], [None, None, 0, 200], [None, None, None, 0]]
    body = plan(monkeypatch, stations, RoadFixture(700, matrix))
    assert body["visited_station_ids"] == ["transit", "buy"]
    assert [s["station_id"] for s in body["fuel_stops"]] == ["buy"]
    assert body["distance_miles"] == "600.000"
    assert body["total_fuel_cost_usd"] == "30.00"
    assert_balances(body)


def test_expanded_endpoint_transit_is_not_mistaken_for_a_pump(monkeypatch):
    stations = [station("A", 400, 3)]
    matrix = [[0, 600, 450], [None, 0, 25], [None, 25, 0]]
    body = plan(monkeypatch, stations, RoadFixture(450, matrix), reserve=10)
    assert body["visited_station_ids"] == ["A"]
    assert body["road_leg_distances_miles"] == ["450.000", "25.000", "25.000"]
    assert body["total_fuel_cost_usd"] == "30.00"
    assert_balances(body)


def test_no_catalog_station_does_not_make_network_table_request(monkeypatch):
    router = RoadFixture(600)
    with pytest.raises(PlanningError) as error:
        plan(monkeypatch, [], router)
    assert error.value.code == "insufficient_verified_stations"
    assert router.calls == 1
