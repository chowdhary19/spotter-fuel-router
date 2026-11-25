"""Candidate discovery preserves options; road reachability belongs to routing."""

from copy import deepcopy
from decimal import Decimal
from random import Random

import pytest

from fuel.geo import candidates_on_route
from fuel.routing_client import METERS_PER_MILE, RoadRoute
from fuel.station_candidates import select_station_candidates


def route(coordinates=((-100, 30), (-100, 44)), miles=1000):
    distance = Decimal(str(miles)) * METERS_PER_MILE
    return RoadRoute(tuple(coordinates), distance, 1.0, (distance,), tuple(coordinates))


def station(identifier, longitude, latitude, price="4.00012345"):
    return {
        "opis_id": identifier,
        "longitude": longitude,
        "latitude": latitude,
        "price_usd_per_gallon": price,
        "coordinate_source": "synthetic test fixture",
    }


def identifiers(selected):
    return {item["opis_id"] for item in selected}


def coverage_pool():
    """Four distinct route regions, each with separate access and price options."""
    stations = []
    for index, latitude in enumerate((28, 34, 40, 46)):
        stations.extend(
            [
                station(f"near-{index}", -100, latitude, "5.0"),
                station(f"cheap-{index}", -99.95, latitude, "2.0"),
                *[
                    station(f"filler-{index}-{n}", -99.9, latitude + n * 0.01, "6.0")
                    for n in range(8)
                ],
            ]
        )
    return stations


def test_uncapped_pool_preserves_prices_provenance_and_does_not_mutate_input():
    stations = [station("north", -100, 40), station("south", -100, 32)]
    original = deepcopy(stations)

    selected, metadata = select_station_candidates(route(), stations)

    assert [item["opis_id"] for item in selected] == ["south", "north"]
    assert stations == original
    assert all(item["price_usd_per_gallon"] == "4.00012345" for item in selected)
    assert all(item["coordinate_source"] == "synthetic test fixture" for item in selected)
    assert metadata["corridor_station_count"] == metadata["matrix_station_count"] == 2
    assert metadata["subset_was_capped"] is False


def test_geographic_corridor_follows_full_bent_geometry_not_endpoint_chord_or_box():
    bent = route(((-110, 35), (-110, 40), (-100, 40)))
    stations = [
        station("at-bend", -110, 40),
        station("near-first-leg", -109.99, 37),
        station("inside-box-but-far-from-road", -105, 37),
        station("outside-corridor", -109, 37),
    ]

    selected = candidates_on_route(bent, stations, corridor_miles=2)

    assert identifiers(selected) == {"at-bend", "near-first-leg"}
    assert all(item["straight_line_offset_miles"] <= 2 for item in selected)
    assert all(0 <= item["progress_miles"] <= bent.miles for item in selected)


def test_projection_keeps_near_endpoints_and_offset_options_without_claiming_access():
    stations = [
        station("before-start", -100, 29.99),
        station("after-finish", -100, 44.01),
        station("across-an-unknown-road-barrier", -99.9, 37),
    ]

    selected = candidates_on_route(route(), stations, corridor_miles=10)
    by_id = {item["opis_id"]: item for item in selected}

    assert identifiers(selected) == identifiers(stations)
    assert by_id["before-start"]["progress_miles"] == 0
    assert by_id["after-finish"]["progress_miles"] == 1000
    # A nearby point may need a large detour or be unreachable. Discovery adds
    # only projection metadata, never a road leg, fuel decision, or access flag.
    for candidate in selected:
        original = next(s for s in stations if s["opis_id"] == candidate["opis_id"])
        assert set(candidate) - set(original) == {
            "progress_miles",
            "straight_line_offset_miles",
        }


def test_capped_pool_retains_each_geographic_region_and_its_price_alternative():
    long_route = route(((-100, 25), (-100, 49)), miles=2400)

    selected, metadata = select_station_candidates(long_route, coverage_pool(), max_stations=12)

    assert {f"{kind}-{index}" for kind in ("near", "cheap") for index in range(4)} <= (
        identifiers(selected)
    )
    assert len(selected) == len(identifiers(selected)) == 12
    assert metadata["corridor_station_count"] == 40
    assert metadata["matrix_station_count"] == metadata["station_limit"] == 12
    assert metadata["subset_was_capped"] is True


