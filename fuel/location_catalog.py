"""Load local price/reference catalogs and resolve explicit US place identities.

Common aliases are sourced and attached to Census GEOIDs during data preparation.
Resolution preserves ambiguity rather than guessing between similarly named cities.
"""

import json
import math
import re
import unicodedata
from functools import lru_cache

from django.conf import settings
from shapely.geometry import Point, shape
from shapely.ops import unary_union

from .errors import PlanningError


def normalize_name(value):
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", value.lower()).strip()


@lru_cache(maxsize=1)
def station_catalog():
    try:
        data = json.loads(settings.STATION_DATA_PATH.read_text())
        if not isinstance(data["stations"], list):
            raise ValueError("Invalid catalog")
        return data
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PlanningError(
            "station_data_unavailable",
            "Load the verified station catalog before planning a route.",
            status=503,
        ) from exc


@lru_cache(maxsize=1)
def city_index():
    """Index exact normalized, sourced place aliases and reviewed point corrections."""
    index = {}
    try:
        data = json.loads(settings.CITY_DATA_PATH.read_text())
        corrections = json.loads(settings.CITY_OVERRIDES_PATH.read_text())["overrides"]
        for place in data["places"]:
            if correction := corrections.get(place["geoid"]):
                place = {
                    **place,
                    **{
                        key: correction[key]
                        for key in ("latitude", "longitude", "resolution", "source_url")
                    },
                }
            for alias in set([place["name"], *place.get("aliases", [])]):
                key = (normalize_name(alias), place["state"])
                matches = index.setdefault(key, {})
                matches[place["geoid"]] = place
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PlanningError(
            "city_data_unavailable", "The local city gazetteer is not available.", status=503
        ) from exc
    return index


@lru_cache(maxsize=1)
def us_boundary():
    """Union the 50-state/DC Census polygons without a bounding-box approximation."""
    try:
        data = json.loads(settings.COUNTRY_DATA_PATH.read_text())
        if data["type"] == "FeatureCollection":
            return unary_union([shape(f["geometry"]) for f in data["features"]])
        return shape(data.get("geometry", data))
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise PlanningError(
            "boundary_data_unavailable", "The US boundary data is not available.", status=503
        ) from exc


def resolve_location(value, field):
    """Resolve a city/state or validate literal coordinates; never fuzzy-match a city."""
    if isinstance(value, str):
        parts = value.rsplit(",", 1)
        if len(parts) != 2 or not parts[0].strip() or len(parts[1].strip()) != 2:
            raise PlanningError(
                "invalid_location",
                f'{field} must be "City, ST" or a latitude/longitude object.',
                status=400,
            )
        city, state = parts[0].strip(), parts[1].strip().upper()
        matches = list(city_index().get((normalize_name(city), state), {}).values())
        if not matches:
            raise PlanningError(
                "unknown_city",
                f"No US city match for {value!r}. Use explicit coordinates if needed.",
                status=400,
            )
        if len(matches) > 1:
            raise PlanningError(
                "ambiguous_city",
                f"{value!r} identifies multiple places. Use explicit coordinates.",
                status=400,
                details={
                    "matches": [
                        {
                            "name": p["official_name"],
                            "latitude": p["latitude"],
                            "longitude": p["longitude"],
                        }
                        for p in matches
                    ]
                },
            )
        p = matches[0]
        if not us_boundary().covers(Point(p["longitude"], p["latitude"])):
            raise PlanningError(
                "city_point_not_routable",
                f"The reference point for {value!r} is offshore. Supply a road-accessible "
                "latitude/longitude within that city.",
                status=400,
            )
        return {
            "label": f"{p['name']}, {state}",
            "latitude": p["latitude"],
            "longitude": p["longitude"],
            "resolution": p.get(
                "resolution", "Census representative place point, not a street address"
            ),
            "geoid": p["geoid"],
            **({"source_url": p["source_url"]} if "source_url" in p else {}),
        }
    if not isinstance(value, dict) or set(value) != {"latitude", "longitude"}:
        raise PlanningError(
            "invalid_location", f"{field} needs exactly latitude and longitude.", status=400
        )
    try:
        if any(isinstance(v, bool) or not isinstance(v, (int, float)) for v in value.values()):
            raise ValueError
        lat, lon = float(value["latitude"]), float(value["longitude"])
        if (
            not math.isfinite(lat)
            or not math.isfinite(lon)
            or not -90 <= lat <= 90
            or not -180 <= lon <= 180
        ):
            raise ValueError
    except (ValueError, TypeError, OverflowError):
        raise PlanningError(
            "invalid_coordinates", f"{field} has invalid numeric coordinates.", status=400
        ) from None
    if not us_boundary().covers(Point(lon, lat)):
        raise PlanningError(
            "outside_usa", f"{field} must be within the 50 US states or Washington, DC.", status=400
        )
    return {
        "label": f"{lat:.5f}, {lon:.5f}",
        "latitude": lat,
        "longitude": lon,
        "resolution": "user-supplied coordinates; Census cartographic boundary check",
    }
