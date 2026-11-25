"""Bounded OSRM adapter. Nothing here geocodes stations or guesses road distances."""

import json
import math
import threading
import time
from dataclasses import dataclass
from decimal import Decimal

import requests
from django.conf import settings
from urllib3.exceptions import HTTPError as Urllib3HTTPError

from .errors import PlanningError

METERS_PER_MILE = Decimal("1609.344")
MAX_WAYPOINTS = 100
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
RESPONSE_BUDGET_SECONDS = 20.0
CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 12
# Full GeoJSON and snapped waypoint coordinates can use different decimal
# precision. One metre permits coordinate rounding, not a different road line.
GEOMETRY_ENDPOINT_TOLERANCE_METERS = 1.0
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


@dataclass(frozen=True)
class RoadDistanceMatrix:
    """Directed fastest-road-path distances, not straight lines or shortest paths.

    A None entry means unreachable. Both waypoint arrays are retained so a
    consumer can audit arrival/departure snaps; divergent node snaps are rejected.
    """

    distances_meters: tuple
    input_points: tuple
    source_waypoints: tuple
    destination_waypoints: tuple

    @property
    def snapped_waypoints(self):
        return self.source_waypoints

    @property
    def size(self):
        return len(self.input_points)


def _point_distance_meters(a, b):
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    haversine = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6_371_008.8 * 2 * math.asin(math.sqrt(min(1, max(0, haversine))))


def _decimal_distance(value):
    if isinstance(value, bool) or not isinstance(value, (int, float, str, Decimal)):
        raise ValueError("Distance must be numeric")
    result = Decimal(str(value))
    if not result.is_finite() or result < 0:
        raise ValueError("Distance must be finite and nonnegative")
    return result


def _deadline_check(deadline):
    if time.monotonic() > deadline:
        raise PlanningError(
            "routing_deadline_exceeded",
            "The routing response exceeded its processing time budget.",
            status=504,
        )


def _read_json(response, deadline):
    """Bound bytes and check elapsed time between available network reads.

    read1 yields available bytes rather than waiting to fill a large iter_content
    chunk. This is a checked response budget, not a hard OS-level deadline: a
    blocking read can overrun it by READ_TIMEOUT_SECONDS, and platform DNS
    resolution may not obey socket timeouts. No background I/O thread survives.
    """
    length = response.headers.get("Content-Length")
    if length is not None and int(length) > MAX_RESPONSE_BYTES:
        raise PlanningError(
            "routing_response_too_large", "Routing response exceeds 8 MiB.", status=502
        )
    body = bytearray()
    while True:
        _deadline_check(deadline)
        chunk = response.raw.read1(64 * 1024, decode_content=True)
        _deadline_check(deadline)
        if not chunk:
            break
        if len(body) + len(chunk) > MAX_RESPONSE_BYTES:
            raise PlanningError(
                "routing_response_too_large", "Routing response exceeds 8 MiB.", status=502
            )
        body.extend(chunk)
    payload = json.loads(body)
    _deadline_check(deadline)
    return payload


