import copy
import hashlib
import json
import threading
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache

from .catalog import resolve_location, station_catalog
from .errors import PlanningError
from .geo import candidates_on_route, meters_between
from .optimizer import FuelStation, InfeasibleFuelPlan, optimize_fuel
from .routing import METERS_PER_MILE, OSRMClient

_planning_lock = threading.Lock()
CAPACITY = Decimal("50")
MPG = Decimal("10")


def number(value, places="0.000001"):
    return str(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def gallons_parameter(data, name, default):
    value = data.get(name, default)
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
            raise ValueError
        result = Decimal(str(value))
        if not result.is_finite() or not 0 <= result <= CAPACITY:
            raise ValueError
        if result.as_tuple().exponent < -6:
            raise ValueError
    except (ValueError, InvalidOperation):
        raise PlanningError(
            "invalid_fuel_quantity", f"{name} must be between 0 and 50 gallons.", status=400
        ) from None
    return result


def station_input(station, miles):
    return FuelStation(str(station["opis_id"]), miles, Decimal(station["price_usd_per_gallon"]))


def points_for(start, finish, selected):
    return [
        (start["longitude"], start["latitude"]),
        *[(s["longitude"], s["latitude"]) for s in selected],
        (finish["longitude"], finish["latitude"]),
    ]


def validate_snaps(route, points):
    # An OSRM waypoint snapped kilometres away could silently ignore a fuel stop.
    for index, (requested, snapped) in enumerate(zip(points, route.snapped_waypoints, strict=True)):
        snap = meters_between(requested, snapped)
        limit = 500 if index not in {0, len(points) - 1} else 5000
        if snap > limit:
            raise PlanningError(
                "waypoint_too_far_from_road",
                "A location could not be snapped closely enough to a road.",
                details={
                    "waypoint_index": index,
                    "snap_distance_meters": round(snap, 1),
                    "maximum_meters": limit,
                },
            )


def final_plan(route, selected, initial, reserve):
    distance = Decimal(0)
    stops = []
    for index, station in enumerate(selected):
        distance += route.leg_distances_meters[index] / METERS_PER_MILE
        stops.append(station_input(station, distance))
    return optimize_fuel(stops, route.miles, initial_fuel_gallons=initial, reserve_gallons=reserve)


def repair_waypoints(route, selected, candidates, initial, reserve):
    """Insert at most one midpoint candidate in each newly over-range road leg.

    This is a bounded repair heuristic, not a global road-network search.
    The repaired itinerary is accepted only after another real route validates it.
    """
    additions = []
    selected_ids = {s["opis_id"] for s in selected}
    for i, meters in enumerate(route.leg_distances_meters):
        maximum = initial * MPG if i == 0 else CAPACITY * MPG
        if i == len(route.leg_distances_meters) - 1:
            maximum -= reserve * MPG
        if meters / METERS_PER_MILE <= maximum:
            continue
        left = selected[i - 1]["progress_miles"] if i else Decimal(0)
        right = selected[i]["progress_miles"] if i < len(selected) else Decimal("Infinity")
        options = [
            s
            for s in candidates
            if s["opis_id"] not in selected_ids and left < s["progress_miles"] < right
        ]
        if not options:
            raise PlanningError(
                "insufficient_verified_stations",
                "No verified station can repair an over-range road leg.",
                details={"leg": i, "distance_miles": number(meters / METERS_PER_MILE, "0.001")},
            )
        target = (left + right) / 2 if right.is_finite() else left + maximum / 2
        choice = min(
            options,
            key=lambda s: (
                abs(s["progress_miles"] - target),
                Decimal(s["price_usd_per_gallon"]),
                s["opis_id"],
            ),
        )
        additions.append(choice)
        selected_ids.add(choice["opis_id"])
    if not additions:
        raise PlanningError(
            "insufficient_verified_stations",
            "The selected road itinerary cannot satisfy the fuel constraints.",
        )
    return sorted([*selected, *additions], key=lambda s: (s["progress_miles"], s["opis_id"]))


def build_plan(start, finish, initial, reserve, catalog, router):
    base_points = points_for(start, finish, [])
    base = router.route(base_points)
    validate_snaps(base, base_points)
    candidates = []
    selected = []
    route = base
    corridor = 0
    if initial * MPG >= base.miles + reserve * MPG:
        plan = final_plan(base, [], initial, reserve)
    else:
        for corridor in (2, 5, 10):
            candidates = candidates_on_route(base, catalog["stations"], corridor_miles=corridor)
            try:
                projected = optimize_fuel(
                    [station_input(s, s["progress_miles"]) for s in candidates],
                    base.miles,
                    initial_fuel_gallons=initial,
                    reserve_gallons=reserve,
                )
                break
            except InfeasibleFuelPlan:
                continue
        else:
            raise PlanningError(
                "insufficient_verified_stations",
                "The verified price-and-coordinate catalog cannot cover this route "
                "within the fuel range.",
                details={
                    "candidate_stations": len(candidates),
                    "corridor_miles": corridor,
                    "action": "Enrich more stations; no city-centre fuel stops are fabricated.",
                },
            )
        selected_ids = {p.station.identifier for p in projected.purchases}
        selected = [s for s in candidates if str(s["opis_id"]) in selected_ids]
        if len(selected) > 98:
            raise PlanningError(
                "too_many_stops", "This itinerary exceeds the routing waypoint limit."
            )
        points = points_for(start, finish, selected)
        route = router.route(points)
        validate_snaps(route, points)
        try:
            plan = final_plan(route, selected, initial, reserve)
        except InfeasibleFuelPlan:
            selected = repair_waypoints(route, selected, candidates, initial, reserve)
            points = points_for(start, finish, selected)
            route = router.route(points)
            validate_snaps(route, points)
            try:
                plan = final_plan(route, selected, initial, reserve)
            except InfeasibleFuelPlan as exc:
                raise PlanningError(
                    "route_not_validated",
                    "A feasible itinerary could not be validated within three routing calls.",
                ) from exc
    return serialize_plan(
        start, finish, base, route, selected, plan, corridor, catalog, router.calls
    )


def serialize_plan(start, finish, base, route, selected, plan, corridor, catalog, calls):
    by_id = {str(s["opis_id"]): s for s in selected}
    purchases = []
    for purchase in plan.purchases:
        station = by_id[purchase.station.identifier]
        purchases.append(
            {
                "station_id": purchase.station.identifier,
                "name": station["name"],
                "address": station["address"],
                "city": station["city"],
                "state": station["state"],
                "latitude": station["latitude"],
                "longitude": station["longitude"],
                "price_usd_per_gallon": station["price_usd_per_gallon"],
                "gallons": number(purchase.gallons),
                "cost_usd": number(purchase.cost_usd, "0.01"),
                "route_mile": number(purchase.station.distance_miles, "0.001"),
                "arrival_fuel_gallons": number(purchase.arrival_fuel_gallons),
                "departure_fuel_gallons": number(purchase.departure_fuel_gallons),
                "coordinate_source": station["coordinate_source"],
            }
        )
    total_cost = sum((Decimal(p["cost_usd"]) for p in purchases), Decimal(0))
    route_id = uuid.uuid4().hex
    features = [
        {
            "type": "Feature",
            "geometry": {"type": "LineString", "coordinates": route.coordinates},
            "properties": {"kind": "route"},
        }
    ]
    for label, p in (("start", start), ("finish", finish)):
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [p["longitude"], p["latitude"]]},
                "properties": {"kind": label, "name": p["label"]},
            }
        )
    for purchase in purchases:
        features.append(
            {
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [purchase["longitude"], purchase["latitude"]],
                },
                "properties": {"kind": "fuel_stop", **purchase},
            }
        )
    return {
        "route_id": route_id,
        "map_url": f"/routes/{route_id}/map/",
        "start": start,
        "finish": finish,
        "distance_miles": number(route.miles, "0.001"),
        "base_route_distance_miles": number(base.miles, "0.001"),
        "extra_route_miles": number(route.miles - base.miles, "0.001"),
        "duration_seconds": round(route.duration_seconds),
        "tank_capacity_gallons": "50",
        "maximum_range_miles": "500",
        "mpg": "10",
        "initial_fuel_gallons": number(plan.initial_fuel_gallons),
        "reserve_gallons": number(plan.reserve_gallons),
        "fuel_consumed_gallons": number(plan.fuel_consumed_gallons),
        "fuel_purchased_gallons": number(plan.fuel_purchased_gallons),
        "final_fuel_gallons": number(plan.final_fuel_gallons),
        "total_fuel_cost_usd": number(total_cost, "0.01"),
        "cost_basis": (
            "Fuel purchased during this trip; acquisition cost of fuel already in the tank "
            "is excluded. USD per US gallon assumed for US source rows."
        ),
        "fuel_stops": purchases,
        "road_leg_distances_miles": [
            number(d / METERS_PER_MILE, "0.001") for d in route.leg_distances_meters
        ],
        "visited_station_ids": [str(s["opis_id"]) for s in selected],
        "geojson": {"type": "FeatureCollection", "features": features},
        "validation": {
            "road_distances_verified": True,
            "fuel_balance_verified": True,
            "optimization_scope": (
                "Minimum fuel purchase cost on the selected, road-validated station itinerary. "
                "Candidate projection and itinerary selection are heuristics, "
                "not a global cheapest-route guarantee."
            ),
            "candidate_corridor_miles": corridor,
            "routing_provider": "OSRM driving (car profile, not commercial-truck restrictions)",
            "initial_route_api_calls": calls,
        },
        "data": catalog["metadata"],
        "warnings": [
            "Fuel prices are from the supplied undated assessment snapshot, not live pump prices.",
            "Conflicting quotes use the highest recorded price for each OPIS station; "
            "fuel type is unspecified in the source.",
            "Only stations with accepted coordinate evidence are eligible. "
            "Unlocated stations are excluded, so feasible cheaper alternatives may be missing.",
            "OSRM road snapping checks proximity, not driveway access, opening hours, "
            "fuel availability or truck legality.",
        ],
    }


