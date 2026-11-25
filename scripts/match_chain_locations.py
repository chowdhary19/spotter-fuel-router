#!/usr/bin/env python3
"""Match source stations to official chain directory snapshots, without guessing.

Matches require chain identity, a store number (or a unique exact TA name), state,
normalized city, and an actual state polygon check. A Census place-point distance
is an additional plausibility check when that place exists in the gazetteer.
Neither polygon nor place point supplies the station coordinate.
This script performs no network requests and never executes downloaded JavaScript.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import unicodedata
import xml.etree.ElementTree as ET
import zipfile
from collections import Counter, defaultdict
from pathlib import Path

from build_catalog import digest, parse_prices, write_json
from shapely.geometry import Point, shape

PILOT_URL = "https://pilotcompany.com/locations/directory"
LOVES_URL = "https://www.loves.com/api/fetch_stores"
TA_URL = "https://www.ta-petro.com/api/locationsdata/download/"
STATE_NAMES = dict(
    zip(
        (
            "AL AK AZ AR CA CO CT DE DC FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO "
            "MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY"
        ).split(),
        (
            "Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|"
            "District of Columbia|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|"
            "Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|"
            "Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|"
            "North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|"
            "South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|"
            "West Virginia|Wisconsin|Wyoming"
        ).split("|"),
        strict=True,
    )
)
NAME_TO_STATE = {value.casefold(): key for key, value in STATE_NAMES.items()}
PILOT_NETWORK_BRANDS = frozenset(
    {"pilot", "flying_j", "one9", "mr_fuel", "pilot_network", "pilot_network_partner"}
)


def chain_identity(chain: str) -> str:
    """Pilot's own directory uses one location-ID namespace across its network."""
    return "pilot_network" if chain in PILOT_NETWORK_BRANDS else chain


def normalize(value: str) -> str:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode().casefold()
    value = re.sub(r"[^\w\s]", " ", value)
    words = value.split()
    expansions = {"st": "saint", "mt": "mount", "ft": "fort"}
    return " ".join(expansions.get(word, word) for word in words)


def load_state_boundaries(path: Path) -> dict:
    """Load the unchanged Census state polygons for identity crosschecks."""
    payload = json.loads(path.read_text())
    boundaries = {}
    for feature in payload["features"]:
        state = feature["properties"]["state"]
        polygon = shape(feature["geometry"])
        if state not in STATE_NAMES or state in boundaries or polygon.is_empty:
            raise ValueError("State boundary file has an unknown, duplicate, or empty state")
        if polygon.geom_type not in {"Polygon", "MultiPolygon"}:
            raise ValueError("State boundary geometry must be polygonal")
        boundaries[state] = polygon
    return boundaries


def normalize_ta_name(value: str) -> str:
    value = normalize(value).replace("travelcenter", "travel center")
    value = re.sub(r"\b(?:travel center|travel centers|stopping center|travel plaza)\b", "", value)
    return " ".join(value.split())


def distance_meters(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    latitude_delta = math.radians(lat2 - lat1)
    longitude_delta = math.radians(lon2 - lon1)
    value = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(longitude_delta / 2) ** 2
    )
    return 6_371_008.8 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, value))))


def valid_point(lat, lon) -> bool:
    return (
        not isinstance(lat, bool)
        and not isinstance(lon, bool)
        and isinstance(lat, (int, float))
        and isinstance(lon, (int, float))
        and math.isfinite(lat)
        and math.isfinite(lon)
        and -90 <= lat <= 90
        and -180 <= lon <= 180
    )


