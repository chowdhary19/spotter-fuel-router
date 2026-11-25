"""Metre adapters preserve physical feasibility before formatting public miles."""

from decimal import Decimal, localcontext

import pytest

from fuel.fixed_route_fuel_optimizer import (
    InfeasibleFuelPlan,
    InvalidFuelInput,
    optimize_road_fuel,
)
from fuel.fuel_route_optimizer import (
    GraphStation,
    InfeasibleGraphPlan,
    optimize_road_station_graph,
)

D = Decimal
METERS_PER_MILE = D("1609.344")
METERS_PER_GALLON = D("16093.44")


def chain_matrix(legs):
    size = len(legs) + 1
    matrix = [[D(0) if i == j else None for j in range(size)] for i in range(size)]
    for i, length in enumerate(legs):
        matrix[i][i + 1] = D(str(length))
    return matrix


def test_500_mile_chain_is_exact_even_when_individual_mile_quotients_sum_above_500():
    # These provider metre legs sum to exactly 500 miles. Converting each leg
    # at Python's default Decimal precision before summing gives 500+1e-25.
    legs = [D(408745), D(226895), D(169032)]
    assert sum(legs) == 500 * METERS_PER_MILE
    assert sum((leg / METERS_PER_MILE for leg in legs), D(0)) > 500
    graph = optimize_road_station_graph(
        [GraphStation("a", D(4)), GraphStation("b", D(3))], chain_matrix(legs)
    )
    assert graph.node_indices == (0, 1, 2, 3)
    assert graph.total_distance_miles == 500
    assert graph.mpg == 10
    assert graph.purchases == ()
    assert graph.fuel_consumed_gallons == 50
    assert graph.final_fuel_gallons == 0
    fixed = optimize_road_fuel([("a", D(408745), D(4)), ("b", D(635640), D(3))], D(804672))
    assert fixed.total_distance_miles == 500
    assert fixed.mpg == 10
    assert fixed.purchases == ()
    assert fixed.final_fuel_gallons == 0


def test_destination_colocated_pump_does_not_move_past_finish_due_to_rounding():
    # Previously final_plan summed 2m/1609.344 twice, placing the destination
    # pump 1e-30 miles beyond a total computed as 4m/1609.344.
    with localcontext() as context:
        context.prec = 28
        assert 2 * (D(2) / METERS_PER_MILE) > D(4) / METERS_PER_MILE
    fixed = optimize_road_fuel(
        [("transit", D(2), D(4)), ("finish-pump", D(4), D(3))],
        D(4),
        initial_fuel_gallons=D(1),
        reserve_gallons=D(5),
    )
    assert fixed.final_fuel_gallons == 5
    assert len(fixed.purchases) == 1
    assert fixed.purchases[0].station.identifier == "finish-pump"
    assert fixed.purchases[0].station.distance_miles == fixed.total_distance_miles
    graph = optimize_road_station_graph(
        [GraphStation("transit", D(4)), GraphStation("finish-pump", D(3))],
        chain_matrix([2, 2, 0]),
        initial_fuel_gallons=D(1),
        reserve_gallons=D(5),
    )
    assert graph.final_fuel_gallons == 5
    assert graph.total_distance_miles == fixed.total_distance_miles
    assert abs(graph.total_cost_usd - fixed.total_cost_usd) < D("1e-55")


def test_one_micro_meter_beyond_full_tank_is_not_rounded_into_feasibility():
    distance = D(804672) + D("0.000001")
    with pytest.raises(InfeasibleGraphPlan):
        optimize_road_station_graph([], chain_matrix([distance]))
    with pytest.raises(InfeasibleFuelPlan) as caught:
        optimize_road_fuel([], distance)
    assert caught.value.available_range_miles == 500
    assert caught.value.end_miles > 500


def test_metre_graph_and_fixed_plan_agree_on_partial_purchases_and_reserve():
    graph = optimize_road_station_graph(
        [GraphStation("a", D("3.47"))],
        chain_matrix([D(400) * METERS_PER_MILE, D(200) * METERS_PER_MILE]),
        reserve_gallons=D(5),
    )
    fixed = optimize_road_fuel(
        [("a", D(400) * METERS_PER_MILE, D("3.47"))],
        D(600) * METERS_PER_MILE,
        reserve_gallons=D(5),
    )
    for result in (graph, fixed):
        assert result.total_distance_miles == 600
        assert result.fuel_purchased_gallons == 15
        assert result.total_cost_usd == D("52.05")
        assert result.final_fuel_gallons == 5
        assert result.purchases[0].arrival_fuel_gallons == 10
        assert result.purchases[0].departure_fuel_gallons == 25
        assert result.mpg == 10


def test_zero_distance_refill_can_meet_a_reserve_without_driving():
    fixed = optimize_road_fuel(
        [("here", D(0), D(3))], D(0), initial_fuel_gallons=D(0), reserve_gallons=D(50)
    )
    graph = optimize_road_station_graph(
        [GraphStation("here", D(3))],
        chain_matrix([0, 0]),
        initial_fuel_gallons=D(0),
        reserve_gallons=D(50),
    )
    for result in (fixed, graph):
        assert result.total_distance_miles == 0
        assert result.fuel_consumed_gallons == 0
        assert result.fuel_purchased_gallons == 50
        assert result.total_cost_usd == 150
        assert result.final_fuel_gallons == 50


@pytest.mark.parametrize("bad", [("a",), ("a", 1), ("a", 1, 2, 3), "abc"])
def test_fixed_road_station_tuple_shape_is_validated(bad):
    with pytest.raises(InvalidFuelInput):
        optimize_road_fuel([bad], D(100))
