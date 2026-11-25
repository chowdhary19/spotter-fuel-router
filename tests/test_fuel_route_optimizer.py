"""Graph optimizer contracts and an independent, exhaustive integer-fuel oracle."""

import heapq
import random
from decimal import Decimal
from fractions import Fraction

import pytest

from fuel.fuel_route_optimizer import (
    GraphStation,
    InfeasibleGraphPlan,
    InvalidGraphInput,
    optimize_station_graph,
)

D = Decimal


def matrix(size, edges):
    result = [[None] * size for _ in range(size)]
    for i in range(size):
        result[i][i] = D(0)
    for (origin, finish), miles in edges.items():
        result[origin][finish] = D(str(miles))
    return result


def assert_plan(plan, stations, roads):
    assert plan.node_indices[0] == 0
    assert plan.node_indices[-1] == len(stations) + 1
    assert len(plan.leg_distances_miles) == len(plan.node_indices) - 1
    events = {purchase.path_index: purchase for purchase in plan.purchases}
    assert len(events) == len(plan.purchases)
    fuel = plan.initial_fuel_gallons
    bought = D(0)
    cost = D(0)
    distance = D(0)
    for index, (origin, finish) in enumerate(
        zip(plan.node_indices[:-1], plan.node_indices[1:], strict=True)
    ):
        if purchase := events.get(index):
            assert purchase.node_index == origin
            assert purchase.identifier == stations[origin - 1].identifier
            assert purchase.gallons > 0
            assert purchase.arrival_fuel_gallons == fuel
            fuel += purchase.gallons
            bought += purchase.gallons
            assert purchase.departure_fuel_gallons == fuel
            assert purchase.cost_usd == (
                purchase.gallons * D(str(stations[origin - 1].price_usd_per_gallon))
            )
            cost += purchase.cost_usd
        assert 0 <= fuel <= plan.tank_capacity_gallons
        edge = roads[origin][finish]
        assert edge is not None
        assert plan.leg_distances_miles[index] == D(str(edge))
        distance += D(str(edge))
        fuel -= D(str(edge)) / plan.mpg
        assert fuel >= 0
    assert fuel == plan.final_fuel_gallons >= plan.reserve_gallons
    assert distance == plan.total_distance_miles
    assert bought == plan.fuel_purchased_gallons
    assert cost == plan.total_cost_usd
    assert plan.initial_fuel_gallons + bought - distance / plan.mpg == fuel


def integer_fuel_oracle(roads, prices, capacity, initial, reserve):
    """Enumerate buy-one-unit and traverse-one-edge actions on the original graph.

    No closure, station-price exchange rule, next-cheaper logic, or critical
    states are used. Integer edge consumption and inventory imply that the
    continuous linear refueling problem has an integer optimum for these cases.
    Fraction prices avoid sharing Decimal calculations with the implementation.
    """
    end = len(roads) - 1
    origin = (0, initial)
    best = {origin: (Fraction(0), 0, 0)}
    queue = [(Fraction(0), 0, 0, *origin)]
    while queue:
        cost, distance, legs, node, fuel = heapq.heappop(queue)
        score = (cost, distance, legs)
        if best.get((node, fuel)) != score:
            continue
        if node == end and fuel >= reserve:
            return score
        actions = []
        if prices[node] is not None and fuel < capacity:
            actions.append((node, fuel + 1, cost + prices[node], distance, legs))
        for neighbor, consumption in enumerate(roads[node]):
            if neighbor != node and consumption is not None and consumption <= fuel:
                actions.append(
                    (neighbor, fuel - consumption, cost, distance + consumption, legs + 1)
                )
        for next_node, next_fuel, next_cost, next_distance, next_legs in actions:
            state = (next_node, next_fuel)
            candidate = (next_cost, next_distance, next_legs)
            if state not in best or candidate < best[state]:
                best[state] = candidate
                heapq.heappush(queue, (*candidate, *state))
    return None


