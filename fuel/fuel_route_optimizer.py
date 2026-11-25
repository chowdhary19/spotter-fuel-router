"""Exact continuous-fuel optimization over supplied directed road segments.

The table is a graph, not an assumed distance metric: OSRM reports lengths of
fastest routes. A shortest-distance closure first permits cheaper concatenations
through transit waypoints. Every such waypoint is retained in the returned path.

The refueling exchange rule follows Khuller, Malekian and Mestre, "To Fill or
not to Fill: The Gas Station Problem", Section 2 / Lemma 2.1:
https://www.cs.umd.edu/users/samir/grant/gas-j.pdf

Between positive-purchase stops, fill the tank before a pricier stop, or buy
just enough to reach a no-pricier stop. Thus arrival states are finite despite
continuous purchases. We search these states with Dijkstra, using exact integer
scales derived from the input decimals, not discretized fuel buckets.

Optimality covers supplied directed segments and their concatenations only.
Candidate selection, missing stations and roads absent from the provider table
remain outside this model. Initial fuel is inventory, not a paid purchase.
"""

from __future__ import annotations

import heapq
import math
from collections.abc import Sequence
from dataclasses import dataclass, replace
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction


class InvalidGraphInput(ValueError):
    """The station list, matrix or vehicle quantities are invalid."""


class InfeasibleGraphPlan(ValueError):
    """No route through the supplied graph can satisfy the fuel constraints."""


@dataclass(frozen=True, slots=True)
class GraphStation:
    identifier: str
    price_usd_per_gallon: Decimal


@dataclass(frozen=True, slots=True)
class GraphFuelPurchase:
    # path_index distinguishes multiple visits to the same matrix node.
    path_index: int
    node_index: int
    identifier: str
    gallons: Decimal
    arrival_fuel_gallons: Decimal
    departure_fuel_gallons: Decimal
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class GraphFuelPlan:
    # Matrix order: origin 0, stations 1..n, destination n+1.
    # Includes non-purchasing transit nodes required by distance closure.
    node_indices: tuple[int, ...]
    leg_distances_miles: tuple[Decimal, ...]
    purchases: tuple[GraphFuelPurchase, ...]
    total_distance_miles: Decimal
    tank_capacity_gallons: Decimal
    mpg: Decimal
    initial_fuel_gallons: Decimal
    reserve_gallons: Decimal
    final_fuel_gallons: Decimal
    fuel_consumed_gallons: Decimal
    fuel_purchased_gallons: Decimal
    total_cost_usd: Decimal


def _decimal(value, name):
    if isinstance(value, bool):
        raise InvalidGraphInput(f"{name} must be a finite number")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidGraphInput(f"{name} must be a finite number") from exc
    if not result.is_finite():
        raise InvalidGraphInput(f"{name} must be a finite number")
    return result


def _as_decimal(value: Fraction) -> Decimal:
    # Required 10-mpg inputs yield terminating decimals. For another mpg with
    # a recurring quotient, only returned presentation quantities are rounded;
    # the optimizer and every feasibility/cost comparison remain exact.
    with localcontext() as context:
        context.prec = max(60, len(str(abs(value.numerator))) + len(str(value.denominator)) + 5)
        return Decimal(value.numerator) / Decimal(value.denominator)


