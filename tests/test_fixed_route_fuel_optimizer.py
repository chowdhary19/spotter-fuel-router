"""Behavioral tests and an independent exhaustive oracle for fuel planning."""

from __future__ import annotations

import random
import unittest
from decimal import Decimal, localcontext
from fractions import Fraction

from fuel.fixed_route_fuel_optimizer import (
    FuelPlan,
    FuelStation,
    InfeasibleFuelPlan,
    InvalidFuelInput,
    optimize_fuel,
)

D = Decimal


def station(identifier: str, position: object, price: object) -> FuelStation:
    return FuelStation(identifier, D(str(position)), D(str(price)))


def exhaustive_cost(
    stations: list[FuelStation],
    destination: int,
    capacity: int,
    initial: int,
    reserve: int,
) -> Fraction | None:
    """Enumerate every feasible integer purchase, independent of greedy logic.

    Distances and tank sizes are small integers at 1 mpg. Every optimal fuel
    breakpoint is integral for these instances, so this is an exact oracle.
    Prices use Fraction to avoid sharing the solver's numeric implementation.
    """
    states = {initial: Fraction(0)}
    previous = 0
    for stop in sorted(stations, key=lambda item: item.distance_miles):
        position = int(stop.distance_miles)
        demand = position - previous
        after_purchases: dict[int, Fraction] = {}
        for carried, paid in states.items():
            remaining = carried - demand
            if remaining < 0:
                continue
            for quantity in range(capacity - remaining + 1):
                new_fuel = remaining + quantity
                new_cost = paid + quantity * Fraction(stop.price_usd_per_gallon)
                if new_fuel not in after_purchases or new_cost < after_purchases[new_fuel]:
                    after_purchases[new_fuel] = new_cost
        states = after_purchases
        previous = position
    costs = [paid for fuel, paid in states.items() if fuel - (destination - previous) >= reserve]
    return min(costs) if costs else None


