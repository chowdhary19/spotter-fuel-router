"""200 deterministic HTTP scenarios, with synthetic roads and no external requests.

Only the public routing transport and private station catalog are replaced.
Django HTTP parsing, bundled city/boundary lookup, candidate geometry, OSRM JSON
validation, graph search, final-road fuel verification, serialization and cached
map rendering execute normally. These are integration simulations, not 200 live
US road trips. Every station and road in this file is explicitly synthetic.

Set SPOTTER_E2E_REPORT to a local JSON path to retain per-case outcomes, including
failed counterexamples. No report or employer data is uploaded by this suite.
"""

from __future__ import annotations

import hashlib
import heapq
import io
import json
import os
import random
import threading
import time
from collections import Counter
from copy import deepcopy
from decimal import Decimal as D
from pathlib import Path
from urllib.parse import urlsplit

import pytest
import requests
from django.core.cache import cache
from django.test import Client

from fuel.location_catalog import resolve_location
from fuel.routing_client import METERS_PER_MILE

CITY_PAIRS = [
    ("Chicago, IL", "Nashville, TN"),
    ("Dallas, TX", "Houston, TX"),
    ("Boise, ID", "Salt Lake City, UT"),
    ("Macon, GA", "Atlanta, GA"),
    ("Pecos, TX", "El Paso, TX"),
    ("La Salle, IL", "Utica, IL"),
]


def roads(size, edges):
    result = [[None] * size for _ in range(size)]
    for i in range(size):
        result[i][i] = 0
    for (a, b), value in edges.items():
        result[a][b] = value
    return result


def scenario(name, matrix, prices, *, initial=50, reserve=0, **extra):
    return dict(
        name=name,
        category="functional",
        matrix=matrix,
        prices=prices,
        initial=initial,
        reserve=reserve,
        **extra,
    )


def exhaustive_cost(matrix, prices, initial, reserve):
    """Independent exact oracle for integer gallon-consumption road segments.

    It expands buy-one-gallon and traverse-one-original-edge actions directly.
    It neither closes the matrix nor uses the production refill exchange rule.
    Integer edge consumption, inventory and reserve ensure an integer optimum
    exists even though the production optimizer permits continuous purchases.
    """
    consumption = [[None if d is None else D(str(d)) / 10 for d in row] for row in matrix]
    assert all(v is None or v == int(v) for row in consumption for v in row)
    cents = [None, *[int(D(str(p)) * 100) for p in prices], None]
    finish = len(matrix) - 1
    best = {(0, initial): 0}
    queue = [(0, 0, initial)]
    while queue:
        paid, node, fuel = heapq.heappop(queue)
        if best.get((node, fuel)) != paid:
            continue
        if node == finish and fuel >= reserve:
            return D(paid) / 100
        actions = []
        if cents[node] is not None and fuel < 50:
            actions.append((node, fuel + 1, paid + cents[node]))
        for target, required in enumerate(consumption[node]):
            if target != node and required is not None and required <= fuel:
                actions.append((target, fuel - int(required), paid))
        for target, remaining, cost in actions:
            if cost < best.get((target, remaining), float("inf")):
                best[target, remaining] = cost
                heapq.heappush(queue, (cost, target, remaining))
    return None


def generated_graphs():
    cases = []
    for seed in range(120):
        rng = random.Random(884900 + seed)
        size = rng.randint(3, 7)
        matrix = roads(size, {})
        for i in range(size):
            for j in range(size):
                if i != j and rng.random() < 0.62:
                    matrix[i][j] = rng.randint(0, 65) * 10
        # A base route exists, even if its fuel requirements cannot be covered.
        matrix[0][-1] = rng.randint(51, 90) * 10
        prices = [str(D(rng.randint(175, 650)) / 100) for _ in range(size - 2)]
        initial = rng.choice([0, 1, 5, 10, 25, 49, 50])
        reserve = rng.choice([0, 1, 5, 10, 25, 50])
        if seed < 80:
            # Guarantee an independently obvious feasible corridor in most
            # cases, while retaining random directed alternatives and cycles.
            # The last 40 graphs deliberately retain unconstrained coverage.
            initial = rng.choice([1, 5, 10, 25, 49, 50])
            reserve = rng.choice([0, 1, 5, 10, 25])
            matrix[0][1] = rng.randint(1, initial) * 10
            matrix[1][-1] = rng.randint(1, 50 - reserve) * 10
        cases.append(
            scenario(
                f"graph-{seed:03d}",
                matrix,
                prices,
                initial=initial,
                reserve=reserve,
                seed=seed,
                oracle=True,
                pair=seed % len(CITY_PAIRS),
            )
        )
    return cases