def test_costlier_pump_wins_when_cheaper_pump_requires_a_long_detour():
    stations = [GraphStation("detour", D("3.00")), GraphStation("on-route", D("3.10"))]
    roads = matrix(4, {(0, 1): 450, (1, 3): 250, (0, 2): 400, (2, 3): 200, (0, 3): 600})
    plan = optimize_station_graph(stations, roads)
    assert plan.node_indices == (0, 2, 3)
    assert plan.total_distance_miles == 600
    assert plan.total_cost_usd == 31
    assert plan.fuel_purchased_gallons == 10
    assert_plan(plan, stations, roads)


def test_nonmetric_distance_closure_preserves_a_nonpurchasing_transit_waypoint():
    stations = [GraphStation("transit", D(9)), GraphStation("refill", D(3))]
    roads = matrix(
        4,
        {(0, 1): 100, (1, 2): 100, (0, 2): 600, (2, 3): 400, (0, 3): 1000},
    )
    plan = optimize_station_graph(stations, roads)
    assert plan.node_indices == (0, 1, 2, 3)
    assert [p.identifier for p in plan.purchases] == ["refill"]
    assert plan.purchases[0].path_index == 2
    assert plan.total_cost_usd == 30
    assert_plan(plan, stations, roads)


def test_a_profitable_station_detour_can_revisit_a_node_without_looping_forever():
    stations = [GraphStation("expensive", D(10)), GraphStation("cheap", D(1))]
    roads = matrix(4, {(0, 1): 1, (1, 2): 1, (2, 1): 1, (1, 3): 3})
    plan = optimize_station_graph(
        stations, roads, initial_fuel_gallons=1, tank_capacity_gallons=4, mpg=1
    )
    assert plan.node_indices == (0, 1, 2, 1, 3)
    assert plan.total_cost_usd == 14
    assert [p.gallons for p in plan.purchases] == [D(1), D(4)]
    assert_plan(plan, stations, roads)


def test_partial_purchase_uses_existing_fuel_and_meets_destination_reserve():
    stations = [GraphStation("a", D("3.47"))]
    roads = matrix(3, {(0, 1): 400, (1, 2): 200})
    plan = optimize_station_graph(stations, roads, reserve_gallons=5)
    assert plan.purchases[0].arrival_fuel_gallons == 10
    assert plan.purchases[0].gallons == 15
    assert plan.purchases[0].departure_fuel_gallons == 25
    assert plan.final_fuel_gallons == 5
    assert plan.total_cost_usd == D("52.05")
    assert_plan(plan, stations, roads)


def test_zero_tank_can_buy_at_a_colocated_station_but_not_a_distant_one():
    stations = [GraphStation("origin-pump", D(3))]
    roads = matrix(3, {(0, 1): 0, (1, 2): 100})
    plan = optimize_station_graph(stations, roads, initial_fuel_gallons=0)
    assert plan.total_cost_usd == 30
    assert plan.purchases[0].gallons == 10
    assert_plan(plan, stations, roads)
    roads[0][1] = D("0.0000001")
    with pytest.raises(InfeasibleGraphPlan):
        optimize_station_graph(stations, roads, initial_fuel_gallons=0)


def test_exact_500_miles_and_a_distance_one_micro_mile_beyond_range():
    roads = matrix(2, {(0, 1): 500})
    plan = optimize_station_graph([], roads)
    assert plan.total_cost_usd == 0
    assert plan.final_fuel_gallons == 0
    assert_plan(plan, [], roads)
    roads[0][1] = D("500.000001")
    with pytest.raises(InfeasibleGraphPlan):
        optimize_station_graph([], roads)


def test_decimal_critical_state_preserves_a_tiny_positive_purchase():
    stations = [GraphStation("tiny-top-up", D("3.0000001"))]
    roads = matrix(3, {(0, 1): "250.0000001", (1, 2): "250.0000009"})
    plan = optimize_station_graph(stations, roads)
    assert plan.fuel_purchased_gallons == D("0.0000001")
    assert plan.total_cost_usd == D("0.00000030000001")
    assert_plan(plan, stations, roads)


