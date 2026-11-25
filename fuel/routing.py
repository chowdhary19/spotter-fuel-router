"""Bounded OSRM adapter. Nothing here geocodes stations or guesses road distances."""

import math
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings

from .errors import PlanningError

METERS_PER_MILE = Decimal("1609.344")
_provider_lock = threading.Lock()
_last_call = 0.0


def valid_point(point):
    return (
        isinstance(point, (tuple, list))
        and len(point) == 2
        and all(
            isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)
            for v in point
        )
        and -180 <= point[0] <= 180
        and -90 <= point[1] <= 90
    )


@dataclass(frozen=True)
class RoadRoute:
    coordinates: tuple
    distance_meters: Decimal
    duration_seconds: float
    leg_distances_meters: tuple
    snapped_waypoints: tuple

    @property
    def miles(self):
        return self.distance_meters / METERS_PER_MILE


class OSRMClient:
    """At most three calls per plan; one outbound call/second in this process.

    The documented demo deployment uses one process. Multi-process deployments
    must supply their own routing service or a shared outbound rate limiter.
    No implicit retries: a retry also consumes the assessment's call budget.
    """

    def __init__(self, base_url=None):
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")
        self.calls = 0

    def route(self, points):
        global _last_call
        if self.calls >= 3:
            raise PlanningError(
                "routing_budget_exhausted", "Three routing calls were used.", status=503
            )
        if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= 100:
            raise PlanningError("invalid_waypoints", "A route needs between 2 and 100 waypoints.")
        if not all(valid_point(p) for p in points):
            raise PlanningError(
                "invalid_waypoints",
                "Routing waypoints must contain finite longitude/latitude coordinates.",
            )
        coordinates = ";".join(f"{lon:.7f},{lat:.7f}" for lon, lat in points)
        url = f"{self.base_url}/route/v1/driving/{coordinates}"
        if not _provider_lock.acquire(timeout=3):
            raise PlanningError(
                "routing_busy", "Routing is busy. Retry in a few seconds.", status=503
            )
        try:
            time.sleep(max(0.0, 1.05 - (time.monotonic() - _last_call)))
            _last_call = time.monotonic()
            self.calls += 1
            try:
                response = requests.get(
                    url,
                    params={"overview": "full", "geometries": "geojson", "steps": "false"},
                    headers={"User-Agent": "SpotterFuelRouter/0.1 (assessment; no bulk routing)"},
                    timeout=(3, 12),
                )
                if response.status_code == 429:
                    raise PlanningError(
                        "routing_rate_limited",
                        "Routing provider is rate-limiting requests.",
                        status=503,
                    )
                response.raise_for_status()
                payload = response.json()
            except (requests.RequestException, ValueError) as exc:
                raise PlanningError(
                    "routing_unavailable",
                    "The routing provider did not return a usable response.",
                    status=502,
                ) from exc
        finally:
            _provider_lock.release()
        if not isinstance(payload, dict):
            raise PlanningError(
                "invalid_routing_response",
                "The routing provider did not return a JSON object.",
                status=502,
            )
        if payload.get("code") in {"NoRoute", "NoSegment"}:
            raise PlanningError(
                "no_road_route", "No drivable road route was found between these locations."
            )
        if payload.get("code") != "Ok" or not payload.get("routes"):
            raise PlanningError(
                "invalid_routing_response",
                "The routing provider returned an invalid route.",
                status=502,
            )
        try:
            route = payload["routes"][0]
            geometry = route["geometry"]
            coords = geometry["coordinates"]
            legs = tuple(Decimal(str(leg["distance"])) for leg in route["legs"])
            distance = Decimal(str(route["distance"]))
            duration = float(route["duration"])
            snapped = tuple(tuple(w["location"]) for w in payload["waypoints"])
            valid = (
                geometry["type"] == "LineString"
                and len(coords) >= 2
                and len(legs) == len(points) - 1
                and len(snapped) == len(points)
                and all(valid_point(p) for p in snapped)
                and distance.is_finite()
                and distance >= 0
                and all(d.is_finite() and d >= 0 for d in legs)
                and abs(sum(legs) - distance) <= Decimal("2")
                and math.isfinite(duration)
                and duration >= 0
                and all(valid_point(p) for p in coords)
            )
            if not valid:
                raise ValueError("Inconsistent route geometry or legs")
            # Use sum(legs) so optimization and the reported total have exactly
            # the same denominator despite provider rounding of the route total.
            return RoadRoute(tuple(map(tuple, coords)), sum(legs), duration, legs, snapped)
        except (
            KeyError,
            TypeError,
            ValueError,
            ArithmeticError,
            AttributeError,
            IndexError,
        ) as exc:
            raise PlanningError(
                "invalid_routing_response",
                "The routing provider returned inconsistent route data.",
                status=502,
            ) from exc