class OSRMClient:
    """At most three calls per plan; one outbound call/second in this process.

    The documented demo deployment uses one process. Multi-process deployments
    must supply their own routing service or a shared outbound rate limiter.
    No implicit retries: a retry also consumes the assessment's call budget.
    """

    def __init__(self, base_url=None):
        self.base_url = (base_url or settings.OSRM_BASE_URL).rstrip("/")
        self.calls = 0

    def _request(self, service, points, params, radiuses=None):
        global _last_call
        if self.calls >= 3:
            raise PlanningError(
                "routing_budget_exhausted", "Three routing calls were used.", status=503
            )
        if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= MAX_WAYPOINTS:
            raise PlanningError("invalid_waypoints", "Routing needs between 2 and 100 waypoints.")
        if not all(valid_point(p) for p in points):
            raise PlanningError(
                "invalid_waypoints",
                "Routing waypoints must contain finite longitude/latitude coordinates.",
            )
        if radiuses is not None:
            if (
                not isinstance(radiuses, (list, tuple))
                or len(radiuses) != len(points)
                or any(
                    isinstance(radius, bool)
                    or not isinstance(radius, (int, float))
                    or not math.isfinite(radius)
                    or radius < 0
                    for radius in radiuses
                )
            ):
                raise PlanningError(
                    "invalid_radiuses", "Supply one finite nonnegative radius per waypoint."
                )
            params = {**params, "radiuses": ";".join(f"{r:g}" for r in radiuses)}
        coordinates = ";".join(f"{lon:.7f},{lat:.7f}" for lon, lat in points)
        url = f"{self.base_url}/{service}/v1/driving/{coordinates}"
        if not _provider_lock.acquire(timeout=3):
            raise PlanningError(
                "routing_busy", "Routing is busy. Retry in a few seconds.", status=503
            )
        try:
            time.sleep(max(0.0, 1.05 - (time.monotonic() - _last_call)))
            _last_call = time.monotonic()
            deadline = _last_call + RESPONSE_BUDGET_SECONDS
            self.calls += 1
            response = None
            try:
                response = requests.get(
                    url,
                    params=params,
                    headers={
                        "User-Agent": "SpotterFuelRouter/0.2 (assessment; no bulk routing)",
                        "Accept-Encoding": "identity",
                    },
                    timeout=(CONNECT_TIMEOUT_SECONDS, READ_TIMEOUT_SECONDS),
                    allow_redirects=False,
                    stream=True,
                )
                _deadline_check(deadline)
                if 300 <= response.status_code < 400:
                    raise PlanningError(
                        "routing_redirect_rejected",
                        "Routing redirects are disabled to preserve the request budget.",
                        status=502,
                    )
                if response.status_code == 429:
                    raise PlanningError(
                        "routing_rate_limited",
                        "Routing provider is rate-limiting requests.",
                        status=503,
                    )
                # OSRM returns HTTP400 for valid structured service errors such
                # as NoSegment. Preserve their meaning after the bounded parse.
                if response.status_code != 400:
                    response.raise_for_status()
                payload = _read_json(response, deadline)
                if response.status_code == 400 and (
                    not isinstance(payload, dict) or payload.get("code") == "Ok"
                ):
                    response.raise_for_status()
            except (requests.RequestException, Urllib3HTTPError, ValueError, RecursionError) as exc:
                raise PlanningError(
                    "routing_unavailable",
                    "The routing provider did not return a usable response.",
                    status=502,
                ) from exc
            finally:
                if response is not None:
                    response.close()
        finally:
            _provider_lock.release()
        if not isinstance(payload, dict):
            raise PlanningError(
                "invalid_routing_response",
                "The routing provider did not return a JSON object.",
                status=502,
            )
        if payload.get("code") in {"NoRoute", "NoSegment", "NoTable"}:
            raise PlanningError(
                "no_road_route", "No drivable road route was found between these locations."
            )
        if payload.get("code") == "NotImplemented":
            raise PlanningError(
                "routing_service_unsupported",
                "The routing provider does not support the required service.",
                status=503,
            )
        if payload.get("code") == "TooBig":
            raise PlanningError(
                "routing_provider_limit",
                "The routing provider rejected this matrix or waypoint count.",
                status=503,
            )
        if payload.get("code") != "Ok":
            raise PlanningError(
                "invalid_routing_response",
                "The routing provider returned an invalid route.",
                status=502,
            )
        return payload

    def route(self, points, *, radiuses=None):
        payload = self._request(
            "route",
            points,
            {
                "overview": "full",
                "geometries": "geojson",
                "steps": "false",
                "continue_straight": "false",
            },
            radiuses,
        )
        try:
            route = payload["routes"][0]
            geometry = route["geometry"]
            coords = geometry["coordinates"]
            legs = tuple(_decimal_distance(leg["distance"]) for leg in route["legs"])
            distance = _decimal_distance(route["distance"])
            duration = float(_decimal_distance(route["duration"]))
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
                and _point_distance_meters(coords[0], snapped[0])
                <= GEOMETRY_ENDPOINT_TOLERANCE_METERS
                and _point_distance_meters(coords[-1], snapped[-1])
                <= GEOMETRY_ENDPOINT_TOLERANCE_METERS
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

    def table(self, points, *, radiuses=None):
        """One directed n×n matrix; n<=100, no estimated/fallback edges.

        Upstream's default max-table-size is 100; deployed services may impose a
        smaller limit. A TooBig response is reported, never retried in batches.
        """
        payload = self._request(
            "table",
            points,
            {"annotations": "distance", "sources": "all", "destinations": "all"},
            radiuses,
        )
        try:
            if payload.get("fallback_speed_cells"):
                raise ValueError("Estimated edges are not road distances")
            matrix = payload["distances"]
            size = len(points)
            if not isinstance(matrix, list) or len(matrix) != size:
                raise ValueError("Matrix row count mismatch")
            if any(not isinstance(row, list) or len(row) != size for row in matrix):
                raise ValueError("Matrix column count mismatch")
            distances = tuple(
                tuple(None if cell is None else _decimal_distance(cell) for cell in row)
                for row in matrix
            )
            sources = tuple(tuple(item["location"]) for item in payload["sources"])
            destinations = tuple(tuple(item["location"]) for item in payload["destinations"])
            if (
                len(sources) != size
                or len(destinations) != size
                or not all(valid_point(p) for p in (*sources, *destinations))
                or any(distances[index][index] != 0 for index in range(size))
            ):
                raise ValueError("Invalid matrix waypoint metadata or diagonal")
            if any(
                _point_distance_meters(source, destination) > 1.0
                for source, destination in zip(sources, destinations, strict=True)
            ):
                raise ValueError("Source/destination snaps identify different road points")
            return RoadDistanceMatrix(distances, tuple(map(tuple, points)), sources, destinations)
        except (
            KeyError,
            TypeError,
            ValueError,
            ArithmeticError,
            AttributeError,
            IndexError,
        ) as exc:
            raise PlanningError(
                "invalid_routing_matrix",
                "The routing provider returned an inconsistent distance matrix.",
                status=502,
            ) from exc
