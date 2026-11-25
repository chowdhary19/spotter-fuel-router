"""Bounded station discovery for a directed road-distance matrix.

Geographic projection chooses which locations to ask the routing provider about.
It never establishes reachability or purchase quantities. The graph optimizer
uses provider road distances, including detours and one-way-road asymmetry.
"""

import math
from collections import defaultdict
from decimal import Decimal

from .geo import candidates_on_route, meters_between
from .routing_client import MAX_WAYPOINTS

DEFAULT_CORRIDOR_MILES = 75
DEFAULT_MAX_STATIONS = 62


def select_station_candidates(
    route,
    stations,
    *,
    max_stations=DEFAULT_MAX_STATIONS,
    corridor_miles=DEFAULT_CORRIDOR_MILES,
):
    """Retain spatial coverage and price alternatives within a bounded corridor.

    All stations are retained when the pool fits. Otherwise, geographic buckets
    each contribute a near-route and a cheap candidate; remaining slots include
    endpoint access alternatives and low-price choices. There is no claim that
    this bounded subset contains the global optimum across every priced station.
    """
    if (
        isinstance(max_stations, bool)
        or not isinstance(max_stations, int)
        or not 4 <= max_stations <= MAX_WAYPOINTS - 2
    ):
        raise ValueError("Use 4 to 98 station slots, leaving two slots for endpoints")
    pool = candidates_on_route(route, stations, corridor_miles=corridor_miles)
    chosen = {}

    def add(station):
        if len(chosen) < max_stations:
            chosen.setdefault(str(station["opis_id"]), station)

    if len(pool) <= max_stations:
        for station in pool:
            add(station)
    else:
        endpoint_choices = [
            sorted(
                pool,
                key=lambda s: (
                    meters_between(endpoint, (s["longitude"], s["latitude"])),
                    str(s["opis_id"]),
                ),
            )[:3]
            for endpoint in (route.coordinates[0], route.coordinates[-1])
        ]
        # Even a small matrix must retain an alternative at each end. Reserving
        # these first prevents start-near candidates from consuming every slot.
        for choices in endpoint_choices:
            add(choices[0])
        # Buckets preserve broad geographic coverage rather than letting one
        # cheap metropolitan cluster consume the entire matrix budget.
        buckets_count = min(max_stations // 3, max(1, math.ceil(float(route.miles) / 150)))
        buckets = defaultdict(list)
        for station in pool:
            fraction = float(station["progress_miles"] / route.miles) if route.miles else 0
            buckets[min(buckets_count - 1, int(fraction * buckets_count))].append(station)
        for members in buckets.values():
            add(min(members, key=lambda s: (s["straight_line_offset_miles"], str(s["opis_id"]))))
            add(min(members, key=lambda s: (Decimal(s["price_usd_per_gallon"]), str(s["opis_id"]))))
        for alternatives in zip(*endpoint_choices, strict=True):
            for station in alternatives:
                add(station)
        for station in sorted(
            pool,
            key=lambda s: (
                Decimal(s["price_usd_per_gallon"]),
                s["straight_line_offset_miles"],
                str(s["opis_id"]),
            ),
        ):
            add(station)
    selected = sorted(chosen.values(), key=lambda s: (s["progress_miles"], str(s["opis_id"])))
    return selected, {
        "corridor_miles": corridor_miles,
        "corridor_station_count": len(pool),
        "matrix_station_count": len(selected),
        "station_limit": max_stations,
        "subset_was_capped": len(selected) < len(pool),
        "selection_policy": "spatial coverage, near-route access and price alternatives",
    }
