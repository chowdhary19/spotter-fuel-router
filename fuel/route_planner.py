"""Road itinerary planning: bounded discovery, graph search, final verification."""

import copy
import hashlib
import json
import threading
import time
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

from django.conf import settings
from django.core.cache import cache

from .errors import PlanningError
from .fixed_route_fuel_optimizer import InfeasibleFuelPlan, optimize_road_fuel
from .fuel_route_optimizer import GraphStation, InfeasibleGraphPlan, optimize_road_station_graph
from .geo import meters_between
from .location_catalog import resolve_location, station_catalog
from .routing_client import METERS_PER_MILE, OSRMClient
from .station_candidates import select_station_candidates

_planning_lock = threading.Lock()
CAPACITY = Decimal("50")
MPG = Decimal("10")


def number(value, places="0.000001"):
    """Round only serialized display values, never optimization inputs."""
    return str(Decimal(value).quantize(Decimal(places), rounding=ROUND_HALF_UP))


def gallons_parameter(data, name, default):
    """Validate bounded quantities before any provider request."""
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


def points_for(start, finish, selected):
    return [
        (start["longitude"], start["latitude"]),
        *[(s["longitude"], s["latitude"]) for s in selected],
        (finish["longitude"], finish["latitude"]),
    ]


STATION_SNAP_LIMIT_METERS = 25
CITY_SNAP_LIMIT_METERS = 5000
MATRIX_LEG_TOLERANCE_METERS = Decimal("1")


def endpoint_radius(location):
    """City representatives allow road resolution; exact coordinates stay exact."""
    if location.get("resolution", "").startswith("user-supplied coordinates"):
        return STATION_SNAP_LIMIT_METERS
    return CITY_SNAP_LIMIT_METERS


def validate_snaps(route, points, radiuses):
    """Reject displaced waypoints rather than silently buying fuel elsewhere."""
    for index, (requested, snapped, limit) in enumerate(
        zip(points, route.snapped_waypoints, radiuses, strict=True)
    ):
        snap = meters_between(requested, snapped)
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


def final_plan(route, path_nodes, stations, initial, reserve):
    """Recompute purchases independently on final road legs, including revisits."""
    distance = Decimal(0)
    stops = []
    for path_index, meters in enumerate(route.leg_distances_meters, start=1):
        distance += meters
        node = path_nodes[path_index]
        if 1 <= node <= len(stations):
            station = stations[node - 1]
            stops.append(
                (str(station["opis_id"]), distance, Decimal(station["price_usd_per_gallon"]))
            )
    return optimize_road_fuel(
        stops, route.distance_meters, initial_fuel_gallons=initial, reserve_gallons=reserve
    )


def coverage_error(metadata):
    return PlanningError(
        "insufficient_verified_stations",
        "The verified, road-accessible station graph cannot cover this trip within the fuel range.",
        details={
            **metadata,
            "action": "Add verified station coordinates/prices or widen the "
            "candidate search; no fuel stop or road distance is invented.",
        },
    )


