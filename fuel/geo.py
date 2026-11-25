"""Route projection is candidate selection only; final distances come from routing."""

import math
from decimal import Decimal

from pyproj import Transformer
from shapely.geometry import LineString, Point
from shapely.ops import transform

from .routing_client import METERS_PER_MILE


def meters_between(a, b):
    lon1, lat1 = map(math.radians, a)
    lon2, lat2 = map(math.radians, b)
    h = (
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    )
    return 6_371_008.8 * 2 * math.asin(min(1, math.sqrt(h)))


def _candidates_at_point(point, stations, margin_meters):
    """Keep local options when a trip has no line onto which to project them."""
    result = []
    for station in stations:
        offset = meters_between(point, (station["longitude"], station["latitude"]))
        if offset <= margin_meters:
            result.append(
                {
                    **station,
                    "progress_miles": Decimal(0),
                    "straight_line_offset_miles": offset / float(METERS_PER_MILE),
                }
            )
    return sorted(result, key=lambda s: str(s["opis_id"]))


def candidates_on_route(route, stations, *, corridor_miles=2.0):
    """Cheap spatial prefilter, then project into a metre-based local CRS.

    Projection does NOT prove motorway access. Selected stations must be routed
    as real waypoints and checked for snap distance before any plan is returned.
    """
    margin = corridor_miles * float(METERS_PER_MILE)
    if route.miles == 0:
        # A zero-distance trip can still need fuel to satisfy its requested
        # arrival reserve. Nearby pumps remain candidates, subject to routing.
        return _candidates_at_point(route.coordinates[0], stations, margin)
    midpoint = route.coordinates[len(route.coordinates) // 2]
    projection = Transformer.from_crs(
        "EPSG:4326",
        f"+proj=aeqd +lat_0={midpoint[1]} +lon_0={midpoint[0]} +datum=WGS84 +units=m",
        always_xy=True,
    )
    line = transform(projection.transform, LineString(route.coordinates))
    if not math.isfinite(line.length):
        return []
    if line.length == 0:
        return _candidates_at_point(route.coordinates[0], stations, margin)
    minx, miny, maxx, maxy = line.bounds
    result = []
    for station in stations:
        x, y = projection.transform(station["longitude"], station["latitude"])
        if not (minx - margin <= x <= maxx + margin and miny - margin <= y <= maxy + margin):
            continue
        point = Point(x, y)
        offset = line.distance(point)
        if offset > margin:
            continue
        fraction = Decimal(str(line.project(point) / line.length))
        result.append(
            {
                **station,
                "progress_miles": fraction * route.miles,
                "straight_line_offset_miles": offset / float(METERS_PER_MILE),
            }
        )
    return sorted(result, key=lambda s: (s["progress_miles"], str(s["opis_id"])))
