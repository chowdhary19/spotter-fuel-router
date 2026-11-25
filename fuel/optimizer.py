"""Minimum-cost fuel purchases on a fixed, ordered route.

Distances must describe the route actually being planned. The solver does not
choose roads, locate stations, or turn a straight-line station offset into a
driving distance. Fuel already in the tank is inventory: its acquisition cost
is not included in the cost of purchases made during this trip.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext

ZERO = Decimal("0")
DEFAULT_TANK_GALLONS = Decimal("50")
DEFAULT_MPG = Decimal("10")


class InvalidFuelInput(ValueError):
    """A route, station, or vehicle input violates the solver's contract."""


class InfeasibleFuelPlan(ValueError):
    """The available priced stations cannot support this fixed route."""

    def __init__(
        self,
        message: str,
        *,
        start_miles: Decimal,
        end_miles: Decimal,
        available_range_miles: Decimal,
        reserve_gallons: Decimal = ZERO,
    ) -> None:
        super().__init__(message)
        self.start_miles = start_miles
        self.end_miles = end_miles
        self.available_range_miles = available_range_miles
        self.reserve_gallons = reserve_gallons


@dataclass(frozen=True, slots=True)
class FuelStation:
    identifier: str
    distance_miles: Decimal
    price_usd_per_gallon: Decimal


@dataclass(frozen=True, slots=True)
class FuelPurchase:
    station: FuelStation
    gallons: Decimal
    arrival_fuel_gallons: Decimal
    departure_fuel_gallons: Decimal
    cost_usd: Decimal


@dataclass(frozen=True, slots=True)
class FuelPlan:
    purchases: tuple[FuelPurchase, ...]
    total_distance_miles: Decimal
    tank_capacity_gallons: Decimal
    mpg: Decimal
    initial_fuel_gallons: Decimal
    reserve_gallons: Decimal
    fuel_consumed_gallons: Decimal
    fuel_purchased_gallons: Decimal
    final_fuel_gallons: Decimal
    total_cost_usd: Decimal


def _decimal(value: object, name: str) -> Decimal:
    # str(float) avoids importing binary floating-point artifacts into Decimal.
    # Booleans are not quantities even though Python treats them as integers.
    if isinstance(value, bool):
        raise InvalidFuelInput(f"{name} must be a finite number")
    try:
        number = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise InvalidFuelInput(f"{name} must be a finite number") from exc
    if not number.is_finite():
        raise InvalidFuelInput(f"{name} must be a finite number")
    return number


def _stations_on_route(
    stations: Iterable[FuelStation], total_distance: Decimal
) -> list[FuelStation]:
    by_position: dict[Decimal, FuelStation] = {}
    for station in stations:
        if not isinstance(station, FuelStation):
            raise InvalidFuelInput("stations must contain FuelStation objects")
        if not isinstance(station.identifier, str) or not station.identifier.strip():
            raise InvalidFuelInput("station identifier must be a nonempty string")
        distance = _decimal(station.distance_miles, "station distance_miles")
        price = _decimal(station.price_usd_per_gallon, "station price_usd_per_gallon")
        if not ZERO <= distance <= total_distance:
            raise InvalidFuelInput("station distance must lie between origin and destination")
        if price <= ZERO:
            raise InvalidFuelInput("station price must be positive")
        normalized = FuelStation(station.identifier, distance, price)
        previous = by_position.get(distance)
        # Colocated stations have identical reachability. Retain the cheapest;
        # equal quotes use a stable ID tie-break, independent of input order.
        if previous is None or (price, station.identifier) < (
            previous.price_usd_per_gallon,
            previous.identifier,
        ):
            by_position[distance] = normalized
    return sorted(by_position.values(), key=lambda station: station.distance_miles)