def pilot_records(path: Path) -> list[dict]:
    payload = json.loads(path.read_text())
    records = []
    for site in payload:
        primary = [lob for lob in site["linesOfBusiness"] if lob.get("isPrimaryLineOfBusiness")]
        if len(primary) != 1:
            continue
        business = primary[0]
        public_description = business.get("lobPublicDescription", "").casefold()
        if public_description.startswith("pilot"):
            chain = "pilot"
        elif public_description.startswith("flying j"):
            chain = "flying_j"
        elif public_description.startswith("one9"):
            chain = "one9"
        elif public_description.startswith("mr. fuel") or public_description.startswith("mr fuel"):
            chain = "mr_fuel"
        else:
            # These are explicitly listed partner locations in the same official
            # Pilot directory, not unrelated stations guessed from similar names.
            chain = "pilot_network_partner"
        if site.get("country") != "United States" or not site.get("hasDieselLanes"):
            continue
        if site.get("status") != "Open" or business.get("status") != "Open":
            continue
        if not business.get("isPublicFacing"):
            continue
        selected = None
        coordinate_type = None
        for kind in ("Diesel Routing", "Diesel Entrance"):
            candidates = [
                point
                for point in site.get("geoCodes", [])
                if point.get("geocodeType") == kind and str(point.get("sequence")) == "0"
            ]
            unique = {
                (p.get("latitude"), p.get("longitude"))
                for p in candidates
                if valid_point(p.get("latitude"), p.get("longitude"))
            }
            if len(unique) == 1:
                selected = unique.pop()
                coordinate_type = kind
                break
            if len(unique) > 1:
                # Conflicting primary entrance points need review, not fallback.
                break
        if selected is None:
            continue
        records.append(
            {
                "chain": chain,
                "store_number": str(int(site["locationId"])),
                "name": business.get("lobPublicDescription")
                or business.get("primaryBusinessDescription"),
                "address": site["address1"],
                "city": site["city"],
                "state": site["state"],
                "latitude": selected[0],
                "longitude": selected[1],
                "source_url": PILOT_URL,
                "coordinate_type": coordinate_type,
                "availability_evidence": "Official site and primary business both status Open",
            }
        )
    return records


def loves_records(path: Path) -> list[dict]:
    records = []
    for site in json.loads(path.read_text())["stores"]:
        if not site.get("isLoveStore") or site.get("facilitySubtypeName") not in {
            "Travel Stop",
            "Country Store",
            "Car Stop",
        }:
            continue
        if not valid_point(site.get("latitude"), site.get("longitude")):
            continue
        records.append(
            {
                "chain": "loves",
                "store_number": str(int(site["number"])),
                "name": f"Love's {site['facilitySubtypeName']} #{site['number']}",
                "address": site["address1"],
                "city": site["city"],
                "state": site["state"],
                "latitude": site["latitude"],
                "longitude": site["longitude"],
                "source_url": LOVES_URL,
                "coordinate_type": "Official locator point",
                "availability_evidence": (
                    "Listed in official locator; source has no explicit operating-status field"
                ),
            }
        )
    return records


def read_xlsx_first_sheet(path: Path) -> list[dict]:
    """Read values from the official XLSX without evaluating spreadsheet formulas."""
    ns = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
    with zipfile.ZipFile(path) as archive:
        shared = []
        if "xl/sharedStrings.xml" in archive.namelist():
            root = ET.fromstring(archive.read("xl/sharedStrings.xml"))
            shared = ["".join(node.itertext()) for node in root.findall("m:si", ns)]
        sheet = ET.fromstring(archive.read("xl/worksheets/sheet1.xml"))
        decoded = []
        for row in sheet.findall("m:sheetData/m:row", ns):
            fields = {}
            for cell in row.findall("m:c", ns):
                if cell.find("m:f", ns) is not None:
                    raise ValueError(
                        "Unexpected formula in official location source; review before importing"
                    )
                value = cell.findtext("m:v", "", ns)
                if cell.get("t") == "s":
                    value = shared[int(value)]
                elif cell.get("t") == "inlineStr":
                    value = "".join(cell.find("m:is", ns).itertext())
                column = re.match(r"[A-Z]+", cell.attrib["r"])[0]
                fields[column] = value
            decoded.append(fields)
    headers = decoded[0]
    return [{name: row.get(column, "") for column, name in headers.items()} for row in decoded[1:]]


def ta_records(path: Path) -> list[dict]:
    records = []
    for row in read_xlsx_first_sheet(path):
        brand = row["Brand"].strip().casefold()
        if brand not in {"ta", "petro", "ta express"}:
            continue
        state = NAME_TO_STATE.get(row["State"].strip().casefold())
        if not state:
            continue
        try:
            lat, lon = float(row["Latitude"]), float(row["Longitude"])
        except ValueError:
            continue
        if not valid_point(lat, lon):
            continue
        records.append(
            {
                "chain": "petro" if brand == "petro" else "ta",
                "store_number": str(int(row["Site ID"])),
                "name": row["Location"],
                "address": row["Address"],
                "city": row["City"],
                "state": state,
                "latitude": lat,
                "longitude": lon,
                "source_url": TA_URL,
                "coordinate_type": "Official location sheet point",
                "availability_evidence": (
                    "Listed in official location sheet; no operating-status field"
                ),
            }
        )
    return records


