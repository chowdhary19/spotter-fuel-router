"""Data import checks use synthetic fixtures, never source-directory network calls."""

import csv
import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest


@pytest.fixture
def catalog(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("build_catalog")


@pytest.fixture
def matcher(catalog):
    return importlib.import_module("match_chain_locations")


@pytest.fixture
def example():
    source = {
        "opis_id": "1",
        "name": "PILOT #17",
        "aliases": ["PILOT #17"],
        "address": "I-1 EXIT 2",
        "city": "St. Louis",
        "state": "MO",
    }
    official = {
        "chain": "pilot",
        "store_number": "17",
        "name": "Pilot Travel Center #17",
        "address": "12 Example Road",
        "city": "Saint Louis",
        "state": "MO",
        "latitude": 38.6,
        "longitude": -90.2,
        "source_url": "https://pilotcompany.com/locations/directory",
        "coordinate_type": "Diesel Routing",
        "availability_evidence": "Open",
    }
    cities = {
        "places": [
            {
                "aliases": ["Saint Louis"],
                "state": "MO",
                "geoid": "999",
                "latitude": 38.61,
                "longitude": -90.21,
            }
        ]
    }
    return source, official, cities


def test_price_conflicts_keep_decimal_precision_and_original_rows(catalog, tmp_path):
    source = tmp_path / "prices.csv"
    with source.open("w", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(catalog.SOURCE_FIELDS)
        writer.writerows(
            [
                ["1", " Station A ", " I-1 EXIT 2 ", " Town ", "TX", "5", "3.10000001"],
                ["1", "Alias A", "I-1 EXIT 2", "Town", "TX", "5", "3.20000002"],
                ["2", "Canada", "Road", "Town", "ON", "7", "4.500"],
            ]
        )
    rows, metadata = catalog.parse_prices(source)
    assert len(rows) == 1
    assert rows[0]["price_usd_per_gallon"] == "3.20000002"
    assert rows[0]["source_rows"] == [2, 3]
    assert set(rows[0]["aliases"]) == {"Station A", "Alias A"}
    assert rows[0]["price_conflict"] is True
    assert metadata["counts"]["excluded_non_us_rows"] == 1


def test_station_point_is_never_replaced_by_city_reference(matcher, example):
    station, official, cities = example
    matches, _ = matcher.match_stations([station], [official], cities)
    assert len(matches) == 1
    assert matches[0]["latitude"] == official["latitude"]
    assert matches[0]["latitude"] != cities["places"][0]["latitude"]
    assert matches[0]["city_reference_geoid"] == "999"


@pytest.mark.parametrize(
    "field,value",
    [("chain", "loves"), ("state", "IL"), ("city", "Different Town"), ("store_number", "18")],
)
def test_same_number_cannot_override_chain_state_city_or_store(matcher, example, field, value):
    station, official, cities = example
    official[field] = value
    matches, audit = matcher.match_stations([station], [official], cities)
    assert matches == []
    assert audit["unmatched"][0]["reason"] == "no_unique_official_identity_state_city_match"


def test_geographic_mismatch_is_quarantined(matcher, example):
    station, official, cities = example
    official["latitude"] = 40
    matches, audit = matcher.match_stations([station], [official], cities)
    assert matches == []
    assert audit["unmatched"][0]["reason"] == "official_point_outside_city_plausibility_threshold"


def test_missing_city_reference_does_not_guess(matcher, example):
    station, official, _ = example
    matches, audit = matcher.match_stations([station], [official], {"places": []})
    assert matches == []
    assert audit["unmatched"][0]["reason"] == "no_census_place_for_geographic_crosscheck"


def test_two_opis_ids_for_one_site_are_both_quarantined(matcher, example):
    station, official, cities = example
    other = {**station, "opis_id": "2"}
    matches, audit = matcher.match_stations([station, other], [official], cities)
    assert matches == []
    assert len(audit["unmatched"]) == 2
    assert all(
        row["reason"] == "multiple_opis_ids_match_one_official_station"
        for row in audit["unmatched"]
    )


def test_conflicting_alias_store_numbers_are_not_silently_resolved(matcher, example):
    station, official, cities = example
    station["aliases"].append("PILOT #18")
    matches, audit = matcher.match_stations([station], [official], cities)
    assert matches == []
    assert audit["unmatched"][0]["reason"] == "conflicting_chain_or_store_numbers_in_aliases"


def test_ta_name_requires_an_exact_normalized_match(matcher, example):
    station, official, cities = example
    station["aliases"] = ["TA SAINT LOUIS TRAVEL CENTER"]
    official.update(chain="ta", name="TA Saint Louis", store_number="10")
    matches, _ = matcher.match_stations([station], [official], cities)
    assert len(matches) == 1
    official["name"] = "TA Saint Louis West"
    matches, _ = matcher.match_stations([station], [official], cities)
    assert matches == []


def test_pilot_requires_open_status_and_non_geofence_diesel_point(matcher, tmp_path):
    site = {
        "locationId": 17,
        "country": "United States",
        "hasDieselLanes": True,
        "status": "Open",
        "address1": "12 Example Road",
        "city": "Saint Louis",
        "state": "MO",
        "linesOfBusiness": [
            {
                "isPrimaryLineOfBusiness": True,
                "dieselBrandDescription": "Pilot",
                "status": "Open",
                "isPublicFacing": True,
                "lobPublicDescription": "Pilot #17",
            }
        ],
        "geoCodes": [
            {"geocodeType": "Geo Fencing", "sequence": 0, "latitude": 40, "longitude": -90},
            {"geocodeType": "Diesel Routing", "sequence": 0, "latitude": 38.6, "longitude": -90.2},
        ],
    }
    path = tmp_path / "pilot.json"
    path.write_text(json.dumps([site]))
    assert matcher.pilot_records(path)[0]["latitude"] == 38.6
    site["status"] = "Temporary Closed"
    path.write_text(json.dumps([site]))
    assert matcher.pilot_records(path) == []
    site["status"] = "Open"
    site["geoCodes"] = [site["geoCodes"][0]]
    path.write_text(json.dumps([site]))
    assert matcher.pilot_records(path) == []


@pytest.mark.parametrize(
    "change",
    [{"method": "city_centroid"}, {"verified": False}, {"confidence": "low"}, {"latitude": True}],
)
def test_invalid_coordinate_evidence_is_rejected(catalog, tmp_path, change):
    record = {
        "opis_id": "1",
        "latitude": 38.6,
        "longitude": -90.2,
        "source_url": "https://example.org/station/17",
        "method": "official_store_match",
        "verified": True,
        "confidence": "high",
        **change,
    }
    path = tmp_path / "coordinates.json"
    path.write_text(json.dumps([record]))
    accepted, rejected = catalog.load_coordinates(path, {"1"})
    assert accepted == {}
    assert len(rejected) == 1


def test_conflicting_coordinate_points_do_not_choose_first(catalog, tmp_path):
    record = {
        "opis_id": "1",
        "latitude": 38.6,
        "longitude": -90.2,
        "source_url": "https://example.org/station/17",
        "method": "official_store_match",
        "verified": True,
        "confidence": "high",
    }
    other = deepcopy(record)
    other["latitude"] = 39.6
    path = tmp_path / "coordinates.json"
    path.write_text(json.dumps([record, other]))
    accepted, rejected = catalog.load_coordinates(path, {"1"})
    assert accepted == {}
    assert rejected


def test_pilot_extractor_handles_quoted_brackets_without_javascript(catalog):
    pytest.importorskip("pyjson5")
    prepare = importlib.import_module("prepare_data")
    html = "prefix locations:[{siteSystemId:1,locationId:17,name:'[a] \\\"b\\\"'}] suffix"
    assert prepare.extract_pilot_locations(html)[0]["locationId"] == 17
    with pytest.raises(ValueError, match="Unterminated"):
        prepare.extract_pilot_locations("locations:[{siteSystemId:1,locationId:17")
