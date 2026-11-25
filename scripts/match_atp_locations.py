#!/usr/bin/env python3
"""Match additional CSV stations to pinned AllThePlaces locator snapshots.

AllThePlaces is an aggregator, not a live official feed. Exact identities and
state polygons establish coordinate provenance, not current fuel availability
or road access. This module never imports external prices or geocodes cities.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from build_catalog import parse_prices, write_json
from match_chain_locations import (
    NAME_TO_STATE,
    cities_match,
    distance_meters,
    load_state_boundaries,
    normalize,
    valid_point,
)
from shapely.geometry import Point

DEFAULT_MANIFEST = Path(__file__).resolve().parents[1] / "data/supplemental_sources.json"
MAX_SNAPSHOT_BYTES = 20 * 1024 * 1024
SUPPORTED_SPIDERS = frozenset(
    {
        "circle_k",
        "kwik_trip",
        "caseys_general_store_us",
        "maverik",
        "sheetz",
        "speedway_us",
        "seven_eleven_ca_us",
        "rutters_us",
        "cefco_us",
        "roadys_us",
    }
)
# Explicit spelling variants present in the price source and locator snapshots.
BRANDS = {
    "circle k": "circle_k",
    "kwik trip": "kwik_trip",
    "kwik star": "kwik_star",
    "caseys": "caseys",
    "casey's general store": "caseys",
    "casey's": "caseys",
    "maverik country store": "maverik",
    "maverik": "maverik",
    "sheetz": "sheetz",
    "speedway": "speedway",
    "7 eleven": "7_eleven",
    "stripes": "stripes",
    "stripes 7fleet": "stripes",
    "rutters": "rutters",
    "rutter's": "rutters",
    "cefco": "cefco",
}
BRANDS = {normalize(key): value for key, value in BRANDS.items()}
COORDINATE_FIELDS = [
    "opis_id",
    "latitude",
    "longitude",
    "source_url",
    "method",
    "confidence",
    "verified",
]


def source_identities(station: dict) -> set[tuple[str, str]]:
    """Read explicit #store labels only; conflicting aliases remain conflicting."""
    identities = set()
    for alias in station["aliases"]:
        match = re.fullmatch(r"(.+?)\s*#\s*(\d+)\s*", alias.strip())
        if match and (brand := BRANDS.get(normalize(match[1]))):
            identities.add((brand, str(int(match[2]))))
    return identities


def read_manifest(path: Path = DEFAULT_MANIFEST) -> dict:
    """Reject unbounded or ambiguous snapshot manifests before reading/downloading."""
    manifest = json.loads(path.read_text())
    snapshots = manifest.get("snapshots", [])
    if not snapshots or len(snapshots) > len(SUPPORTED_SPIDERS):
        raise ValueError("Supplemental manifest has no snapshots or too many entries")
    seen = set()
    for source in snapshots:
        spider = source["spider"]
        if spider not in SUPPORTED_SPIDERS or spider in seen:
            raise ValueError("Unknown or duplicate supplemental spider")
        seen.add(spider)
        if source["filename"] != f"{spider}.geojson":
            raise ValueError("Supplemental snapshot filename must match its spider")
        if not source["url"].startswith("https://alltheplaces-data.openaddresses.io/runs/"):
            raise ValueError("Supplemental URL must identify a pinned AllThePlaces snapshot")
        if "/latest/" in source["url"] or not re.fullmatch(r"[0-9a-f]{64}", source["sha256"]):
            raise ValueError("Supplemental snapshot must have a fixed version and SHA-256")
        size = source["bytes"]
        if (
            isinstance(size, bool)
            or not isinstance(size, int)
            or not 0 < size <= MAX_SNAPSHOT_BYTES
        ):
            raise ValueError("Supplemental snapshot size exceeds the allowed limit")
    return manifest