def special_cases():
    cases = [
        scenario(
            "cheap-pump-costly-detour",
            roads(4, {(0, 3): 600, (0, 1): 450, (1, 3): 250, (0, 2): 400, (2, 3): 200}),
            [3, 3.1],
            oracle=True,
        ),
        scenario(
            "exact-range-to-pump",
            roads(3, {(0, 2): 600, (0, 1): 500, (1, 2): 100}),
            [3],
            oracle=True,
        ),
        scenario(
            "just-over-range-to-pump",
            roads(3, {(0, 2): 600, (0, 1): "500.000001", (1, 2): 100}),
            [3],
            expected_error="insufficient_verified_stations",
        ),
        scenario(
            "empty-tank-zero-distance-pump",
            roads(3, {(0, 2): 100, (0, 1): 0, (1, 2): 100}),
            [3],
            initial=0,
            oracle=True,
        ),
        scenario(
            "empty-tank-nonzero-pump",
            roads(3, {(0, 2): 100, (0, 1): ".000001", (1, 2): 100}),
            [3],
            initial=0,
            expected_error="insufficient_verified_stations",
        ),
        scenario(
            "destination-reserve",
            roads(3, {(0, 2): 600, (0, 1): 400, (1, 2): 200}),
            [3.47],
            reserve=5,
            oracle=True,
        ),
        scenario(
            "full-reserve-unreachable",
            roads(3, {(0, 2): 600, (0, 1): 400, (1, 2): 200}),
            [3],
            reserve=50,
            oracle=True,
        ),
        scenario(
            "transit-without-purchase",
            roads(4, {(0, 3): 700, (0, 1): 100, (1, 2): 300, (0, 2): 550, (2, 3): 200}),
            [10, 3],
            oracle=True,
        ),
        scenario(
            "profitable-revisit",
            roads(4, {(0, 3): 700, (0, 1): 100, (1, 2): 100, (2, 1): 100, (1, 3): 300}),
            [10, 1],
            initial=10,
            oracle=True,
        ),
        scenario(
            "destination-as-transit",
            roads(3, {(0, 2): 450, (2, 1): 20, (1, 2): 20}),
            [3],
            reserve=10,
            oracle=True,
        ),
        scenario(
            "zero-edge-cycle",
            roads(4, {(0, 3): 600, (0, 1): 0, (1, 2): 0, (2, 1): 0, (2, 3): 500}),
            [4, 3],
            initial=0,
            oracle=True,
        ),
        scenario(
            "same-price-alternatives",
            roads(4, {(0, 3): 600, (0, 1): 400, (1, 3): 200, (0, 2): 450, (2, 3): 150}),
            [3, 3],
            oracle=True,
        ),
        scenario(
            "inaccessible-cheapest-pump",
            roads(4, {(0, 3): 600, (0, 1): 400, (1, 3): 200, (0, 2): 410, (2, 3): 190}),
            [1, 4],
            snap_node=1,
            snap_latitude_delta=0.003,
            expected_cost="40.00",
        ),
        scenario(
            "near-road-pump-retained",
            roads(3, {(0, 2): 600, (0, 1): 400, (1, 2): 200}),
            [3],
            snap_node=1,
            snap_latitude_delta=0.0002,
            expected_cost="30.00",
        ),
        scenario(
            "only-pump-off-road",
            roads(3, {(0, 2): 600, (0, 1): 400, (1, 2): 200}),
            [3],
            snap_node=1,
            snap_latitude_delta=0.003,
            expected_error="insufficient_verified_stations",
        ),
        scenario(
            "final-route-two-metre-change",
            roads(3, {(0, 2): 600, (0, 1): 400, (1, 2): 200}),
            [3],
            final_delta_meters=2,
            expected_error="route_changed_after_optimization",
        ),
        scenario(
            "short-trip-existing-inventory",
            roads(3, {(0, 2): 200, (0, 1): 100, (1, 2): 100}),
            [3],
            oracle=True,
        ),
        scenario(
            "exact-range-no-new-fuel",
            roads(3, {(0, 2): 500, (0, 1): 300, (1, 2): 200}),
            [3],
            oracle=True,
        ),
        scenario(
            "partial-initial-inventory",
            roads(3, {(0, 2): 400, (0, 1): 90, (1, 2): 310}),
            [3.29],
            initial=10,
            oracle=True,
        ),
        scenario(
            "range-preserving-metre-rounding",
            roads(4, {(0, 3): 600, (0, 1): 400, (1, 2): 100, (2, 3): 100}),
            [3.00733333, 3.00111111],
            expected_cost="30.01",
        ),
    ]
    assert len(cases) == 20
    return cases