def source_identity(station: dict) -> tuple[str | None, str | None, str | None]:
    """Read explicit network/store identifiers, including declared DBA labels."""
    identities = set()
    for name in station["aliases"]:
        numbered = re.search(
            r"\b(PILOT|FLYING\s+J|ONE9|MR\.?\s+FUEL)\b[^#]*#\s*(\d+)\b", name, re.I
        ) or re.match(r"^\s*(LOVE['’]?S|PETRO|TA)\b.*?#\s*(\d+)\b", name, re.I)
        if numbered:
            family = normalize(numbered[1]).replace(" ", "_")
            family = "loves" if family in {"loves", "love_s"} else family
            identities.add((chain_identity(family), str(int(numbered[2]))))
    if len(identities) > 1:
        return None, None, "conflicting_chain_or_store_numbers_in_aliases"
    if identities:
        chain, store = identities.pop()
        return chain, store, None
    names = {normalize_ta_name(name) for name in station["aliases"]}
    if all(name.startswith("ta ") for name in names):
        return "ta", None, None
    if all(name.startswith("petro ") for name in names):
        return "petro", None, None
    return None, None, "not_a_supported_chain_with_reliable_identity"


def cities_match(source_city: str, official_city: str, state: str, city_points: dict) -> bool:
    """Accept spelling normalization or one identical, explicitly sourced place identity."""
    source_name, official_name = normalize(source_city), normalize(official_city)
    if source_name == official_name:
        return True
    source_ids = set(city_points.get((source_name, state), {}))
    official_ids = set(city_points.get((official_name, state), {}))
    return len(source_ids) == 1 and source_ids == official_ids