def _scaled(value: Fraction, scale: int) -> int:
    return value.numerator * (scale // value.denominator)


def _closure(matrix):
    """Directed Floyd-Warshall, preferring fewer original edges on equal distance."""
    size = len(matrix)
    distances = [row[:] for row in matrix]
    hops = [
        [None if x is None else int(i != j) for j, x in enumerate(row)]
        for i, row in enumerate(matrix)
    ]
    successor = [[None if x is None else j for j, x in enumerate(row)] for row in matrix]
    for middle in range(size):
        for origin in range(size):
            first = distances[origin][middle]
            if first is None:
                continue
            for finish in range(size):
                last = distances[middle][finish]
                if last is None:
                    continue
                candidate = first + last
                edge_count = hops[origin][middle] + hops[middle][finish]
                old = distances[origin][finish]
                if old is None or (candidate, edge_count) < (old, hops[origin][finish]):
                    distances[origin][finish] = candidate
                    hops[origin][finish] = edge_count
                    successor[origin][finish] = successor[origin][middle]
    return distances, hops, successor


def _path(successor, origin, finish):
    path = [origin]
    while origin != finish:
        origin = successor[origin][finish]
        if origin is None or len(path) > len(successor):
            raise AssertionError("Invalid shortest-distance path reconstruction")
        path.append(origin)
    return tuple(path)


def optimize_station_graph(
    stations: Sequence[GraphStation],
    distances_miles: Sequence[Sequence[Decimal | None]],
    *,
    initial_fuel_gallons=Decimal("50"),
    reserve_gallons=Decimal("0"),
    tank_capacity_gallons=Decimal("50"),
    mpg=Decimal("10"),
) -> GraphFuelPlan:
    """Minimize new fuel purchases, then road distance, then number of road legs.

    Matrix node 0 is the origin, nodes 1..n correspond to stations in list order,
    and node n+1 is the destination. Endpoints do not sell fuel; represent an
    endpoint pump as a separate station, with its actual road distance (possibly
    zero). None means an unavailable directed segment. Diagonal entries are 0.

    At most 100 total nodes are accepted to bound the matrix and state search.
    Closure costs O(n^3); the fuel graph has O(n^2) states and O(n^3) arcs, with
    O(n^3 log n) worst-case Dijkstra time. No number of refills is imposed.
    All transit nodes must be passed to the final route provider, including
    those absent from purchases.
    """
    if not isinstance(stations, Sequence) or isinstance(stations, (str, bytes)):
        raise InvalidGraphInput("stations must be a sequence")
    size = len(stations) + 2
    if size > 100:
        raise InvalidGraphInput("At most 98 stations are supported")
    prices = [None]
    identifiers = set()
    normalized = []
    for station in stations:
        if not isinstance(station, GraphStation):
            raise InvalidGraphInput("stations must contain GraphStation objects")
        identifier = station.identifier
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise InvalidGraphInput("Station identifiers must be nonempty and unique")
        price = _decimal(station.price_usd_per_gallon, "station price")
        if price <= 0:
            raise InvalidGraphInput("Station prices must be positive")
        normalized.append(GraphStation(identifier, price))
        identifiers.add(identifier)
        prices.append(Fraction(price))
    prices.append(None)

    capacity = _decimal(tank_capacity_gallons, "tank capacity")
    efficiency = _decimal(mpg, "mpg")
    initial = _decimal(initial_fuel_gallons, "initial fuel")
    reserve = _decimal(reserve_gallons, "reserve")
    if capacity <= 0 or efficiency <= 0:
        raise InvalidGraphInput("Tank capacity and mpg must be positive")
    if not 0 <= initial <= capacity or not 0 <= reserve <= capacity:
        raise InvalidGraphInput("Initial fuel and reserve must lie between zero and capacity")

    if (
        not isinstance(distances_miles, Sequence)
        or isinstance(distances_miles, (str, bytes))
        or len(distances_miles) != size
    ):
        raise InvalidGraphInput("Distance matrix dimensions must match [start, stations, finish]")
    matrix = []
    for i, row in enumerate(distances_miles):
        if not isinstance(row, Sequence) or isinstance(row, (str, bytes)) or len(row) != size:
            raise InvalidGraphInput("Distance matrix must be square")
        converted = []
        for j, value in enumerate(row):
            if value is None:
                if i == j:
                    raise InvalidGraphInput("Distance matrix diagonal must be zero")
                converted.append(None)
                continue
            distance = _decimal(value, "road distance")
            if distance < 0 or (i == j and distance != 0):
                raise InvalidGraphInput("Road distances must be nonnegative; diagonal must be zero")
            converted.append(Fraction(distance))
        matrix.append(converted)

    efficiency_fraction = Fraction(efficiency)
    ranges = [
        Fraction(capacity) * efficiency_fraction,
        Fraction(initial) * efficiency_fraction,
        Fraction(reserve) * efficiency_fraction,
    ]
    # Common exact units represent all finite decimal inputs. This is an
    # arithmetic normalization, not a chosen-resolution approximation.
    distance_scale = math.lcm(
        *(value.denominator for row in matrix for value in row if value is not None),
        *(value.denominator for value in ranges),
    )
    price_scale = math.lcm(*(value.denominator for value in prices if value is not None))
    original = [[None if x is None else _scaled(x, distance_scale) for x in row] for row in matrix]
    tank, initial_range, reserve_range = (_scaled(x, distance_scale) for x in ranges)
    price_units = [None if x is None else _scaled(x, price_scale) for x in prices]
    distances, hops, successor = _closure(original)

    # A state records range remaining on arrival at a refueling node. By the
    # exchange rule, arrivals after a positive purchase are 0 or tank-d(u,v).
    # Origin-reachable stations additionally inherit initial_range-d(start,v).
    origin_state = (0, initial_range)
    scores = {origin_state: (0, 0, 0)}
    parents = {}
    queue = [(0, 0, 0, *origin_state)]
    destination = size - 1
    goal_state = None

    def relax(previous, target, fuel, bought, score):
        node = previous[0]
        distance = distances[node][target]
        extra_cost = 0 if node == 0 else bought * price_units[node]
        candidate = (
            score[0] + extra_cost,
            score[1] + distance,
            score[2] + hops[node][target],
        )
        state = (target, fuel)
        if state not in scores or candidate < scores[state]:
            scores[state] = candidate
            parents[state] = (previous, bought)
            heapq.heappush(queue, (*candidate, *state))

    while queue:
        cost, traveled, legs, node, fuel = heapq.heappop(queue)
        state = (node, fuel)
        score = (cost, traveled, legs)
        if scores.get(state) != score:
            continue
        if node == destination:
            goal_state = state
            break

        to_finish = distances[node][destination]
        if to_finish is not None and to_finish + reserve_range <= tank:
            needed = max(0, to_finish + reserve_range - fuel)
            if node != 0 or needed == 0:
                relax(state, destination, fuel + needed - to_finish, needed, score)

        for target in range(1, destination):
            if target == node:
                continue
            distance = distances[node][target]
            if distance is None or distance > tank:
                continue
            if node == 0:
                if distance <= fuel:
                    relax(state, target, fuel - distance, 0, score)
                continue
            target_range = tank if price_units[target] > price_units[node] else distance
            bought = target_range - fuel
            if bought <= 0:
                # No positive refill here: shortest-distance closure already
                # permits the preceding refill to bypass this decision node,
                # retaining it as a transit waypoint if needed.
                continue
            relax(state, target, target_range - distance, bought, score)

    if goal_state is None:
        raise InfeasibleGraphPlan("No feasible fuel plan exists in the supplied directed graph")

    arcs = []
    current = goal_state
    while current != origin_state:
        previous, bought = parents[current]
        arcs.append((previous, current, bought))
        current = previous
    arcs.reverse()

    indices = [0]
    road_legs = []
    purchases = []
    fuel = initial_range
    total_bought = 0
    total_cost = 0

    def gallons(range_units):
        return _as_decimal(Fraction(range_units, distance_scale) / efficiency_fraction)

    def dollars(cost_units):
        return _as_decimal(Fraction(cost_units, distance_scale * price_scale) / efficiency_fraction)

    for previous, current, bought in arcs:
        node = previous[0]
        assert fuel == previous[1]
        if bought:
            charge = bought * price_units[node]
            purchases.append(
                GraphFuelPurchase(
                    path_index=len(indices) - 1,
                    node_index=node,
                    identifier=normalized[node - 1].identifier,
                    gallons=gallons(bought),
                    arrival_fuel_gallons=gallons(fuel),
                    departure_fuel_gallons=gallons(fuel + bought),
                    cost_usd=dollars(charge),
                )
            )
            total_bought += bought
            total_cost += charge
            fuel += bought
        for target in _path(successor, node, current[0])[1:]:
            distance = original[indices[-1]][target]
            assert distance is not None and 0 <= distance <= fuel
            road_legs.append(_as_decimal(Fraction(distance, distance_scale)))
            fuel -= distance
            indices.append(target)
        assert fuel == current[1]

    total_distance = scores[goal_state][1]
    assert total_cost == scores[goal_state][0]
    assert initial_range + total_bought - total_distance == fuel >= reserve_range
    return GraphFuelPlan(
        node_indices=tuple(indices),
        leg_distances_miles=tuple(road_legs),
        purchases=tuple(purchases),
        total_distance_miles=_as_decimal(Fraction(total_distance, distance_scale)),
        tank_capacity_gallons=capacity,
        mpg=efficiency,
        initial_fuel_gallons=initial,
        reserve_gallons=reserve,
        final_fuel_gallons=gallons(fuel),
        fuel_consumed_gallons=gallons(total_distance),
        fuel_purchased_gallons=gallons(total_bought),
        total_cost_usd=dollars(total_cost),
    )


def optimize_road_station_graph(
    stations: Sequence[GraphStation],
    distances_meters: Sequence[Sequence[Decimal | None]],
    *,
    initial_fuel_gallons=Decimal("50"),
    reserve_gallons=Decimal("0"),
) -> GraphFuelPlan:
    """Optimize provider metre distances without rounding before feasibility.

    One statute mile is exactly 1609.344 metres. Internally the generic solver
    uses metres as its distance unit and 16093.44 metres/gallon as efficiency.
    Its temporary distance/efficiency field names therefore carry metre units.
    Only after all path and fuel decisions are complete do we restore the public
    mile/gallon contract. Never feed these presentation distances back into a
    range check; use the original provider metre legs for final verification.
    """
    meters_per_mile = Fraction(Decimal("1609.344"))
    raw = optimize_station_graph(
        stations,
        distances_meters,
        initial_fuel_gallons=initial_fuel_gallons,
        reserve_gallons=reserve_gallons,
        mpg=Decimal("16093.44"),
    )
    return replace(
        raw,
        mpg=Decimal("10"),
        total_distance_miles=_as_decimal(Fraction(raw.total_distance_miles) / meters_per_mile),
        leg_distances_miles=tuple(
            _as_decimal(Fraction(distance) / meters_per_mile)
            for distance in raw.leg_distances_miles
        ),
    )