FAULTS = [
    "rate_limit",
    "http_failure",
    "redirect",
    "timeout",
    "no_segment",
    "bad_json",
    "bad_schema",
    "too_large",
    "nonfinite",
    "snap_mismatch",
]
FAULT_EXPECTATIONS = {
    "rate_limit": (503, "routing_rate_limited"),
    "http_failure": (502, "routing_unavailable"),
    "redirect": (502, "routing_redirect_rejected"),
    "timeout": (502, "routing_unavailable"),
    "no_segment": (422, "no_road_route"),
    "bad_json": (502, "routing_unavailable"),
    "too_large": (502, "routing_response_too_large"),
}


def fault_cases():
    return [
        dict(name=f"{stage}-{fault}", category="provider", stage=stage, fault=fault)
        for stage in ("base", "table", "final")
        for fault in FAULTS
    ]


INVALID_INPUTS = [
    ("missing-start", {"finish": "Dallas, TX"}, "invalid_request"),
    ("null-object", None, "invalid_request"),
    ("array-object", [], "invalid_request"),
    ("unknown-field", {"finishh": "Chicago, IL"}, "unknown_fields"),
    ("invalid-city-format", {"start": "Chicago"}, "invalid_location"),
    ("unknown-city", {"start": "Unlisted city, TX"}, "unknown_city"),
    ("overseas-coordinate", {"start": {"latitude": 48.8566, "longitude": 2.3522}}, "outside_usa"),
    ("invalid-latitude", {"start": {"latitude": 91, "longitude": -100}}, "invalid_coordinates"),
    ("invalid-longitude", {"start": {"latitude": 40, "longitude": -181}}, "invalid_coordinates"),
    ("bool-coordinate", {"start": {"latitude": True, "longitude": -100}}, "invalid_coordinates"),
    ("string-coordinate", {"start": {"latitude": "40", "longitude": -100}}, "invalid_coordinates"),
    (
        "coordinate-extra-key",
        {"start": {"latitude": 40, "longitude": -100, "z": 1}},
        "invalid_location",
    ),
    ("negative-initial", {"initial_fuel_gallons": -1}, "invalid_fuel_quantity"),
    ("over-capacity", {"initial_fuel_gallons": 51}, "invalid_fuel_quantity"),
    ("negative-reserve", {"reserve_gallons": -1}, "invalid_fuel_quantity"),
    ("reserve-over-capacity", {"reserve_gallons": 51}, "invalid_fuel_quantity"),
    ("nan-fuel", {"initial_fuel_gallons": "NaN"}, "invalid_fuel_quantity"),
    ("boolean-fuel", {"reserve_gallons": True}, "invalid_fuel_quantity"),
    ("excess-fuel-precision", {"initial_fuel_gallons": "1.0000001"}, "invalid_fuel_quantity"),
    (
        "infinite-coordinate",
        {"start": {"latitude": float("inf"), "longitude": -100}},
        "invalid_coordinates",
    ),
]
LIFECYCLE = [
    "cache-hit",
    "evicted-map",
    "changed-catalog",
    "changed-inventory",
    "failed-plan-not-cached",
    "busy-cold",
    "warm-bypasses-busy",
    "expired-map",
    "concurrent-same-request",
    "concurrent-other-request",
]
ALL_CASES = [
    *generated_graphs(),
    *special_cases(),
    *fault_cases(),
    *[
        dict(name=name, category="invalid", patch=patch, error=error)
        for name, patch, error in INVALID_INPUTS
    ],
    *[dict(name=name, category="lifecycle") for name in LIFECYCLE],
]
assert len(ALL_CASES) == 200