def match_stations(
    stations: list[dict],
    official: list[dict],
    cities: dict,
    max_city_distance_meters: float = 50_000,
    state_boundaries: dict | None = None,
) -> tuple[list[dict], dict]:
    by_number: dict[tuple[str, str], list[dict]] = defaultdict(list)
    by_name: dict[tuple[str, str, str], list[dict]] = defaultdict(list)
    city_points: dict[tuple[str, str], dict[str, dict]] = defaultdict(dict)
    for place in cities["places"]:
        for alias in place["aliases"]:
            city_points[(normalize(alias), place["state"])][place["geoid"]] = place
    for record in official:
        by_number[(chain_identity(record["chain"]), record["store_number"])].append(record)
        by_name[(record["chain"], normalize_ta_name(record["name"]), record["state"])].append(
            record
        )
    accepted = []
    rejected = []
    for station in stations:
        chain, number, reason = source_identity(station)
        if reason:
            rejected.append({"opis_id": station["opis_id"], "reason": reason})
            continue
        method = "official_chain_number_state_city_match"
        if number:
            candidates = by_number.get((chain, number), [])
        else:
            method = "official_chain_name_state_city_match"
            candidates_by_identity = {}
            for alias in station["aliases"]:
                for candidate in by_name.get(
                    (chain, normalize_ta_name(alias), station["state"]), []
                ):
                    candidates_by_identity[(candidate["chain"], candidate["store_number"])] = (
                        candidate
                    )
            candidates = list(candidates_by_identity.values())
        candidates = [
            candidate
            for candidate in candidates
            if candidate["state"] == station["state"]
            and cities_match(station["city"], candidate["city"], station["state"], city_points)
        ]
        if len(candidates) != 1:
            rejected.append(
                {
                    "opis_id": station["opis_id"],
                    "reason": "no_unique_official_identity_state_city_match",
                    "chain": chain,
                    "store_number": number,
                    "candidates": len(candidates),
                }
            )
            continue
        candidate = candidates[0]
        state_verified = False
        if state_boundaries is not None:
            polygon = state_boundaries.get(station["state"])
            if polygon is None or not polygon.covers(
                Point(candidate["longitude"], candidate["latitude"])
            ):
                rejected.append(
                    {"opis_id": station["opis_id"], "reason": "official_point_outside_source_state"}
                )
                continue
            state_verified = True
        references = city_points.get((normalize(station["city"]), station["state"]), {})
        if not references and not state_verified:
            rejected.append(
                {
                    "opis_id": station["opis_id"],
                    "reason": "no_census_place_for_geographic_crosscheck",
                    "chain": chain,
                    "store_number": number,
                }
            )
            continue
        distances = [
            (
                distance_meters(
                    candidate["latitude"],
                    candidate["longitude"],
                    place["latitude"],
                    place["longitude"],
                ),
                geoid,
            )
            for geoid, place in references.items()
        ]
        distance, geoid = min(distances) if distances else (None, None)
        if distance is not None and distance > max_city_distance_meters:
            rejected.append(
                {
                    "opis_id": station["opis_id"],
                    "reason": "official_point_outside_city_plausibility_threshold",
                    "distance_meters": round(distance, 3),
                    "chain": chain,
                    "store_number": number,
                }
            )
            continue
        accepted.append(
            {
                "opis_id": station["opis_id"],
                "latitude": candidate["latitude"],
                "longitude": candidate["longitude"],
                "source_url": candidate["source_url"],
                "method": method,
                "confidence": "high",
                "verified": True,
                "verification_meaning": (
                    "Passed documented deterministic identity and geography checks; "
                    "not manually inspected or a guarantee of current fuel availability"
                ),
                "chain": candidate["chain"],
                "identity_namespace": chain_identity(candidate["chain"]),
                "official_store_number": candidate["store_number"],
                "official_name": candidate["name"],
                "official_address": candidate["address"],
                "official_city": candidate["city"],
                "official_state": candidate["state"],
                "coordinate_type": candidate["coordinate_type"],
                "availability_evidence": candidate["availability_evidence"],
                "city_reference_geoid": geoid,
                "city_reference_distance_meters": round(distance, 3)
                if distance is not None
                else None,
                "state_polygon_verified": state_verified,
                "geographic_check": (
                    "state_polygon_and_census_place"
                    if state_verified and references
                    else "state_polygon_without_census_place"
                    if state_verified
                    else "census_place_only"
                ),
            }
        )
    # Do not let two source IDs silently become competing quotes at one known site.
    mapped_ids: dict[tuple[str, str], list[str]] = defaultdict(list)
    for point in accepted:
        mapped_ids[(point["chain"], point["official_store_number"])].append(point["opis_id"])
    duplicate_identities = {key: ids for key, ids in mapped_ids.items() if len(ids) > 1}
    result = []
    for point in accepted:
        identity = (point["chain"], point["official_store_number"])
        if identity in duplicate_identities:
            rejected.append(
                {
                    "opis_id": point["opis_id"],
                    "reason": "multiple_opis_ids_match_one_official_station",
                    "all_opis_ids": duplicate_identities[identity],
                }
            )
        else:
            result.append(point)
    audit = {
        "policy": {
            "identity": (
                "Exact chain+store number+state+normalized city or one identical sourced Census "
                "place identity for both city aliases; if TA/Petro has no store number, "
                "unique exact normalized official/source name+state+city. Pilot, Flying J, ONE9, "
                "Mr. Fuel and listed partner records use Pilot's official location-ID namespace."
            ),
            "normalization": (
                "NFKD/ASCII/casefold, punctuation to spaces, collapse whitespace; "
                "St/Mt/Ft become Saint/Mount/Fort"
            ),
            "geographic_crosscheck": (
                "An exact identity match must lie inside its source state polygon when supplied. "
                "If a same-name/state Census place exists, also check its distance threshold. "
                "Missing Census city names do not invalidate a point inside the confirmed state. "
                "Neither reference supplies the station coordinate."
            ),
            "maximum_city_distance_meters": max_city_distance_meters,
            "pilot_coordinates": (
                "Unique sequence-0 Diesel Routing point, falling back to unique sequence-0 "
                "Diesel Entrance; never geofence vertices"
            ),
            "availability": (
                "Pilot must be Open at site and primary business. Love's/TA must be listed in "
                "official locator/sheet; their sources do not expose explicit operating status."
            ),
            "duplicate_station_identity": (
                "Reject all IDs when multiple OPIS IDs resolve to one official chain/store "
                "identity, pending review"
            ),
            "verified_flag": "Deterministic evidence checks passed; does not mean human inspection",
        },
        "counts": {
            "source_us_stations": len(stations),
            "official_usable_records": len(official),
            "matched_stations": len(result),
            "unmatched_stations": len(rejected),
            "matched_by_chain": dict(sorted(Counter(point["chain"] for point in result).items())),
            "matched_by_state": dict(
                sorted(Counter(point["official_state"] for point in result).items())
            ),
            "rejected_by_reason": dict(sorted(Counter(row["reason"] for row in rejected).items())),
        },
        "matches": result,
        "unmatched": rejected,
    }
    return result, audit


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fuel-csv", type=Path, required=True)
    parser.add_argument("--cities", type=Path, required=True)
    parser.add_argument("--state-boundaries", type=Path, default=Path("data/us-boundary.geojson"))
    parser.add_argument("--official-records", type=Path)
    parser.add_argument("--pilot", type=Path)
    parser.add_argument("--loves", type=Path)
    parser.add_argument("--ta", type=Path)
    parser.add_argument("--export-official-records", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--audit-output", type=Path, required=True)
    parser.add_argument("--max-city-distance-meters", type=float, default=50_000)
    args = parser.parse_args()
    if not math.isfinite(args.max_city_distance_meters) or args.max_city_distance_meters <= 0:
        parser.error("max-city-distance-meters must be finite and positive")
    if args.official_records:
        if args.pilot or args.loves or args.ta:
            parser.error("Use either --official-records or raw --pilot/--loves/--ta inputs")
    elif not args.pilot or not args.loves:
        parser.error("Provide --official-records or both --pilot and --loves")
    stations, source_metadata = parse_prices(args.fuel_csv)
    cities = json.loads(args.cities.read_text())
    if args.official_records:
        snapshot = json.loads(args.official_records.read_text())
        official = snapshot["official_records"]
        official_sources = snapshot["metadata"]["sources"]
        for record in official:
            if not valid_point(record.get("latitude"), record.get("longitude")):
                raise ValueError("Official snapshot contains an invalid coordinate")
            if record.get("source_url") not in {PILOT_URL, LOVES_URL, TA_URL}:
                raise ValueError("Official snapshot contains an unsupported source")
    else:
        official = pilot_records(args.pilot) + loves_records(args.loves)
        if args.ta:
            official += ta_records(args.ta)
        official_sources = {
            "pilot": {"url": PILOT_URL, "snapshot_sha256": digest(args.pilot)},
            "loves": {"url": LOVES_URL, "snapshot_sha256": digest(args.loves)},
            "ta": None if args.ta is None else {"url": TA_URL, "snapshot_sha256": digest(args.ta)},
        }
    if args.export_official_records:
        write_json(
            args.export_official_records,
            {
                "metadata": {
                    "schema_version": 1,
                    "sources": official_sources,
                    "extraction": "pilot_records/loves_records/ta_records in this script",
                    "record_count": len(official),
                    "purpose": "Offline reproducible matching; not live fuel availability",
                },
                "official_records": official,
            },
        )
    coordinates, audit = match_stations(
        stations,
        official,
        cities,
        args.max_city_distance_meters,
        state_boundaries=load_state_boundaries(args.state_boundaries),
    )
    audit["sources"] = {
        "fuel_csv": {"sha256": source_metadata["source_sha256"]},
        "cities": {"sha256": digest(args.cities), "underlying_source": cities["metadata"]},
        "state_boundaries": {
            "filename": args.state_boundaries.name,
            "sha256": digest(args.state_boundaries),
        },
        **official_sources,
    }
    if args.official_records:
        audit["sources"]["official_records"] = {
            "filename": args.official_records.name,
            "sha256": digest(args.official_records),
        }
    fieldnames = [
        "opis_id",
        "latitude",
        "longitude",
        "source_url",
        "method",
        "confidence",
        "verified",
    ]
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(coordinates)
    audit["coordinate_output"] = {"filename": args.output.name, "sha256": digest(args.output)}
    write_json(args.audit_output, audit)
    print(json.dumps(audit["counts"], indent=2))


if __name__ == "__main__":
    main()