def test_directed_disconnections_are_not_mistaken_for_reverse_roads():
    stations = [GraphStation("a", D(3))]
    roads = matrix(3, {(1, 0): 100, (1, 2): 100})
    with pytest.raises(InfeasibleGraphPlan):
        optimize_station_graph(stations, roads)


def test_equal_purchase_cost_prefers_distance_then_fewer_original_road_legs():
    stations = [GraphStation("a", D(3)), GraphStation("b", D(3))]
    roads = matrix(4, {(0, 1): 100, (1, 3): 100, (0, 2): 150, (2, 3): 150, (0, 3): 200})
    plan = optimize_station_graph(stations, roads)
    assert plan.node_indices == (0, 3)
    assert plan.total_distance_miles == 200
    assert_plan(plan, stations, roads)


def test_zero_distance_cycles_and_duplicate_locations_terminate_deterministically():
    stations = [GraphStation("expensive", D(4)), GraphStation("cheap", D(3))]
    roads = matrix(4, {(0, 1): 0, (1, 2): 0, (2, 1): 0, (2, 3): 500})
    plan = optimize_station_graph(stations, roads, initial_fuel_gallons=0)
    assert plan.node_indices == (0, 1, 2, 3)
    assert plan.total_cost_usd == 150
    assert_plan(plan, stations, roads)


def test_finish_can_be_a_transit_waypoint_before_returning_with_requested_reserve():
    stations = [GraphStation("near-destination", D(3))]
    roads = matrix(3, {(0, 2): 100, (2, 1): 10, (1, 2): 10})
    plan = optimize_station_graph(stations, roads, initial_fuel_gallons=12, reserve_gallons=5)
    assert plan.node_indices == (0, 2, 1, 2)
    assert plan.total_cost_usd == 15
    assert plan.final_fuel_gallons == 5
    assert_plan(plan, stations, roads)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"initial_fuel_gallons": -1},
        {"initial_fuel_gallons": 51},
        {"initial_fuel_gallons": True},
        {"reserve_gallons": 51},
        {"reserve_gallons": "NaN"},
        {"tank_capacity_gallons": 0},
        {"mpg": 0},
        {"mpg": "Infinity"},
    ],
)
def test_invalid_vehicle_quantities_fail_before_search(kwargs):
    with pytest.raises(InvalidGraphInput):
        optimize_station_graph([], matrix(2, {(0, 1): 1}), **kwargs)


@pytest.mark.parametrize("bad", [None, True, -1, "NaN", "Infinity", {}, []])
def test_invalid_matrix_diagonal(bad):
    roads = matrix(2, {(0, 1): 1})
    roads[0][0] = bad
    with pytest.raises(InvalidGraphInput):
        optimize_station_graph([], roads)


@pytest.mark.parametrize("bad", [True, -1, "NaN", "Infinity", {}, []])
def test_invalid_edge_values(bad):
    roads = matrix(2, {(0, 1): 1})
    roads[0][1] = bad
    with pytest.raises(InvalidGraphInput):
        optimize_station_graph([], roads)


@pytest.mark.parametrize("roads", [[], [[0]], [[0, 1], [0]], "bad"])
def test_invalid_matrix_dimensions(roads):
    with pytest.raises(InvalidGraphInput):
        optimize_station_graph([], roads)


@pytest.mark.parametrize(
    "stations",
    [
        [GraphStation("", D(3))],
        [GraphStation("a", D(0))],
        [GraphStation("a", D(-1))],
        [GraphStation("a", D("NaN"))],
        [GraphStation("a", True)],
        [GraphStation("a", D(3)), GraphStation("a", D(4))],
        ["not-a-station"],
        "not-a-sequence-of-stations",
    ],
)
def test_invalid_station_definitions(stations):
    with pytest.raises(InvalidGraphInput):
        optimize_station_graph(stations, [])