class RawBody(io.BytesIO):
    def read1(self, size=-1, **kwargs):
        return self.read(size)


class SyntheticTransport:
    """Only the network edge is replaced; the OSRM parser still consumes bytes."""

    def __init__(self, case, points):
        self.case = case
        self.points = points
        self.requests = []
        self.responses = []
        self.arrived = None
        self.release = None

    def index(self, point):
        distances = [sum(abs(a - b) for a, b in zip(point, p, strict=True)) for p in self.points]
        index = min(range(len(distances)), key=distances.__getitem__)
        assert distances[index] < 0.000002, (point, self.points)
        return index

    def snap(self, index):
        point = self.points[index]
        if index == self.case.get("snap_node"):
            return [point[0], point[1] + self.case["snap_latitude_delta"]]
        return list(point)

    def __call__(self, url, **kwargs):
        assert kwargs["allow_redirects"] is False and kwargs["stream"] is True
        stage = "base" if not self.requests else "table" if "/table/" in url else "final"
        points = [
            tuple(map(float, p.split(","))) for p in urlsplit(url).path.rsplit("/", 1)[1].split(";")
        ]
        indices = [self.index(p) for p in points]
        self.requests.append(dict(stage=stage, indices=indices, params=kwargs["params"]))
        if self.arrived is not None and stage == "base":
            self.arrived.set()
            assert self.release.wait(5), "Test failed to release synthetic provider"
        matrix = self.case["matrix"]
        if stage == "table":
            payload = dict(
                code="Ok",
                distances=[
                    [
                        None
                        if matrix[i][j] is None
                        else str(D(str(matrix[i][j])) * METERS_PER_MILE)
                        for j in indices
                    ]
                    for i in indices
                ],
                sources=[dict(location=self.snap(i)) for i in indices],
                destinations=[dict(location=self.snap(i)) for i in indices],
            )
        else:
            legs = [
                D(str(matrix[a][b])) * METERS_PER_MILE
                for a, b in zip(indices[:-1], indices[1:], strict=True)
            ]
            if stage == "final":
                legs[0] += D(str(self.case.get("final_delta_meters", 0)))
            payload = dict(
                code="Ok",
                routes=[
                    dict(
                        distance=str(sum(legs)),
                        duration=1000,
                        geometry=dict(type="LineString", coordinates=points),
                        legs=[dict(distance=str(v)) for v in legs],
                    )
                ],
                waypoints=[
                    dict(location=self.snap(i) if stage == "final" else list(p))
                    for i, p in zip(indices, points, strict=True)
                ],
            )
        status = 200
        fault = self.case.get("fault") if stage == self.case.get("stage") else None
        if fault == "timeout":
            raise requests.ReadTimeout("synthetic timeout")
        if fault == "rate_limit":
            status = 429
        if fault == "http_failure":
            status = 503
        if fault == "redirect":
            status = 302
        if fault == "no_segment":
            status, payload = 400, dict(code="NoSegment")
        if fault == "bad_schema":
            payload = dict(code="Ok")
        if fault == "nonfinite":
            if stage == "table":
                payload["distances"][0][1] = "NaN"
            else:
                payload["routes"][0]["distance"] = "NaN"
        if fault == "snap_mismatch":
            if stage == "table":
                payload["destinations"][1]["location"][1] += 0.001
            else:
                payload["waypoints"][0]["location"][1] += 1
        response = requests.Response()
        response.status_code = status
        response.url = url
        body = b"{ broken json" if fault == "bad_json" else json.dumps(payload).encode()
        response.headers["Content-Length"] = str(
            9 * 1024 * 1024 if fault == "too_large" else len(body)
        )
        response.raw = RawBody(body)
        self.responses.append(response)
        return response


