#!/usr/bin/env python3
"""Build private local catalog inputs from the assessment CSV and official directories.

Run: uv run --group data python scripts/prepare_data.py --fuel-csv path/to/fuel.csv
Use --offline --source-dir path/to/snapshots to reproduce a saved source snapshot.
Each download is bounded, TLS-verified, and performed once, without retries.
Downloaded source data and derived station catalogs are not repository artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pyjson5
import requests
from build_catalog import build_catalog, digest, parse_prices, write_json
from match_chain_locations import (
    LOVES_URL,
    PILOT_URL,
    TA_URL,
    loves_records,
    match_stations,
    pilot_records,
    ta_records,
)

MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024
MAX_DOWNLOAD_SECONDS = 60


def download(url: str, destination: Path) -> dict:
    """Download one public directory with a byte cap and finite socket/deadline limits."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".download")
    started = time.monotonic()
    count = 0
    try:
        with requests.Session() as session:
            session.max_redirects = 3
            with session.get(
                url,
                stream=True,
                timeout=(5, 20),
                headers={"User-Agent": "SpotterAssessmentDataPrep/1.0"},
            ) as response:
                response.raise_for_status()
                if not response.url.startswith("https://"):
                    raise ValueError("Source redirected to a non-HTTPS URL")
                with temporary.open("wb") as output:
                    for chunk in response.iter_content(chunk_size=64 * 1024):
                        count += len(chunk)
                        if count > MAX_DOWNLOAD_BYTES:
                            raise ValueError("Source exceeds the 20 MiB download limit")
                        if time.monotonic() - started > MAX_DOWNLOAD_SECONDS:
                            raise TimeoutError("Source download exceeded its deadline")
                        output.write(chunk)
        if not count:
            raise ValueError("Source download was empty")
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
    return {
        "url": url,
        "filename": destination.name,
        "downloaded_at_utc": datetime.now(UTC).isoformat(),
        "sha256": digest(destination),
        "bytes": count,
    }


def extract_pilot_locations(html: str) -> list[dict]:
    """Parse the observed JSON5 array; never evaluate downloaded JavaScript."""
    marker = "locations:[{siteSystemId:"
    try:
        start = html.index(marker) + len("locations:")
    except ValueError as exc:
        raise ValueError("Pilot page schema changed: locations marker missing") from exc
    depth = 0
    quote = None
    escaped = False
    for position in range(start, len(html)):
        character = html[position]
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in ("'", '"'):
            quote = character
        elif character in "[{":
            depth += 1
        elif character in "]}":
            depth -= 1
            if depth == 0:
                result = pyjson5.decode(html[start : position + 1])
                if not isinstance(result, list) or not result:
                    raise ValueError("Pilot locations array is empty or invalid")
                if not all(isinstance(row, dict) and "locationId" in row for row in result):
                    raise ValueError("Pilot location records changed schema")
                return result
    raise ValueError("Unterminated embedded Pilot locations array")


def prepare(fuel_csv: Path, cities_file: Path, source_dir: Path, output_dir: Path, offline: bool):
    stations, price_metadata = parse_prices(fuel_csv)
    cities = json.loads(cities_file.read_text())
    if not isinstance(cities.get("places"), list) or not cities["places"]:
        raise ValueError("Census place lookup is empty or invalid")
    source_dir.mkdir(parents=True, exist_ok=True)
    sources = {
        "pilot": (PILOT_URL, source_dir / "pilot-directory.html"),
        "loves": (LOVES_URL, source_dir / "loves-locations.json"),
        "ta": (TA_URL, source_dir / "ta-locations.xlsx"),
    }
    provenance = {}
    for key, (url, path) in sources.items():
        if offline:
            if not path.is_file() or path.stat().st_size > MAX_DOWNLOAD_BYTES:
                raise ValueError(f"Missing or oversized offline source: {path.name}")
            provenance[key] = {
                "url": url,
                "filename": path.name,
                "sha256": digest(path),
                "bytes": path.stat().st_size,
                "mode": "supplied offline snapshot; download date not inferred",
            }
        else:
            provenance[key] = download(url, path)
    pilot_parsed = source_dir / "pilot-locations.json"
    pilot_parsed.write_text(
        json.dumps(extract_pilot_locations(sources["pilot"][1].read_text()), separators=(",", ":")),
        encoding="utf-8",
    )
    provenance["pilot"]["parsed_sha256"] = digest(pilot_parsed)
    write_json(source_dir / "source_manifest.json", provenance)
    official = (
        pilot_records(pilot_parsed)
        + loves_records(sources["loves"][1])
        + ta_records(sources["ta"][1])
    )
    if not official:
        raise ValueError("No usable official locations were parsed")
    coordinates, audit = match_stations(stations, official, cities)
    if not coordinates:
        raise ValueError("No stations matched; source schema or coverage needs review")
    output_dir.mkdir(parents=True, exist_ok=True)
    coordinate_file = output_dir / "station_coordinates.csv"
    with coordinate_file.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "opis_id",
                "latitude",
                "longitude",
                "source_url",
                "method",
                "confidence",
                "verified",
            ],
            extrasaction="ignore",
        )
        writer.writeheader()
        writer.writerows(coordinates)
    audit["sources"] = {
        "fuel_csv": {"filename": fuel_csv.name, "sha256": price_metadata["source_sha256"]},
        "cities": {"sha256": digest(cities_file), "underlying_source": cities["metadata"]},
        **provenance,
    }
    audit["coordinate_output"] = {
        "filename": coordinate_file.name,
        "sha256": digest(coordinate_file),
    }
    catalog, price_audit = build_catalog(fuel_csv, coordinate_file)
    write_json(output_dir / "coordinate_match_audit.json", audit)
    write_json(output_dir / "stations.json", catalog)
    write_json(output_dir / "price_audit.json", price_audit)
    return {"catalog": catalog["metadata"]["counts"], "matching": audit["counts"]}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fuel-csv", type=Path, required=True)
    parser.add_argument("--cities", type=Path, default=Path("data/cities.json"))
    parser.add_argument("--source-dir", type=Path, default=Path(".data-source-cache"))
    parser.add_argument("--output-dir", type=Path, default=Path("data"))
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    try:
        result = prepare(args.fuel_csv, args.cities, args.source_dir, args.output_dir, args.offline)
    except (ValueError, KeyError, OSError, requests.RequestException) as exc:
        parser.error(str(exc))
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