def parse_features(payload: dict, source: dict) -> tuple[list[dict], Counter]:
    """Normalize known locator ID semantics without treating arbitrary refs as IDs."""
    spider = source["spider"]
    if spider not in SUPPORTED_SPIDERS:
        raise ValueError("Unsupported supplemental spider")
    features = payload.get("features")
    if payload.get("type") != "FeatureCollection" or not isinstance(features, list):
        raise ValueError(f"Invalid GeoJSON collection for {spider}")
    shop_evidence = defaultdict(list)
    if spider == "seven_eleven_ca_us":
        for feature in features:
            props = feature["properties"]
            ref = str(props.get("ref", ""))
            if ref.endswith("_SHOP"):
                shop_evidence[ref[:-5]].append(feature)
    records, skipped = [], Counter()
    for feature in features:
        props, geometry = feature["properties"], feature.get("geometry") or {}
        if (
            props.get("addr:country") != "US"
            or props.get("end_date")
            or props.get("disused") == "yes"
        ):
            skipped["not_us_or_closed"] += 1
            continue
        if geometry.get("type") != "Point" or len(geometry.get("coordinates", [])) < 2:
            skipped["not_point"] += 1
            continue
        longitude, latitude = geometry["coordinates"][:2]
        if not valid_point(latitude, longitude):
            skipped["invalid_coordinate"] += 1
            continue
        locator_url = props.get("@source_uri") or ""
        if not locator_url.startswith("https://") or props.get("@spider") != spider:
            skipped["missing_or_conflicting_locator_provenance"] += 1
            continue
        brand = BRANDS.get(normalize(props.get("brand") or ""))
        brand_evidence = None
        if spider == "seven_eleven_ca_us" and props.get("amenity") == "fuel":
            # Collector emits branding on the separate shop feature. The fuel
            # feature is used only when this is the exact same store and point.
            shops = [
                candidate
                for candidate in shop_evidence.get(str(props.get("ref", "")), [])
                if candidate["geometry"] == geometry
                and candidate["properties"].get("addr:city") == props.get("addr:city")
                and candidate["properties"].get("addr:state") == props.get("addr:state")
                and candidate["properties"].get("@source_uri") == locator_url
            ]
            if len(shops) == 1:
                brand = BRANDS.get(normalize(shops[0]["properties"].get("brand") or ""))
                brand_evidence = shops[0]
        name, city = props.get("name") or "", props.get("addr:city") or ""
        state = props.get("addr:state") or ""
        state = NAME_TO_STATE.get(state.casefold(), state.upper())
        raw_ref = str(props.get("ref", ""))
        store, identity_kind = raw_ref, "brand_store_number"
        if spider == "roadys_us":
            brand, store, identity_kind = "roadys", normalize(name), "exact_name"
        elif spider == "rutters_us":
            # Rutter's ref is a CMS ID; its name contains the actual store number.
            match = re.fullmatch(r".+?#\s*(\d+)", name)
            store = match[1] if match else ""
        elif spider == "caseys_general_store_us":
            match = re.fullmatch(r"(\d+)_fuel", raw_ref)
            store = match[1] if match else ""
        elif spider == "seven_eleven_ca_us":
            if props.get("amenity") != "fuel":
                continue
            match = re.fullmatch(r"(\d+)(?:_FUEL)?", raw_ref)
            store = match[1] if match else ""
        elif spider == "cefco_us" and not city:
            # The locator provides an explicit comma-delimited city in addr:full.
            match = re.fullmatch(
                r".+,\s*([^,]+),\s*([A-Z]{2})\s+\d{5}(?:-\d{4})?", props.get("addr:full", "")
            )
            if match and match[2] == state:
                city = match[1]
        if not brand or not city or not state or not store:
            skipped["missing_supported_identity_or_city"] += 1
            continue
        if identity_kind == "brand_store_number":
            if not store.isdigit():
                skipped["non_numeric_store"] += 1
                continue
            store = str(int(store))
        records.append(
            {
                "chain": brand,
                "store_number": store,
                "identity_kind": identity_kind,
                "name": name,
                "address": props.get("addr:street_address", props.get("addr:full", "")),
                "city": city,
                "state": state,
                "latitude": latitude,
                "longitude": longitude,
                "source_url": source["url"],
                "source_sha256": source["sha256"],
                "official_source_url": locator_url,
                "website": props.get("website"),
                "spider": spider,
                "feature_id": feature.get("id"),
                "raw_ref": raw_ref,
                "coordinate_type": "AllThePlaces store-locator-derived business point",
                "source_properties": props,
                "brand_evidence": brand_evidence,
            }
        )
    return records, skipped


def load_records(source_dir: Path, manifest: dict) -> tuple[list[dict], dict]:
    """Require exact byte count and pinned hash before parsing any external file."""
    records, skipped, provenance = [], Counter(), []
    for source in manifest["snapshots"]:
        path = source_dir / source["filename"]
        if not path.is_file() or path.stat().st_size != source["bytes"]:
            raise ValueError(f"Missing or wrong-sized supplemental snapshot: {path.name}")
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != source["sha256"]:
            raise ValueError(f"Supplemental snapshot hash mismatch: {path.name}")
        parsed, reasons = parse_features(json.loads(raw), source)
        records.extend(parsed)
        skipped.update(reasons)
        provenance.append({**source, "parsed_records": len(parsed)})
    return records, {"sources": provenance, "feature_skips": dict(skipped)}