def build_plan(start, finish, initial, reserve, catalog, router):
    """Use route, distance table, final route; one call suffices without purchases."""
    base_points = points_for(start, finish, [])
    endpoint_radii = [endpoint_radius(start), endpoint_radius(finish)]
    base = router.route(base_points, radiuses=endpoint_radii)
    validate_snaps(base, base_points, endpoint_radii)
    selection = {
        "corridor_miles": 0,
        "matrix_station_count": 0,
        "accessible_station_count": 0,
        "subset_was_capped": False,
    }
    if (initial - reserve) * MPG * METERS_PER_MILE >= base.distance_meters:
        plan = final_plan(base, (0, 1), [], initial, reserve)
        return serialize_plan(start, finish, base, base, [], plan, selection, catalog, router.calls)

    candidates, selection = select_station_candidates(base, catalog["stations"])
    if not candidates:
        raise coverage_error(selection)
    points = points_for(start, finish, candidates)
    # Broad table snaps allow excluding bad station points individually.
    # Only <=25m station snaps enter the optimizer and final itinerary.
    matrix = router.table(
        points, radiuses=[endpoint_radii[0], *([5000] * len(candidates)), endpoint_radii[1]]
    )
    retained = [0]
    excluded = []
    for index, station in enumerate(candidates, start=1):
        offset = meters_between(points[index], matrix.snapped_waypoints[index])
        if offset <= STATION_SNAP_LIMIT_METERS:
            retained.append(index)
        else:
            excluded.append(
                {"station_id": str(station["opis_id"]), "snap_distance_meters": round(offset, 1)}
            )
    retained.append(len(points) - 1)
    for index, limit in ((0, endpoint_radii[0]), (len(points) - 1, endpoint_radii[1])):
        if meters_between(points[index], matrix.snapped_waypoints[index]) > limit:
            raise PlanningError(
                "waypoint_too_far_from_road",
                "An endpoint could not be resolved closely enough to a road.",
            )
    candidates = [candidates[i - 1] for i in retained[1:-1]]
    points = [points[i] for i in retained]
    snapped_points = [matrix.snapped_waypoints[i] for i in retained]
    distances = tuple(tuple(matrix.distances_meters[i][j] for j in retained) for i in retained)
    selection.update(
        accessible_station_count=len(candidates),
        excluded_station_snaps=excluded,
        maximum_station_snap_meters=STATION_SNAP_LIMIT_METERS,
    )
    try:
        graph = optimize_road_station_graph(
            [
                GraphStation(str(s["opis_id"]), Decimal(s["price_usd_per_gallon"]))
                for s in candidates
            ],
            distances,
            initial_fuel_gallons=initial,
            reserve_gallons=reserve,
        )
    except InfeasibleGraphPlan as exc:
        raise coverage_error(selection) from exc
    if len(graph.node_indices) > 100:
        raise PlanningError("too_many_stops", "The optimized itinerary exceeds 100 road waypoints.")
    route_points = [points[i] for i in graph.node_indices]
    radiuses = [
        endpoint_radii[0]
        if i == 0
        else endpoint_radii[1]
        if i == len(points) - 1
        else STATION_SNAP_LIMIT_METERS
        for i in graph.node_indices
    ]
    route = router.route(route_points, radiuses=radiuses)
    validate_snaps(route, route_points, radiuses)
    # One metre accommodates provider rounding, not a detour allowance.
    for position, (left, right, actual) in enumerate(
        zip(
            graph.node_indices[:-1], graph.node_indices[1:], route.leg_distances_meters, strict=True
        )
    ):
        expected = distances[left][right]
        if (
            expected is None
            or abs(actual - expected) > MATRIX_LEG_TOLERANCE_METERS
            or meters_between(route.snapped_waypoints[position], snapped_points[left]) > 1
            or meters_between(route.snapped_waypoints[position + 1], snapped_points[right]) > 1
        ):
            raise PlanningError(
                "route_changed_after_optimization",
                "The final road itinerary differs from the optimized road matrix; retry the trip.",
                details={
                    "leg": position,
                    "matrix_meters": str(expected),
                    "route_meters": str(actual),
                },
                status=503,
            )
    try:
        plan = final_plan(route, graph.node_indices, candidates, initial, reserve)
    except InfeasibleFuelPlan as exc:
        raise PlanningError(
            "route_not_validated", "Final road distances cannot satisfy the fuel constraints."
        ) from exc
    selected = [candidates[i - 1] for i in graph.node_indices if 1 <= i <= len(candidates)]
    selection["graph_minimum_purchase_cost_usd"] = number(graph.total_cost_usd, "0.000001")
    selection["matrix_route_max_leg_difference_meters"] = str(
        max(
            abs(actual - distances[left][right])
            for left, right, actual in zip(
                graph.node_indices[:-1],
                graph.node_indices[1:],
                route.leg_distances_meters,
                strict=True,
            )
        )
    )
    return serialize_plan(
        start, finish, base, route, selected, plan, selection, catalog, router.calls
    )


def serialize_plan(start, finish, base, route, selected, plan, selection, catalog, calls):
    """Build map features and a reconciled, auditable fuel-purchase response."""
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
                "Minimum purchase cost over selected stations and directed provider road segments; "
                "final purchases independently verified on the returned route. Candidate selection "
                "is bounded and station geography is incomplete, so this is not an all-roads, "
                "all-stations nationwide optimum."
            ),
            "candidate_corridor_miles": selection["corridor_miles"],
            "station_selection": selection,
            "matrix_and_final_route_consistent": calls == 3,
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
    """Resolve input, reuse compatible cached plans, and bound cold-plan concurrency."""
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
        [
            "road-matrix-v2",
            start,
            finish,
            str(initial),
            str(reserve),
            catalog["metadata"],
            settings.OSRM_BASE_URL,
        ],
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