class FuelOptimizerTests(unittest.TestCase):
    def assert_plan_conserves_fuel(self, plan: FuelPlan) -> None:
        with localcontext() as context:
            context.prec = 60
            fuel = plan.initial_fuel_gallons
            position = D(0)
            paid = D(0)
            bought = D(0)
            for purchase in plan.purchases:
                self.assertGreaterEqual(purchase.station.distance_miles, position)
                fuel -= (purchase.station.distance_miles - position) / plan.mpg
                self.assertGreaterEqual(fuel, 0)
                self.assertEqual(fuel, purchase.arrival_fuel_gallons)
                self.assertGreater(purchase.gallons, 0)
                fuel += purchase.gallons
                self.assertLessEqual(fuel, plan.tank_capacity_gallons)
                self.assertEqual(fuel, purchase.departure_fuel_gallons)
                self.assertEqual(
                    purchase.cost_usd,
                    purchase.gallons * purchase.station.price_usd_per_gallon,
                )
                paid += purchase.cost_usd
                bought += purchase.gallons
                position = purchase.station.distance_miles
            fuel -= (plan.total_distance_miles - position) / plan.mpg
            self.assertGreaterEqual(fuel, plan.reserve_gallons)
            self.assertEqual(fuel, plan.final_fuel_gallons)
            self.assertEqual(paid, plan.total_cost_usd)
            self.assertEqual(bought, plan.fuel_purchased_gallons)
            self.assertEqual(
                plan.initial_fuel_gallons + bought - plan.fuel_consumed_gallons,
                plan.final_fuel_gallons,
            )

    def test_short_trip_uses_existing_fuel_without_inventing_acquisition_cost(self):
        plan = optimize_fuel([], D("100"))
        self.assertEqual(plan.purchases, ())
        self.assertEqual(plan.total_cost_usd, 0)
        self.assertEqual(plan.fuel_consumed_gallons, 10)
        self.assertEqual(plan.final_fuel_gallons, 40)
        self.assert_plan_conserves_fuel(plan)

    def test_exact_500_mile_boundary_is_feasible_without_a_stop(self):
        plan = optimize_fuel([], D("500"))
        self.assertEqual(plan.final_fuel_gallons, 0)
        self.assert_plan_conserves_fuel(plan)

    def test_tiny_distance_above_range_is_not_rounded_into_feasibility(self):
        with self.assertRaises(InfeasibleFuelPlan) as caught:
            optimize_fuel([], D("500.000000000001"))
        self.assertEqual(caught.exception.available_range_miles, D("500"))
        self.assertEqual(caught.exception.end_miles, D("500.000000000001"))

    def test_tiny_distance_below_range_preserves_remaining_fuel(self):
        plan = optimize_fuel([], D("499.999999999999"))
        self.assertEqual(plan.final_fuel_gallons, D("0.0000000000001"))

    def test_partial_purchases_at_first_cheaper_station_not_cheapest_in_bucket(self):
        plan = optimize_fuel(
            [station("a", 0, 5), station("b", 100, 4), station("c", 400, 1)],
            D("500"),
            initial_fuel_gallons=D(0),
        )
        self.assertEqual([p.gallons for p in plan.purchases], [10, 30, 10])
        self.assertEqual(plan.total_cost_usd, 180)
        self.assert_plan_conserves_fuel(plan)

    def test_initial_fuel_is_carried_through_expensive_stations(self):
        plan = optimize_fuel(
            [station("a", 100, 5), station("b", 400, 4), station("c", 700, 3)],
            D("1000"),
        )
        self.assertEqual([p.station.identifier for p in plan.purchases], ["b", "c"])
        self.assertEqual([p.gallons for p in plan.purchases], [20, 30])
        self.assertEqual(plan.total_cost_usd, 170)
        self.assert_plan_conserves_fuel(plan)

    def test_increasing_prices_buy_earlier_but_never_beyond_destination(self):
        plan = optimize_fuel(
            [station("a", 0, 3), station("b", 400, 4), station("c", 800, 5)],
            D("1000"),
            initial_fuel_gallons=D(0),
        )
        self.assertEqual([p.gallons for p in plan.purchases], [50, 40, 10])
        self.assertEqual(plan.total_cost_usd, 360)
        self.assertEqual(plan.final_fuel_gallons, 0)
        self.assert_plan_conserves_fuel(plan)

    def test_cheaper_station_at_exact_range_boundary_is_reachable(self):
        plan = optimize_fuel(
            [station("a", 0, 4), station("b", 500, 3)],
            D("700"),
            initial_fuel_gallons=D(0),
        )
        self.assertEqual([p.gallons for p in plan.purchases], [50, 20])
        self.assertEqual(plan.total_cost_usd, 260)
        self.assert_plan_conserves_fuel(plan)

    def test_colocated_price_and_identifier_ties_are_deterministic(self):
        candidates = [station("z", 0, 2), station("a", 0, 2), station("x", 0, 3)]
        expected = optimize_fuel(candidates, D(100), initial_fuel_gallons=D(0))
        reversed_plan = optimize_fuel(reversed(candidates), D(100), initial_fuel_gallons=D(0))
        self.assertEqual(expected, reversed_plan)
        self.assertEqual(expected.purchases[0].station.identifier, "a")
        self.assertEqual(expected.total_cost_usd, 20)

    def test_equal_prices_do_not_create_zero_quantity_stops(self):
        plan = optimize_fuel(
            [station("a", 0, 3), station("b", 100, 3), station("c", 300, 3)],
            D(400),
            initial_fuel_gallons=D(0),
        )
        self.assertEqual([p.station.identifier for p in plan.purchases], ["a"])
        self.assertEqual(plan.total_cost_usd, 120)

    def test_reserve_is_inventory_not_extra_reported_travel(self):
        plan = optimize_fuel(
            [station("a", 0, 4), station("b", 400, 3)],
            D(600),
            initial_fuel_gallons=D(0),
            reserve_gallons=D(5),
        )
        self.assertEqual([p.gallons for p in plan.purchases], [40, 25])
        self.assertEqual(plan.fuel_consumed_gallons, 60)
        self.assertEqual(plan.final_fuel_gallons, 5)
        self.assert_plan_conserves_fuel(plan)

    def test_reserve_can_make_an_otherwise_feasible_route_infeasible(self):
        with self.assertRaises(InfeasibleFuelPlan) as caught:
            optimize_fuel([station("a", 0, 3)], D(500), reserve_gallons=D("0.01"))
        self.assertEqual(caught.exception.reserve_gallons, D("0.01"))

    def test_zero_distance_route_and_station_at_destination(self):
        plan = optimize_fuel(
            [station("a", 0, "3.00733333")],
            D(0),
            initial_fuel_gallons=D(0),
            reserve_gallons=D(2),
        )
        self.assertEqual(plan.total_cost_usd, D("6.01466666"))
        self.assertEqual(plan.final_fuel_gallons, 2)
        self.assert_plan_conserves_fuel(plan)

    def test_source_price_precision_is_not_rounded_before_optimization(self):
        plan = optimize_fuel(
            [station("a", 0, "3.00733333"), station("b", 100, "3.00733332")],
            D(200),
            initial_fuel_gallons=D(0),
        )
        self.assertEqual([p.gallons for p in plan.purchases], [10, 10])
        self.assertEqual(plan.total_cost_usd, D("60.14666650"))
        self.assert_plan_conserves_fuel(plan)

    def test_unordered_inputs_are_sorted_by_route_position(self):
        candidates = [station("a", 0, 4), station("b", 200, 3), station("c", 400, 2)]
        self.assertEqual(
            optimize_fuel(candidates, D(600)),
            optimize_fuel(reversed(candidates), D(600)),
        )

    def test_first_station_unreachable_has_typed_gap_context(self):
        with self.assertRaises(InfeasibleFuelPlan) as caught:
            optimize_fuel([station("a", 100, 3)], D(200), initial_fuel_gallons=D(5))
        self.assertEqual(caught.exception.start_miles, 0)
        self.assertEqual(caught.exception.end_miles, 100)
        self.assertEqual(caught.exception.available_range_miles, 50)

    def test_invalid_vehicle_and_route_numbers(self):
        cases = [
            {"total_distance_miles": D(-1)},
            {"total_distance_miles": D("NaN")},
            {"total_distance_miles": D("Infinity")},
            {"total_distance_miles": True},
            {"initial_fuel_gallons": D(-1)},
            {"initial_fuel_gallons": D(51)},
            {"initial_fuel_gallons": D("NaN")},
            {"reserve_gallons": D(-1)},
            {"reserve_gallons": D(51)},
            {"tank_capacity_gallons": D(0)},
            {"mpg": D(0)},
            {"mpg": D("Infinity")},
        ]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(InvalidFuelInput):
                optimize_fuel([], **({"total_distance_miles": D(100)} | case))

    def test_invalid_station_inputs(self):
        candidates = [
            station("a", -1, 3),
            station("a", 101, 3),
            station("a", 1, 0),
            station("a", 1, -1),
            station("a", "NaN", 3),
            station("a", 1, "NaN"),
            station("", 1, 3),
            station(" ", 1, 3),
            {"identifier": "a", "distance_miles": 0, "price_usd_per_gallon": 3},
        ]
        for candidate in candidates:
            with self.subTest(candidate=candidate), self.assertRaises(InvalidFuelInput):
                optimize_fuel([candidate], D(100))

    def test_greedy_matches_exhaustive_oracle_on_600_seeded_instances(self):
        rng = random.Random(20261125)
        feasible = 0
        infeasible = 0
        for case in range(600):
            capacity = rng.randint(2, 8)
            destination = rng.randint(0, 25)
            initial = rng.randint(0, capacity)
            reserve = rng.randint(0, capacity)
            candidates = [
                station(str(index), rng.randint(0, destination), D(rng.randint(1, 500)) / 100)
                for index in range(rng.randint(0, 10))
            ]
            oracle = exhaustive_cost(candidates, destination, capacity, initial, reserve)
            with self.subTest(case=case, capacity=capacity, destination=destination):
                kwargs = {
                    "tank_capacity_gallons": D(capacity),
                    "mpg": D(1),
                    "initial_fuel_gallons": D(initial),
                    "reserve_gallons": D(reserve),
                }
                if oracle is None:
                    infeasible += 1
                    with self.assertRaises(InfeasibleFuelPlan):
                        optimize_fuel(candidates, D(destination), **kwargs)
                else:
                    feasible += 1
                    plan = optimize_fuel(candidates, D(destination), **kwargs)
                    self.assertEqual(Fraction(plan.total_cost_usd), oracle)
                    self.assert_plan_conserves_fuel(plan)
        self.assertGreater(feasible, 100)
        self.assertGreater(infeasible, 100)


if __name__ == "__main__":
    unittest.main()
