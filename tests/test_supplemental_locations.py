"""Counterexamples for supplemental identity, provenance and conflict handling.

All fixtures are synthetic. These tests make no network requests and include no
assessment prices or real store-locator records.
"""

import hashlib
import importlib
import json
from copy import deepcopy
from pathlib import Path

import pytest
from shapely.geometry import box


@pytest.fixture
def supplemental(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("match_atp_locations")


def snapshot(spider="kwik_trip"):
    return {
        "spider": spider,
        "filename": f"{spider}.geojson",
        "url": f"https://alltheplaces-data.openaddresses.io/runs/2026-11-14/output/{spider}.geojson",
        "sha256": "a" * 64,
        "bytes": 1,
    }


def feature(spider="kwik_trip", **properties):
    return {
        "type": "Feature",
        "id": "synthetic-feature-id",
        "properties": {
            "ref": "17",
            "brand": "Kwik Trip",
            "name": "Kwik Trip",
            "amenity": "fuel",
            "addr:city": "Test Town",
            "addr:state": "WI",
            "addr:country": "US",
            "@source_uri": "https://example.org/locator/17",
            "@spider": spider,
            **properties,
        },
        "geometry": {"type": "Point", "coordinates": [-90.5, 44.0]},
    }


def collection(*features):
    return {"type": "FeatureCollection", "features": list(features)}


def station(opis_id="1", **fields):
    return {
        "opis_id": opis_id,
        "name": "KWIK TRIP #17",
        "aliases": ["KWIK TRIP #17"],
        "city": "Test Town",
        "state": "WI",
        **fields,
    }


def match(supplemental, stations, records, existing=None, cities=None, states=None):
    return supplemental.match_supplemental(
        stations,
        records,
        cities or {"places": []},
        states or {"WI": box(-91, 43, -90, 45)},
        existing,
    )


def test_cms_ref_is_not_confused_with_rutters_store_number(supplemental):
    data = feature("rutters_us", ref="999", name="Rutter’s #17", brand="Rutter's")
    records, _ = supplemental.parse_features(collection(data), snapshot("rutters_us"))
    assert records[0]["store_number"] == "17"
    assert records[0]["raw_ref"] == "999"
    matches, _ = match(supplemental, [station(aliases=["RUTTERS #999"])], records)
    assert matches == []
    matches, _ = match(supplemental, [station(aliases=["RUTTERS #17"])], records)
    assert len(matches) == 1


def test_arbitrary_yext_or_wordpress_refs_are_not_enabled(supplemental):
    for spider in ["quiktrip_us", "allsups_yesway_us", "race_trac_us"]:
        with pytest.raises(ValueError, match="Unsupported"):
            supplemental.parse_features(collection(feature(spider)), snapshot(spider))


def test_seven_eleven_brand_requires_same_id_city_and_point(supplemental):
    fuel = feature("seven_eleven_ca_us", brand=None)
    shop = feature("seven_eleven_ca_us", ref="17_SHOP", brand="7-Eleven", amenity=None)
    records, _ = supplemental.parse_features(collection(fuel, shop), snapshot("seven_eleven_ca_us"))
    assert len(records) == 1
    assert records[0]["chain"] == "7_eleven"
    assert records[0]["brand_evidence"] == shop
    for change in ["point", "city", "id", "duplicate"]:
        wrong = deepcopy(shop)
        if change == "point":
            wrong["geometry"]["coordinates"][0] += 0.01
        elif change == "city":
            wrong["properties"]["addr:city"] = "Other Town"
        elif change == "id":
            wrong["properties"]["ref"] = "18_SHOP"
        payload = (
            collection(fuel, wrong, shop) if change == "duplicate" else collection(fuel, wrong)
        )
        records, _ = supplemental.parse_features(payload, snapshot("seven_eleven_ca_us"))
        assert records == []


def test_conflicting_external_points_are_not_first_match_wins(supplemental):
    first, second = feature(), feature()
    second["geometry"]["coordinates"][0] += 0.001
    records, _ = supplemental.parse_features(collection(first, second), snapshot())
    matches, audit = match(supplemental, [station()], records)
    assert matches == []
    assert audit["unmatched"][0]["candidate_groups"] == 2


def test_identical_external_records_preserve_both_provenances(supplemental):
    first, second = feature(), feature()
    second["id"] = "another-collector-id"
    records, _ = supplemental.parse_features(collection(first, second), snapshot())
    matches, _ = match(supplemental, [station()], records)
    assert len(matches) == 1
    assert len(matches[0]["external_evidence"]) == 2


def test_conflicting_csv_aliases_cannot_fall_back_to_independent_name(supplemental):
    data = feature("roadys_us", name="KWIK TRIP #17", brand="Roady's")
    records, _ = supplemental.parse_features(collection(data), snapshot("roadys_us"))
    matches, audit = match(
        supplemental, [station(aliases=["KWIK TRIP #17", "KWIK TRIP #18"])], records
    )
    assert matches == []
    assert audit["unmatched"][0]["reason"] == "conflicting_source_alias_ids"


def test_many_opis_to_one_site_rejected_even_if_one_id_already_has_coordinates(supplemental):
    records, _ = supplemental.parse_features(collection(feature()), snapshot())
    matches, audit = match(supplemental, [station("1"), station("2")], records, [{"opis_id": "1"}])
    assert matches == []
    assert len(audit["unmatched"]) == 2
    assert all(row["all_opis_ids"] == ["1", "2"] for row in audit["unmatched"])


def test_existing_coordinate_is_never_replaced(supplemental):
    records, _ = supplemental.parse_features(collection(feature()), snapshot())
    existing = [{"opis_id": "1", "latitude": 44.001, "longitude": -90.501}]
    before = deepcopy(existing)
    matches, _ = match(supplemental, [station()], records, existing)
    assert matches == []
    assert existing == before


@pytest.mark.parametrize("field,value", [("city", "Other Town"), ("state", "MI")])
def test_same_store_number_cannot_override_city_or_state(supplemental, field, value):
    records, _ = supplemental.parse_features(collection(feature()), snapshot())
    matches, _ = match(supplemental, [station(**{field: value})], records)
    assert matches == []


def test_point_must_be_in_state_and_near_matching_census_city(supplemental):
    records, _ = supplemental.parse_features(collection(feature()), snapshot())
    matches, audit = match(supplemental, [station()], records, states={"WI": box(-92, 43, -91, 45)})
    assert matches == []
    assert audit["unmatched"][0]["reason"] == "outside_source_state_polygon"
    cities = {
        "places": [
            {
                "aliases": ["Test Town"],
                "state": "WI",
                "geoid": "1",
                "latitude": 45,
                "longitude": -90.5,
            }
        ]
    }
    matches, audit = match(supplemental, [station()], records, cities=cities)
    assert matches == []
    assert audit["unmatched"][0]["reason"] == "outside_city_plausibility_threshold"


def test_locator_url_and_spider_provenance_are_required(supplemental):
    for change in [
        {"@source_uri": ""},
        {"@source_uri": "http://example.org"},
        {"@spider": "other"},
    ]:
        records, skipped = supplemental.parse_features(collection(feature(**change)), snapshot())
        assert records == []
        assert skipped["missing_or_conflicting_locator_provenance"] == 1


def test_snapshot_hash_checked_even_when_file_size_matches(supplemental, tmp_path):
    raw = json.dumps(collection(feature())).encode()
    source = {**snapshot(), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}
    path = tmp_path / source["filename"]
    path.write_bytes(raw)
    records, _ = supplemental.load_records(tmp_path, {"snapshots": [source]})
    assert len(records) == 1
    # Equal-sized corruption would evade a size-only provenance check.
    path.write_bytes(raw.replace(b"Test Town", b"Fake Town"))
    with pytest.raises(ValueError, match="hash mismatch"):
        supplemental.load_records(tmp_path, {"snapshots": [source]})


@pytest.mark.parametrize(
    "change",
    [
        {"filename": "../outside.geojson"},
        {"url": "https://alltheplaces-data.openaddresses.io/runs/latest/output/x.geojson"},
        {"url": "https://unapproved.example/source"},
        {"bytes": 21 * 1024 * 1024},
        {"sha256": "missing"},
    ],
)
def test_manifest_rejects_unpinned_or_unbounded_sources(supplemental, tmp_path, change):
    path = tmp_path / "manifest.json"
    path.write_text(json.dumps({"snapshots": [{**snapshot(), **change}]}))
    with pytest.raises(ValueError):
        supplemental.read_manifest(path)


def test_checked_in_manifest_is_bounded_pinned_and_has_no_station_data(supplemental):
    manifest = supplemental.read_manifest()
    assert len(manifest["snapshots"]) == 10
    assert sum(s["bytes"] for s in manifest["snapshots"]) < 25 * 1024 * 1024
    assert all("coordinates" not in source for source in manifest["snapshots"])