def plan_request(data):
    began = time.perf_counter()
    if not isinstance(data, dict) or not {"start", "finish"} <= data.keys():
        raise PlanningError(
            "invalid_request", "Supply a JSON object containing start and finish.", status=400
        )
    extra = set(data) - {"start", "finish", "initial_fuel_gallons", "reserve_gallons"}
    if extra:
        raise PlanningError(
            "unknown_fields",
            "Unexpected request fields.",
            status=400,
            details={"fields": sorted(extra)},
        )
    start = resolve_location(data["start"], "start")
    finish = resolve_location(data["finish"], "finish")
    initial = gallons_parameter(data, "initial_fuel_gallons", 50)
    reserve = gallons_parameter(data, "reserve_gallons", 0)
    catalog = station_catalog()
    identity = json.dumps(
        [start, finish, str(initial), str(reserve), catalog["metadata"], settings.OSRM_BASE_URL],
        sort_keys=True,
    )
    key = "plan:" + hashlib.sha256(identity.encode()).hexdigest()
    result = cache.get(key)
    hit = result is not None
    if result is None:
        if not _planning_lock.acquire(timeout=2):
            raise PlanningError(
                "planner_busy", "Another route is being calculated. Retry shortly.", status=503
            )
        try:
            result = cache.get(key)
            if result is not None:
                hit = True
            else:
                result = build_plan(start, finish, initial, reserve, catalog, OSRMClient())
                cache.set(key, result, settings.ROUTE_CACHE_TTL)
                cache.set("route:" + result["route_id"], result, settings.ROUTE_CACHE_TTL)
        finally:
            _planning_lock.release()
    # A map and plan occupy separate bounded-cache entries. Republish the map
    # even on a plan cache hit, so an independently evicted map is usable again.
    cache.set("route:" + result["route_id"], result, settings.ROUTE_CACHE_TTL)
    result = copy.deepcopy(result)
    result["performance"] = {
        "cache_hit": hit,
        "route_api_calls": 0 if hit else result["validation"]["initial_route_api_calls"],
        "elapsed_ms": round((time.perf_counter() - began) * 1000, 2),
    }
    return result
