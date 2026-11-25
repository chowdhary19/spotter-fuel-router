#!/usr/bin/env python3
"""Build a traceable fuel catalog and an endpoint-only Census place lookup.

This preparation command performs no network calls. It never turns a city center
into a fuel-station coordinate. Coordinates must be provided separately with
source provenance and an explicit verified flag.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

US_STATES = frozenset(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN "
    "MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA "
    "WA WV WI WY DC".split()
)
CANADIAN_PROVINCES = frozenset("AB BC MB NB NL NS NT NU ON PE QC SK YT".split())
SOURCE_FIELDS = (
    "OPIS Truckstop ID",
    "Truckstop Name",
    "Address",
    "City",
    "State",
    "Rack ID",
    "Retail Price",
)
PRICE_POLICY = "maximum_observed_quote_per_opis_id"
GAZETTEER_SOURCE_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2026_Gazetteer/2026_Gaz_place_national.zip"
)
DEFAULT_CITY_ALIASES = Path(__file__).resolve().parents[1] / "data" / "city_aliases.json"
LEGAL_SUFFIXES = (
    " consolidated government",
    " metropolitan government",
    " unified government",
    " metro government",
    " city and borough",
    " urban county",
    " municipality",
    " corporation",
    " borough",
    " village",
    " city",
    " town",
    " CDP",
)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def clean_text(value: str) -> str:
    return " ".join(value.split())


def parse_prices(source: Path) -> tuple[list[dict], dict]:
    """Canonicalize source quote records without implying quote chronology."""
    by_id: dict[str, list[tuple[int, dict, Decimal]]] = defaultdict(list)
    non_us_records = []
    total_rows = 0
    exact_rows: set[tuple[str, ...]] = set()
    exact_duplicate_rows = 0
    with source.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if tuple(reader.fieldnames or ()) != SOURCE_FIELDS:
            raise ValueError(f"Fuel CSV headers must be exactly {SOURCE_FIELDS!r}")
        for source_row, record in enumerate(reader, 2):
            total_rows += 1
            if None in record or any(
                value is None or not value.strip() for value in record.values()
            ):
                raise ValueError(f"Fuel CSV row {source_row}: missing field or wrong column count")
            for field in ("OPIS Truckstop ID", "Rack ID"):
                if not re.fullmatch(r"[0-9]+", record[field]):
                    raise ValueError(f"Fuel CSV row {source_row}: invalid {field}")
            state = record["State"].strip().upper()
            if state not in US_STATES | CANADIAN_PROVINCES:
                raise ValueError(f"Fuel CSV row {source_row}: unknown state/province {state!r}")
            try:
                price = Decimal(record["Retail Price"])
            except InvalidOperation as exc:
                raise ValueError(f"Fuel CSV row {source_row}: invalid price") from exc
            if not price.is_finite() or price <= 0:
                raise ValueError(f"Fuel CSV row {source_row}: price must be finite and positive")
            row_key = tuple(record[field] for field in SOURCE_FIELDS)
            if row_key in exact_rows:
                exact_duplicate_rows += 1
            exact_rows.add(row_key)
            if state not in US_STATES:
                non_us_records.append(
                    {
                        "source_row": source_row,
                        "opis_id": record["OPIS Truckstop ID"],
                        "state": state,
                    }
                )
                continue
            by_id[record["OPIS Truckstop ID"]].append((source_row, record, price))

    canonical = []
    for opis_id, records in sorted(by_id.items(), key=lambda item: int(item[0])):
        first = records[0][1]
        for field in ("Address", "City", "State", "Rack ID"):
            values = {clean_text(record[field]) for _, record, _ in records}
            if len(values) > 1:
                raise ValueError(
                    f"OPIS {opis_id}: conflicting {field}; manual identity review required"
                )
        aliases = list(
            dict.fromkeys(clean_text(record["Truckstop Name"]) for _, record, _ in records)
        )
        name_counts = Counter(clean_text(record["Truckstop Name"]) for _, record, _ in records)
        # max is stable on ties, preserving the first source appearance.
        name = max(aliases, key=name_counts.__getitem__)
        variants: dict[Decimal, list[int]] = defaultdict(list)
        for source_row, _, price in records:
            variants[price].append(source_row)
        selected = max(variants)
        canonical.append(
            {
                "opis_id": opis_id,
                "name": name,
                "aliases": aliases,
                "address": clean_text(first["Address"]),
                "city": clean_text(first["City"]),
                "state": first["State"].strip().upper(),
                "rack_id": first["Rack ID"],
                "price_usd_per_gallon": str(selected),
                "quote_min_usd_per_gallon": str(min(variants)),
                "quote_max_usd_per_gallon": str(selected),
                "price_conflict": len(variants) > 1,
                "source_rows": [source_row for source_row, _, _ in records],
                "quote_variants": [
                    {"price": str(price), "source_rows": source_rows}
                    for price, source_rows in sorted(variants.items())
                ],
            }
        )
    counts = {
        "source_rows": total_rows,
        "us_rows": sum(len(station["source_rows"]) for station in canonical),
        "us_stations": len(canonical),
        "excluded_non_us_rows": len(non_us_records),
        "excluded_non_us_station_ids": len({row["opis_id"] for row in non_us_records}),
        "exact_duplicate_excess_rows": exact_duplicate_rows,
        "us_price_conflict_stations": sum(station["price_conflict"] for station in canonical),
    }
    metadata = {
        "source_file": source.name,
        "source_sha256": digest(source),
        "price_policy": PRICE_POLICY,
        "price_policy_explanation": (
            "Use the maximum supplied quote per station as a conservative planning estimate. "
            "Source quotes lack timestamps and fuel grades; "
            "this is not a verified current pump price."
        ),
        "price_unit_assumption": (
            "USD per US gallon for US records; the supplied CSV does not declare a unit/currency."
        ),
        "source_rows_are_one_based_including_header": True,
        "counts": counts,
        "excluded_non_us_records": non_us_records,
    }
    return canonical, metadata


def load_coordinates(
    source: Path | None, known_ids: set[str]
) -> tuple[dict[str, dict], list[dict]]:
    """Read verified station points. Invalid/conflicting rows are quarantined."""
    if source is None:
        return {}, []
    if source.suffix.casefold() == ".csv":
        with source.open(encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    else:
        payload = json.loads(source.read_text(encoding="utf-8"))
        if not isinstance(payload, (list, dict)):
            raise ValueError("Coordinate JSON must be an array or an object with a stations array")
        rows = payload if isinstance(payload, list) else payload.get("stations")
        if not isinstance(rows, list):
            raise ValueError("Coordinate JSON must be an array or an object with a stations array")
    accepted: dict[str, dict] = {}
    rejected = []
    conflicting_ids = set()
    for row_number, row in enumerate(rows, 1):
        opis_id = str(row.get("opis_id", "")).strip() if isinstance(row, dict) else ""
        reasons = []
        if opis_id not in known_ids:
            reasons.append("unknown_or_non_us_opis_id")
        if not isinstance(row, dict):
            rejected.append(
                {"coordinate_row": row_number, "opis_id": opis_id, "reasons": ["not_an_object"]}
            )
            continue
        if str(row.get("verified", "")).casefold() not in {"true", "1", "yes"}:
            reasons.append("not_explicitly_verified")
        try:
            if isinstance(row.get("latitude"), bool) or isinstance(row.get("longitude"), bool):
                raise ValueError
            lat = float(row["latitude"])
            lon = float(row["longitude"])
            if (
                not math.isfinite(lat)
                or not math.isfinite(lon)
                or not -90 <= lat <= 90
                or not -180 <= lon <= 180
            ):
                raise ValueError
        except (KeyError, TypeError, ValueError):
            lat = lon = None
            reasons.append("invalid_coordinates")
        url = str(row.get("source_url", "")).strip()
        method = str(row.get("method", "")).strip()
        confidence = str(row.get("confidence", "")).strip().casefold()
        parsed_url = urlparse(url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            reasons.append("missing_or_invalid_source_url")
        if not method:
            reasons.append("missing_match_method")
        if any(
            token in method.casefold()
            for token in ("centroid", "city_center", "city_centre", "gazetteer", "state_center")
        ):
            reasons.append("coarse_location_is_not_a_station_point")
        if confidence not in {"high", "verified", "exact"}:
            reasons.append("insufficient_coordinate_confidence")
        if reasons:
            rejected.append({"coordinate_row": row_number, "opis_id": opis_id, "reasons": reasons})
            continue
        point = {
            "latitude": lat,
            "longitude": lon,
            "coordinate_source": {"url": url, "method": method, "confidence": confidence},
        }
        if row.get("distance_to_source_meters") not in (None, ""):
            try:
                distance = float(row["distance_to_source_meters"])
                if not math.isfinite(distance) or distance < 0:
                    raise ValueError
                point["coordinate_source"]["distance_to_source_meters"] = distance
            except (TypeError, ValueError):
                rejected.append(
                    {
                        "coordinate_row": row_number,
                        "opis_id": opis_id,
                        "reasons": ["invalid_source_match_distance"],
                    }
                )
                continue
        if opis_id in accepted and point != accepted[opis_id]:
            conflicting_ids.add(opis_id)
            rejected.append(
                {
                    "coordinate_row": row_number,
                    "opis_id": opis_id,
                    "reasons": ["conflicting_verified_coordinate_rows"],
                }
            )
            continue
        accepted[opis_id] = point
    for opis_id in conflicting_ids:
        accepted.pop(opis_id, None)
    return accepted, rejected


def build_catalog(fuel_csv: Path, coordinates: Path | None = None) -> tuple[dict, dict]:
    canonical, metadata = parse_prices(fuel_csv)
    points, rejected_coordinates = load_coordinates(
        coordinates, {station["opis_id"] for station in canonical}
    )
    usable = []
    missing_ids = []
    for station in canonical:
        point = points.get(station["opis_id"])
        if point is None:
            missing_ids.append(station["opis_id"])
            continue
        usable.append({**station, **point})
    metadata["counts"].update(
        {
            "routable_stations": len(usable),
            "unlocated_us_stations": len(missing_ids),
            "rejected_coordinate_rows": len(rejected_coordinates),
        }
    )
    metadata["coordinate_input"] = (
        None
        if coordinates is None
        else {"filename": coordinates.name, "sha256": digest(coordinates)}
    )
    metadata["coordinate_policy"] = (
        "Only explicitly verified station points with source URL, non-centroid match method "
        "and high/verified/exact confidence are usable. A point is not proof of road access."
    )
    excluded = metadata.pop("excluded_non_us_records")
    catalog = {"metadata": metadata, "stations": usable}
    audit = {
        "metadata": metadata,
        "canonical_us_stations": canonical,
        "excluded_non_us_records": excluded,
        "unlocated_us_station_ids": missing_ids,
        "rejected_coordinate_rows": rejected_coordinates,
        "limitations": [
            "No source quote timestamps, fuel grade, currency or unit are provided.",
            "Geographic coverage is the located subset, not every station in the fuel CSV.",
            "Road reachability and detour distance must be validated separately "
            "by the routing layer.",
        ],
    }
    return catalog, audit


def strip_legal_suffix(name: str) -> str:
    result = name.removesuffix(" (balance)")
    for suffix in LEGAL_SUFFIXES:
        if result.endswith(suffix):
            return result[: -len(suffix)]
    return result


def build_cities(
    gazetteer: Path,
    source_url: str = GAZETTEER_SOURCE_URL,
    aliases_path: Path | None = DEFAULT_CITY_ALIASES,
) -> dict:
    required = {"USPS", "GEOID", "NAME", "INTPTLAT", "INTPTLONG"}
    places = []
    excluded_rows = 0
    seen_ids = set()
    with gazetteer.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter="|")
        if not required.issubset(reader.fieldnames or ()):
            raise ValueError("Gazetteer must contain USPS, GEOID, NAME, INTPTLAT and INTPTLONG")
        for row_number, row in enumerate(reader, 2):
            state = row["USPS"].strip().upper()
            if state not in US_STATES:
                excluded_rows += 1
                continue
            geoid = row["GEOID"].strip()
            if geoid in seen_ids:
                raise ValueError(f"Gazetteer row {row_number}: duplicate GEOID {geoid}")
            seen_ids.add(geoid)
            latitude, longitude = float(row["INTPTLAT"]), float(row["INTPTLONG"])
            if (
                not math.isfinite(latitude)
                or not math.isfinite(longitude)
                or not -90 <= latitude <= 90
                or not -180 <= longitude <= 180
            ):
                raise ValueError(f"Gazetteer row {row_number}: invalid coordinates")
            official_name = clean_text(row["NAME"])
            name = strip_legal_suffix(official_name)
            aliases = list(
                dict.fromkeys([name, official_name, official_name.removesuffix(" (balance)")])
            )
            places.append(
                {
                    "geoid": geoid,
                    "name": name,
                    "official_name": official_name,
                    "state": state,
                    "latitude": latitude,
                    "longitude": longitude,
                    "aliases": aliases,
                }
            )
    alias_metadata = None
    if aliases_path is not None:
        alias_data = json.loads(aliases_path.read_text())
        by_geoid = {place["geoid"]: place for place in places}
        applied_aliases = 0
        for record in alias_data["places"]:
            place = by_geoid.get(record["geoid"])
            if place is None:
                continue
            if place["state"] != record["state"] or place["name"] != record["census_name"]:
                raise ValueError("Sourced city alias no longer matches its Census identity")
            if not record["sources"] or not record["aliases"]:
                raise ValueError("City aliases require explicit source evidence")
            place["aliases"] = list(dict.fromkeys([*place["aliases"], *record["aliases"]]))
            place["alias_sources"] = record["sources"]
            applied_aliases += len(record["aliases"])
        alias_metadata = {
            "filename": aliases_path.name,
            "sha256": digest(aliases_path),
            "aliases_applied": applied_aliases,
            "attribution": alias_data["metadata"],
        }
    return {
        "metadata": {
            "source_file": gazetteer.name,
            "source_sha256": digest(gazetteer),
            "source_url": source_url,
            "sourced_aliases": alias_metadata,
            "coordinate_use": "endpoint_resolution_only",
            "coordinate_meaning": (
                "Census internal representative point of a place, "
                "not a street address or fuel station"
            ),
            "scope": "50 US states and DC; Puerto Rico and other territories excluded",
            "counts": {"places": len(places), "excluded_territory_rows": excluded_rows},
        },
        "places": places,
    }


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fuel-csv", type=Path, required=True)
    parser.add_argument("--gazetteer", type=Path)
    parser.add_argument("--coordinates", type=Path)
    parser.add_argument("--gazetteer-source-url", default=GAZETTEER_SOURCE_URL)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    try:
        catalog, audit = build_catalog(args.fuel_csv, args.coordinates)
        cities = build_cities(args.gazetteer, args.gazetteer_source_url) if args.gazetteer else None
    except (ValueError, KeyError, OSError, csv.Error) as exc:
        parser.error(str(exc))
    write_json(args.output_dir / "stations.json", catalog)
    write_json(args.output_dir / "price_audit.json", audit)
    if cities is not None:
        write_json(args.output_dir / "cities.json", cities)
    print(
        json.dumps(
            {
                "fuel": catalog["metadata"]["counts"],
                "places": cities["metadata"]["counts"] if cities is not None else None,
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