def install(monkeypatch, case):
    """Use actual bundled US lookup/geometry and synthetic station identities."""
    pair = CITY_PAIRS[case.get("pair", 0)]
    resolved = [
        resolve_location(value, field)
        for value, field in zip(pair, ["start", "finish"], strict=True)
    ]
    endpoints = [(p["longitude"], p["latitude"]) for p in resolved]
    size = len(case["matrix"])
    points = [endpoints[0]]
    stations = []
    for index, price in enumerate(case["prices"], start=1):
        fraction = index / (size - 1)
        point = tuple(round(a + (b - a) * fraction, 7) for a, b in zip(*endpoints, strict=True))
        points.append(point)
        stations.append(
            dict(
                opis_id=f"synthetic-{index}",
                name=f"Synthetic station {index}",
                address="Synthetic test road",
                city="Synthetic",
                state="US",
                longitude=point[0],
                latitude=point[1],
                price_usd_per_gallon=str(price),
                coordinate_source={
                    "method": "synthetic-fixture",
                    "url": "https://example.invalid/synthetic",
                },
            )
        )
    points.append(endpoints[1])
    catalog = dict(
        metadata={"source": "synthetic-end-to-end-scenario", "case": case["name"]},
        stations=stations,
    )
    monkeypatch.setattr("fuel.route_planner.station_catalog", lambda: catalog)
    monkeypatch.setattr("fuel.views.station_catalog", lambda: catalog)
    # No network pacing is needed for deterministic in-memory provider bytes.
    monkeypatch.setattr("fuel.routing_client.time.sleep", lambda seconds: None)
    transport = SyntheticTransport(case, points)
    monkeypatch.setattr("fuel.routing_client.requests.get", transport)
    body = dict(
        start=pair[0],
        finish=pair[1],
        initial_fuel_gallons=case.get("initial", 50),
        reserve_gallons=case.get("reserve", 0),
    )
    if case.get("seed", 0) % 3 == 1:
        body["start"] = {"longitude": endpoints[0][0], "latitude": endpoints[0][1]}
    return Client(), body, transport, catalog


def post(client, body):
    return client.post("/api/route/", data=json.dumps(body), content_type="application/json")


def assert_success(client, response, transport):
    assert response.status_code == 200, response.content
    body = response.json()
    assert body["geojson"]["type"] == "FeatureCollection"
    assert body["geojson"]["features"][0]["geometry"]["type"] == "LineString"
    assert len(transport.requests) <= 3
    assert body["performance"]["route_api_calls"] == len(transport.requests)
    assert D(body["tank_capacity_gallons"]) == 50 and D(body["mpg"]) == 10
    legs = body["road_leg_distances_miles"]
    assert abs(sum(map(D, legs)) - D(body["distance_miles"])) <= D(".001") * len(legs)
    stop_features = [
        feature
        for feature in body["geojson"]["features"]
        if feature["properties"]["kind"] == "fuel_stop"
    ]
    assert len(stop_features) == len(body["fuel_stops"])
    assert abs(
        D(body["initial_fuel_gallons"])
        + D(body["fuel_purchased_gallons"])
        - D(body["fuel_consumed_gallons"])
        - D(body["final_fuel_gallons"])
    ) <= D(".000002")
    assert D(body["final_fuel_gallons"]) >= D(body["reserve_gallons"]) - D(".000001")
    previous, fuel = D(0), D(body["initial_fuel_gallons"])
    for stop in body["fuel_stops"]:
        node = int(stop["station_id"].rsplit("-", 1)[1])
        assert D(stop["price_usd_per_gallon"]) == D(str(transport.case["prices"][node - 1]))
        assert (stop["longitude"], stop["latitude"]) == transport.points[node]
        assert stop["coordinate_source"]["method"] == "synthetic-fixture"
        consumed = (D(stop["route_mile"]) - previous) / 10
        assert consumed <= fuel + D(".0001")
        assert abs(fuel - consumed - D(stop["arrival_fuel_gallons"])) <= D(".00011")
        assert 0 <= D(stop["arrival_fuel_gallons"]) <= D(stop["departure_fuel_gallons"]) <= 50
        assert D(stop["gallons"]) > 0
        exact = D(stop["gallons"]) * D(stop["price_usd_per_gallon"])
        assert abs(D(stop["cost_usd"]) - exact) <= D(".00501")
        previous, fuel = D(stop["route_mile"]), D(stop["departure_fuel_gallons"])
    assert D(body["distance_miles"]) - previous <= (fuel - D(body["reserve_gallons"])) * 10 + D(
        ".001"
    )
    assert D(body["total_fuel_cost_usd"]) == sum(
        (D(s["cost_usd"]) for s in body["fuel_stops"]), D(0)
    )
    map_response = client.get(body["map_url"])
    assert map_response.status_code == 200
    assert (
        b'id="saved-route"' in map_response.content
        and body["route_id"].encode() in map_response.content
    )
    assert all(r.raw.closed for r in transport.responses)
    return body