def match_supplemental(
    stations: list[dict],
    records: list[dict],
    cities: dict,
    state_boundaries: dict,
    existing_coordinates: list[dict] | None = None,
) -> tuple[list[dict], dict]:
    """Propose append-only matches, excluding ambiguity across all source OPIS IDs."""
    existing_ids = {point["opis_id"] for point in existing_coordinates or []}
    places, by_number, by_name = defaultdict(dict), defaultdict(list), defaultdict(list)
    for place in cities["places"]:
        for alias in place["aliases"]:
            places[(normalize(alias), place["state"])][place["geoid"]] = place
    for record in records:
        if record["identity_kind"] == "exact_name":
            by_name[(record["store_number"], record["state"])].append(record)
        else:
            by_number[(record["chain"], record["store_number"])].append(record)
    accepted, rejected = [], []
    for station in stations:
        identities = source_identities(station)
        if len(identities) > 1:
            rejected.append(
                {"opis_id": station["opis_id"], "reason": "conflicting_source_alias_ids"}
            )
            continue
        identity = next(iter(identities)) if identities else None
        candidates = by_number.get(identity, []) if identity else []
        method = "atp_exact_brand_store_state_city_match"
        if not candidates:
            exact_names = {normalize(alias) for alias in station["aliases"]}
            candidates = [
                record
                for name in exact_names
                for record in by_name.get((name, station["state"]), [])
            ]
            method = "atp_exact_name_state_city_match"
        candidates = [
            record
            for record in candidates
            if record["state"] == station["state"]
            and cities_match(station["city"], record["city"], station["state"], places)
        ]
        groups = defaultdict(list)
        for record in candidates:
            groups[
                (
                    record["chain"],
                    record["store_number"],
                    record["state"],
                    normalize(record["city"]),
                    record["latitude"],
                    record["longitude"],
                )
            ].append(record)
        if len(groups) != 1:
            rejected.append(
                {
                    "opis_id": station["opis_id"],
                    "reason": "no_unique_exact_identity_city_match",
                    "candidate_groups": len(groups),
                }
            )
            continue
        evidence = next(iter(groups.values()))
        record = evidence[0]
        polygon = state_boundaries.get(station["state"])
        if polygon is None or not polygon.covers(Point(record["longitude"], record["latitude"])):
            rejected.append(
                {"opis_id": station["opis_id"], "reason": "outside_source_state_polygon"}
            )
            continue
        distances = [
            (
                distance_meters(
                    record["latitude"], record["longitude"], place["latitude"], place["longitude"]
                ),
                geoid,
            )
            for geoid, place in places.get(
                (normalize(station["city"]), station["state"]), {}
            ).items()
        ]
        distance, geoid = min(distances) if distances else (None, None)
        if distance is not None and distance > 50_000:
            rejected.append(
                {
                    "opis_id": station["opis_id"],
                    "reason": "outside_city_plausibility_threshold",
                    "distance_meters": distance,
                }
            )
            continue
        accepted.append(
            {
                "opis_id": station["opis_id"],
                "latitude": record["latitude"],
                "longitude": record["longitude"],
                "source_url": record["source_url"],
                "method": method,
                "confidence": "high",
                "verified": True,
                "verification_meaning": (
                    "Deterministic identity and geography checks on "
                    "attributed third-party coordinates; "
                    "not manual field verification, road-access proof or current fuel availability"
                ),
                "chain": record["chain"],
                "official_store_number": record["store_number"],
                "official_name": record["name"],
                "official_city": record["city"],
                "official_state": record["state"],
                "official_address": record["address"],
                "coordinate_type": record["coordinate_type"],
                "state_polygon_verified": True,
                "city_reference_distance_meters": round(distance, 3)
                if distance is not None
                else None,
                "city_reference_geoid": geoid,
                "external_evidence": evidence,
            }
        )
    mapped = defaultdict(list)
    for point in accepted:
        mapped[(point["chain"], point["official_store_number"])].append(point["opis_id"])
    result = []
    for point in accepted:
        ids = mapped[(point["chain"], point["official_store_number"])]
        if len(ids) > 1:
            rejected.append(
                {
                    "opis_id": point["opis_id"],
                    "reason": "multiple_opis_ids_match_one_external_identity",
                    "all_opis_ids": ids,
                }
            )
        elif point["opis_id"] not in existing_ids:
            result.append(point)
    audit = {
        "policy": {
            "source_kind": "AllThePlaces CC0 aggregator snapshots with official locator provenance",
            "identity": "Exact explicit brand/store/state/city or unique exact name/state/city",
            "maximum_city_distance_meters": 50_000,
            "no_price_import": True,
            "no_city_geocoding": True,
            "existing_coordinates_preserved": True,
        },
        "counts": {
            "existing_coordinates": len(existing_ids),
            "supplemental_coordinates": len(result),
            "combined_coordinates": len(existing_ids) + len(result),
            "matched_by_chain": dict(sorted(Counter(p["chain"] for p in result).items())),
            "matched_by_state": dict(sorted(Counter(p["official_state"] for p in result).items())),
        },
        "matches": result,
        "unmatched": rejected,
    }
    return result, audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fuel-csv", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--cities", type=Path, default=Path("data/cities.json"))
    parser.add_argument("--state-boundaries", type=Path, default=Path("data/us-boundary.geojson"))
    parser.add_argument("--existing-coordinates", type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--audit-output", required=True, type=Path)
    args = parser.parse_args()
    manifest = read_manifest(args.manifest)
    records, provenance = load_records(args.source_dir, manifest)
    stations, _ = parse_prices(args.fuel_csv)
    existing = []
    if args.existing_coordinates:
        with args.existing_coordinates.open(newline="") as handle:
            existing = list(csv.DictReader(handle))
    coordinates, audit = match_supplemental(
        stations,
        records,
        json.loads(args.cities.read_text()),
        load_state_boundaries(args.state_boundaries),
        existing,
    )
    audit.update(provenance)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, COORDINATE_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(coordinates)
    write_json(args.audit_output, audit)
    print(json.dumps(audit["counts"], indent=2))


if __name__ == "__main__":
    main()