def optimize_fuel(
    stations: Iterable[FuelStation],
    total_distance_miles: Decimal,
    *,
    initial_fuel_gallons: Decimal = DEFAULT_TANK_GALLONS,
    tank_capacity_gallons: Decimal = DEFAULT_TANK_GALLONS,
    mpg: Decimal = DEFAULT_MPG,
    reserve_gallons: Decimal = ZERO,
) -> FuelPlan:
    """Return exact-cost purchases for a fixed route, or raise a typed error.

    Inputs may be unordered; positions are cumulative miles from the origin.
    At each station, buy enough to reach the first cheaper reachable station.
    If none exists, fill the tank, capped at the distance to the destination
    plus the requested reserve. A monotonic stack finds the next cheaper
    station in linear time. Overall complexity is O(n log n) including sorting.

    Amounts and prices remain Decimal values without presentation rounding.
    Output costs exclude the unknown acquisition cost of initial fuel. The
    caller may round costs for display, but must not round fuel before solving.
    """
    distance = _decimal(total_distance_miles, "total_distance_miles")
    capacity = _decimal(tank_capacity_gallons, "tank_capacity_gallons")
    efficiency = _decimal(mpg, "mpg")
    initial = _decimal(initial_fuel_gallons, "initial_fuel_gallons")
    reserve = _decimal(reserve_gallons, "reserve_gallons")
    if distance < ZERO:
        raise InvalidFuelInput("total_distance_miles must be nonnegative")
    if capacity <= ZERO or efficiency <= ZERO:
        raise InvalidFuelInput("tank_capacity_gallons and mpg must be positive")
    if not ZERO <= initial <= capacity:
        raise InvalidFuelInput("initial fuel must be between zero and tank capacity")
    if not ZERO <= reserve <= capacity:
        raise InvalidFuelInput("reserve must be between zero and tank capacity")
    ordered = _stations_on_route(stations, distance)

    # Range miles avoid repeated divide/multiply operations in feasibility
    # decisions. With the required 10 mpg, all finite-decimal inputs stay exact.
    with localcontext() as context:
        context.prec = 60
        tank_range = capacity * efficiency
        fuel_range = initial * efficiency
        terminal = distance + reserve * efficiency
        positions = [station.distance_miles for station in ordered] + [terminal]
        prices = [station.price_usd_per_gallon for station in ordered] + [ZERO]
        next_cheaper: list[int | None] = [None] * len(prices)
        stack: list[int] = []
        for index in range(len(prices) - 1, -1, -1):
            while stack and prices[stack[-1]] >= prices[index]:
                stack.pop()
            next_cheaper[index] = stack[-1] if stack else None
            stack.append(index)

        purchases: list[FuelPurchase] = []
        previous_position = ZERO
        purchased = ZERO
        cost = ZERO
        for index, station in enumerate(ordered):
            leg_distance = station.distance_miles - previous_position
            if leg_distance > fuel_range:
                raise InfeasibleFuelPlan(
                    "Insufficient fuel to reach the next available priced station",
                    start_miles=previous_position,
                    end_miles=station.distance_miles,
                    available_range_miles=fuel_range,
                )
            fuel_range -= leg_distance
            arrival_fuel = fuel_range / efficiency
            cheaper_index = next_cheaper[index]
            if (
                cheaper_index is not None
                and positions[cheaper_index] - station.distance_miles <= tank_range
            ):
                target_range = positions[cheaper_index] - station.distance_miles
            else:
                target_range = min(tank_range, terminal - station.distance_miles)

            purchase_range = max(ZERO, target_range - fuel_range)
            if purchase_range > ZERO:
                gallons = purchase_range / efficiency
                line_cost = gallons * station.price_usd_per_gallon
                fuel_range += purchase_range
                purchased += gallons
                cost += line_cost
                purchases.append(
                    FuelPurchase(
                        station=station,
                        gallons=gallons,
                        arrival_fuel_gallons=arrival_fuel,
                        departure_fuel_gallons=fuel_range / efficiency,
                        cost_usd=line_cost,
                    )
                )
            previous_position = station.distance_miles

        final_leg = distance - previous_position
        if final_leg > fuel_range:
            raise InfeasibleFuelPlan(
                "Insufficient fuel to reach the destination",
                start_miles=previous_position,
                end_miles=distance,
                available_range_miles=fuel_range,
            )
        fuel_range -= final_leg
        if fuel_range < reserve * efficiency:
            raise InfeasibleFuelPlan(
                "The destination reserve cannot be met with the available stations",
                start_miles=previous_position,
                end_miles=distance,
                available_range_miles=fuel_range + final_leg,
                reserve_gallons=reserve,
            )
        return FuelPlan(
            purchases=tuple(purchases),
            total_distance_miles=distance,
            tank_capacity_gallons=capacity,
            mpg=efficiency,
            initial_fuel_gallons=initial,
            reserve_gallons=reserve,
            fuel_consumed_gallons=distance / efficiency,
            fuel_purchased_gallons=purchased,
            final_fuel_gallons=fuel_range / efficiency,
            total_cost_usd=cost,
        )