def basic_case(name):
    return scenario(name, roads(3, {(0, 2): 600, (0, 1): 400, (1, 2): 200}), [3])


def run_functional(monkeypatch, case):
    client, request, transport, _ = install(monkeypatch, case)
    expected = (
        exhaustive_cost(case["matrix"], case["prices"], case["initial"], case["reserve"])
        if case.get("oracle")
        else case.get("expected_cost")
    )
    response = post(client, request)
    if case.get("expected_error") or (case.get("oracle") and expected is None):
        assert response.status_code in {422, 503}, response.content
        assert response.json()["error"]["code"] == case.get(
            "expected_error", "insufficient_verified_stations"
        )
        result = dict(status=response.status_code, error=response.json()["error"]["code"])
    else:
        body = assert_success(client, response, transport)
        assert D(body["total_fuel_cost_usd"]) == D(expected), (case, body, expected)
        result = dict(
            status=200,
            cost_usd=body["total_fuel_cost_usd"],
            fuel_stops=len(body["fuel_stops"]),
            distance_miles=body["distance_miles"],
        )
    return dict(
        **result, provider_calls=len(transport.requests), oracle_compared=case.get("oracle", False)
    )


def run_fault(monkeypatch, case):
    fixture = basic_case(case["name"]) | case
    client, request, transport, _ = install(monkeypatch, fixture)
    response = post(client, request)
    if case["fault"] in FAULT_EXPECTATIONS:
        status, code = FAULT_EXPECTATIONS[case["fault"]]
    else:
        status, code = (
            502,
            "invalid_routing_matrix" if case["stage"] == "table" else "invalid_routing_response",
        )
    assert response.status_code == status, response.content
    assert response.json()["error"]["code"] == code, response.content
    assert len(transport.requests) == {"base": 1, "table": 2, "final": 3}[case["stage"]]
    assert all(r.raw.closed for r in transport.responses)
    return dict(status=status, error=code, provider_calls=len(transport.requests))


def run_invalid(monkeypatch, case):
    client, request, transport, _ = install(monkeypatch, basic_case(case["name"]))
    patch = case["patch"]
    request = (
        patch
        if patch is None or isinstance(patch, list) or case["name"] == "missing-start"
        else request | patch
    )
    response = post(client, request)
    assert response.status_code == 400, response.content
    assert response.json()["error"]["code"] == case["error"]
    assert transport.requests == []
    return dict(status=400, error=case["error"], provider_calls=0)