def test_four_slot_limit_cannot_crowd_out_finish_access_with_start_alternatives():
    stations = [
        station("middle-near", -100, 37, "5"),
        station("middle-cheap", -99.99, 37, "1"),
        station("start-a", -99.99, 30, "4"),
        station("start-b", -99.99, 30.01, "4"),
        station("start-c", -99.99, 30.02, "4"),
        station("finish", -99.99, 44, "4"),
    ]

    selected, _ = select_station_candidates(route(), stations, max_stations=4)

    assert identifiers(selected) == {"start-a", "finish", "middle-near", "middle-cheap"}


def test_default_cap_includes_multiple_endpoint_access_alternatives():
    stations = [
        *[station(f"start-{n}", -99.99, 30 + n * 0.01) for n in range(3)],
        *[station(f"finish-{n}", -99.99, 44 - n * 0.01) for n in range(3)],
        *[station(f"middle-{n}", -100, 34 + n * 0.05, "2") for n in range(90)],
    ]

    selected, metadata = select_station_candidates(route(), stations)

    assert len(selected) == 62
    assert {f"{end}-{n}" for end in ("start", "finish") for n in range(3)} <= identifiers(selected)
    assert metadata["subset_was_capped"] is True


def test_selection_and_report_order_are_independent_of_input_order_even_with_ties():
    stations = coverage_pool() + [
        station("tie-a", -100, 37, "1"),
        station("tie-z", -100, 37, "1"),
    ]
    long_route = route(((-100, 25), (-100, 49)), miles=2400)
    expected = select_station_candidates(long_route, stations, max_stations=12)
    random = Random(8137)

    for _ in range(8):
        random.shuffle(stations)
        assert select_station_candidates(long_route, stations, max_stations=12) == expected

    selected, _ = expected
    assert selected == sorted(selected, key=lambda s: (s["progress_miles"], s["opis_id"]))


def test_full_provider_budget_reserves_two_waypoints_for_trip_endpoints():
    stations = [station(f"station-{n:03}", -100, 30 + n * 0.1) for n in range(120)]

    selected, metadata = select_station_candidates(route(), stations, max_stations=98)

    assert len(selected) + 2 == 100
    assert metadata["station_limit"] == 98


@pytest.mark.parametrize("limit", [0, 3, 99, 100, 1000, 4.5, True, None])
def test_invalid_station_caps_are_rejected_before_geometry_work(limit):
    with pytest.raises(ValueError, match="4 to 98"):
        select_station_candidates(None, [], max_stations=limit)


@pytest.mark.parametrize("reported_miles", [0, 1])
def test_zero_length_or_degenerate_path_keeps_local_refueling_options(reported_miles):
    trip = route(((-100, 30), (-100, 30)), miles=reported_miles)
    stations = [
        station("at-endpoint", -100, 30),
        station("nearby-needs-road-verification", -99.99, 30),
        station("too-far", -100, 31),
    ]

    selected = candidates_on_route(trip, stations, corridor_miles=2)

    assert identifiers(selected) == {"at-endpoint", "nearby-needs-road-verification"}
    assert all(item["progress_miles"] == 0 for item in selected)
    by_id = {item["opis_id"]: item for item in selected}
    assert by_id["at-endpoint"]["straight_line_offset_miles"] == 0
    assert 0 < by_id["nearby-needs-road-verification"]["straight_line_offset_miles"] < 2


def test_zero_distance_candidate_selection_stays_bounded_and_retains_price_choices():
    trip = route(((-100, 30), (-100, 30)), miles=0)
    stations = [
        station("nearest", -100, 30, "5"),
        station("cheapest", -99.99, 30, "1"),
        *[station(f"other-{n}", -100, 30 + 0.01 * n, "4") for n in range(1, 10)],
    ]

    selected, metadata = select_station_candidates(trip, stations, max_stations=4)

    assert len(selected) == 4
    assert {"nearest", "cheapest"} <= identifiers(selected)
    assert metadata["subset_was_capped"] is True
    assert (selected, metadata) == select_station_candidates(trip, stations[::-1], max_stations=4)


def test_empty_and_no_nearby_station_cases_remain_empty():
    for trip, stations in (
        (route(), []),
        (route(((-100, 30), (-100, 30)), miles=0), []),
        (route(), [station("far", -110, 35)]),
    ):
        selected, metadata = select_station_candidates(trip, stations)
        assert selected == []
        assert metadata["corridor_station_count"] == metadata["matrix_station_count"] == 0
        assert metadata["subset_was_capped"] is False