def test_matrix_bound_is_explicit():
    stations = [GraphStation(str(i), D(1)) for i in range(99)]
    with pytest.raises(InvalidGraphInput):
        optimize_station_graph(stations, [])


@pytest.mark.parametrize("distance_scale", [1, 10])
@pytest.mark.parametrize("seed", range(800))
def test_matches_independent_integer_fuel_oracle_on_directed_nonmetric_graphs(seed, distance_scale):
    rng = random.Random(seed)
    size = rng.randint(2, 8)
    capacity = rng.randint(1, 8)
    initial = rng.randint(0, capacity)
    reserve = rng.randint(0, capacity)
    roads = [
        [
            0 if i == j else (rng.randint(0, 12) if rng.random() < 0.65 else None)
            for j in range(size)
        ]
        for i in range(size)
    ]
    prices = [None] + [Fraction(rng.randint(1, 99), 10) for _ in range(size - 2)] + [None]
    stations = [
        GraphStation(str(i), D(price.numerator) / D(price.denominator))
        for i, price in enumerate(prices[1:-1])
    ]
    reference = integer_fuel_oracle(roads, prices, capacity, initial, reserve)
    scaled_roads = [[None if x is None else D(x) / distance_scale for x in row] for row in roads]
    kwargs = dict(
        initial_fuel_gallons=D(initial) / distance_scale,
        reserve_gallons=D(reserve) / distance_scale,
        tank_capacity_gallons=D(capacity) / distance_scale,
        mpg=1,
    )
    if reference is None:
        with pytest.raises(InfeasibleGraphPlan):
            optimize_station_graph(stations, scaled_roads, **kwargs)
    else:
        plan = optimize_station_graph(stations, scaled_roads, **kwargs)
        actual = (
            Fraction(plan.total_cost_usd) * distance_scale,
            Fraction(plan.total_distance_miles) * distance_scale,
            len(plan.leg_distances_miles),
        )
        assert actual == reference
        assert_plan(plan, stations, scaled_roads)


@pytest.mark.parametrize("seed", range(200))
def test_matches_oracle_on_directed_shortest_distance_metrics(seed):
    rng = random.Random(1000 + seed)
    size = rng.randint(3, 8)
    # Construct metric inputs independently by running ordinary shortest-path
    # searches on a dense directed road graph, not the implementation's closure.
    edges = [[0 if i == j else rng.randint(1, 12) for j in range(size)] for i in range(size)]
    metric = []
    for origin in range(size):
        shortest = [None] * size
        queue = [(0, origin)]
        while queue:
            distance, node = heapq.heappop(queue)
            if shortest[node] is not None:
                continue
            shortest[node] = distance
            for target in range(size):
                if shortest[target] is None:
                    heapq.heappush(queue, (distance + edges[node][target], target))
        metric.append(shortest)
    capacity = rng.randint(2, 8)
    initial = rng.randint(0, capacity)
    reserve = rng.randint(0, capacity)
    prices = [None] + [Fraction(rng.randint(1, 9)) for _ in range(size - 2)] + [None]
    stations = [GraphStation(str(i), D(price.numerator)) for i, price in enumerate(prices[1:-1])]
    expected = integer_fuel_oracle(metric, prices, capacity, initial, reserve)
    kwargs = dict(
        initial_fuel_gallons=initial, reserve_gallons=reserve, tank_capacity_gallons=capacity, mpg=1
    )
    if expected is None:
        with pytest.raises(InfeasibleGraphPlan):
            optimize_station_graph(stations, metric, **kwargs)
    else:
        plan = optimize_station_graph(stations, metric, **kwargs)
        assert (
            Fraction(plan.total_cost_usd),
            int(plan.total_distance_miles),
            len(plan.leg_distances_miles),
        ) == expected
        assert_plan(plan, stations, metric)