def run_lifecycle(monkeypatch, case):
    client, request, transport, catalog = install(monkeypatch, basic_case(case["name"]))
    name = case["name"]
    if name in {"concurrent-same-request", "concurrent-other-request"}:
        transport.arrived = threading.Event()
        transport.release = threading.Event()
        outcomes = []

        def worker():
            outcomes.append(post(Client(), request))

        thread = threading.Thread(target=worker)
        thread.start()
        assert transport.arrived.wait(2)
        try:
            if name == "concurrent-other-request":
                other = post(client, request | {"reserve_gallons": 1})
                assert other.status_code == 503 and other.json()["error"]["code"] == "planner_busy"
            else:
                # A second identical request waits for the first result, then
                # reuses it. Release with a short timer, not an external sleep.
                timer = threading.Timer(0.1, transport.release.set)
                timer.start()
                other = post(client, request)
                assert other.status_code == 200 and other.json()["performance"]["cache_hit"]
        finally:
            transport.release.set()
            thread.join(3)
        assert not thread.is_alive() and len(outcomes) == 1
        assert_success(client, outcomes[0], transport)
        assert len(transport.requests) == 3
        return dict(status=200, provider_calls=3, concurrent_secondary_status=other.status_code)
    if name == "failed-plan-not-cached":
        transport.case.update(stage="base", fault="timeout")
        assert post(client, request).status_code == 502
        transport.case.pop("fault")
        transport.requests.clear()
        response = post(client, request)
        assert_success(client, response, transport)
        return dict(status=200, provider_calls=4, recovered_after="synthetic first-request timeout")
    if name == "busy-cold":
        import fuel.route_planner as planner

        assert planner._planning_lock.acquire(timeout=0.1)
        try:
            response = post(client, request)
            assert (
                response.status_code == 503 and response.json()["error"]["code"] == "planner_busy"
            )
            assert transport.requests == []
        finally:
            planner._planning_lock.release()
        return dict(status=503, error="planner_busy", provider_calls=0)
    first = post(client, request)
    body = assert_success(client, first, transport)
    if name == "expired-map":
        cache.delete("route:" + body["route_id"])
        response = client.get(body["map_url"])
        assert response.status_code == 404 and response.json()["error"]["code"] == "map_expired"
        return dict(status=404, error="map_expired", provider_calls=3)
    if name == "evicted-map":
        cache.delete("route:" + body["route_id"])
    if name == "changed-catalog":
        catalog["metadata"]["version"] = "changed"
    if name == "changed-inventory":
        request["initial_fuel_gallons"] = 45
    if name == "warm-bypasses-busy":
        import fuel.route_planner as planner

        assert planner._planning_lock.acquire(timeout=0.1)
        try:
            response = post(client, request)
        finally:
            planner._planning_lock.release()
    else:
        # Clear mock's first-call marker for an independently uncached plan.
        if name in {"changed-catalog", "changed-inventory"}:
            transport.requests.clear()
        response = post(client, request)
    assert response.status_code == 200, response.content
    if name in {"changed-catalog", "changed-inventory"}:
        second = assert_success(client, response, transport)
        assert second["route_id"] != body["route_id"] and not second["performance"]["cache_hit"]
        return dict(status=200, provider_calls=6, cache_invalidated=True)
    assert response.json()["route_id"] == body["route_id"]
    assert (
        response.json()["performance"]["cache_hit"]
        and response.json()["performance"]["route_api_calls"] == 0
    )
    assert client.get(body["map_url"]).status_code == 200 and len(transport.requests) == 3
    return dict(status=200, provider_calls=3, cache_hit=True)


@pytest.fixture(scope="session")
def e2e_records():
    records = []
    yield records
    if path := os.environ.get("SPOTTER_E2E_REPORT"):
        report = Path(path)
        report.parent.mkdir(parents=True, exist_ok=True)
        report.write_text(
            json.dumps(
                dict(
                    scope=(
                        "200 synthetic transport HTTP integration scenarios; "
                        "no live provider requests"
                    ),
                    test_file_sha256=hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    fuel_python_modules_sha256=hashlib.sha256(
                        b"".join(
                            file.read_bytes()
                            for file in sorted(
                                (Path(__file__).resolve().parents[1] / "fuel").glob("*.py")
                            )
                        )
                    ).hexdigest(),
                    total=len(records),
                    outcomes=dict(Counter(r["outcome"] for r in records)),
                    statuses=dict(Counter(str(r.get("status")) for r in records)),
                    categories=dict(Counter(r["category"] for r in records)),
                    cases=records,
                ),
                indent=2,
                default=str,
            )
            + "\n"
        )


@pytest.mark.parametrize("case", ALL_CASES, ids=lambda case: case["name"])
def test_end_to_end_scenario(monkeypatch, case, e2e_records):
    cache.clear()
    started = time.perf_counter()
    record = dict(name=case["name"], category=case["category"], outcome="failed")
    try:
        runner = {
            "functional": run_functional,
            "provider": run_fault,
            "invalid": run_invalid,
            "lifecycle": run_lifecycle,
        }[case["category"]]
        record.update(runner(monkeypatch, deepcopy(case)))
        record["outcome"] = "passed"
    except Exception as exc:
        record.update(error_type=type(exc).__name__, failure=str(exc), counterexample=case)
        raise
    finally:
        record["elapsed_ms"] = round((time.perf_counter() - started) * 1000, 3)
        e2e_records.append(record)
        cache.clear()
