"""Routing boundary tests: no network access or real rate-limit waits."""

from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch

import requests

from fuel.errors import PlanningError
from fuel.routing import METERS_PER_MILE, OSRMClient

POINTS = [(-100.0, 40.0), (-99.0, 40.0)]


def valid_payload():
    return {
        "code": "Ok",
        "routes": [
            {
                "geometry": {
                    "type": "LineString",
                    "coordinates": [[-100.0, 40.0], [-99.0, 40.0]],
                },
                "distance": 85000.1,
                "duration": 4000.5,
                "legs": [{"distance": 85000.0}],
            }
        ],
        "waypoints": [{"location": [-100.0, 40.0]}, {"location": [-99.0, 40.0]}],
    }


class OSRMClientTests(TestCase):
    def setUp(self):
        self.http = self.enterContext(patch("fuel.routing.requests.get"))
        self.sleep = self.enterContext(patch("fuel.routing.time.sleep"))
        self.clock = self.enterContext(patch("fuel.routing.time.monotonic", return_value=100.0))
        self.lock = Mock()
        self.lock.acquire.return_value = True
        self.enterContext(patch("fuel.routing._provider_lock", self.lock))
        self.enterContext(patch("fuel.routing._last_call", 0.0))
        self.response = Mock()
        self.response.status_code = 200
        self.response.raise_for_status.return_value = None
        self.response.json.return_value = valid_payload()
        self.http.return_value = self.response
        self.client = OSRMClient("https://routing.example.test/")

    def assert_invalid_response(self, payload):
        self.response.json.return_value = payload
        with self.assertRaises(PlanningError) as caught:
            OSRMClient("https://routing.example.test").route(POINTS)
        self.assertEqual(caught.exception.code, "invalid_routing_response")
        self.assertEqual(caught.exception.status, 502)

    def test_valid_response_uses_leg_sum_for_one_consistent_distance(self):
        route = self.client.route(POINTS)
        self.assertEqual(route.distance_meters, Decimal("85000.0"))
        self.assertEqual(route.leg_distances_meters, (Decimal("85000.0"),))
        self.assertEqual(route.miles, Decimal("85000.0") / METERS_PER_MILE)
        self.assertEqual(route.snapped_waypoints, tuple(POINTS))
        self.assertEqual(route.coordinates, tuple(POINTS))
        self.assertEqual(route.duration_seconds, 4000.5)
        self.assertEqual(self.client.calls, 1)
        self.lock.release.assert_called_once()

    def test_one_request_has_explicit_full_geometry_and_timeouts(self):
        self.client.route(POINTS)
        self.http.assert_called_once()
        args, kwargs = self.http.call_args
        self.assertEqual(
            args[0],
            "https://routing.example.test/route/v1/driving/"
            "-100.0000000,40.0000000;-99.0000000,40.0000000",
        )
        self.assertEqual(
            kwargs["params"],
            {"overview": "full", "geometries": "geojson", "steps": "false"},
        )
        self.assertEqual(kwargs["timeout"], (3, 12))
        self.assertIn("User-Agent", kwargs["headers"])

    def test_exact_three_call_budget_has_no_fourth_http_request(self):
        for _ in range(3):
            self.client.route(POINTS)
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_budget_exhausted")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual(self.client.calls, 3)

    def test_failed_requests_also_consume_budget_without_implicit_retries(self):
        self.http.side_effect = requests.Timeout("read timed out")
        for _ in range(3):
            with self.assertRaises(PlanningError) as caught:
                self.client.route(POINTS)
            self.assertEqual(caught.exception.code, "routing_unavailable")
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_budget_exhausted")
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual(self.lock.release.call_count, 3)

    def test_rate_pacing_accounts_for_previous_dispatch_time(self):
        with patch("fuel.routing._last_call", 99.5):
            self.client.route(POINTS)
        self.sleep.assert_called_once()
        self.assertAlmostEqual(self.sleep.call_args.args[0], 0.55)

    def test_busy_provider_does_not_call_network_or_spend_budget(self):
        self.lock.acquire.return_value = False
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_busy")
        self.assertEqual(caught.exception.status, 503)
        self.lock.acquire.assert_called_once_with(timeout=3)
        self.lock.release.assert_not_called()
        self.sleep.assert_not_called()
        self.http.assert_not_called()
        self.assertEqual(self.client.calls, 0)

    def test_http_429_is_explicit_rate_limit_error_without_retry(self):
        self.response.status_code = 429
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_rate_limited")
        self.assertEqual(caught.exception.status, 503)
        self.assertEqual(self.client.calls, 1)
        self.http.assert_called_once()
        self.lock.release.assert_called_once()

    def test_http_failure_is_a_provider_error_not_a_no_route_result(self):
        self.response.raise_for_status.side_effect = requests.HTTPError("503")
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_unavailable")
        self.assertEqual(caught.exception.status, 502)
        self.http.assert_called_once()
        self.lock.release.assert_called_once()

    def test_connection_failure_releases_provider_lock(self):
        self.http.side_effect = requests.ConnectionError("connection failed")
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_unavailable")
        self.assertEqual(caught.exception.status, 502)
        self.lock.release.assert_called_once()

    def test_malformed_json_is_a_typed_provider_error(self):
        self.response.json.side_effect = ValueError("not JSON")
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_unavailable")
        self.assertEqual(caught.exception.status, 502)

    def test_no_route_and_no_segment_are_distinct_from_provider_failure(self):
        for code in ("NoRoute", "NoSegment"):
            with self.subTest(code=code):
                self.response.json.return_value = {"code": code}
                with self.assertRaises(PlanningError) as caught:
                    OSRMClient("https://routing.example.test").route(POINTS)
                self.assertEqual(caught.exception.code, "no_road_route")
                self.assertEqual(caught.exception.status, 422)

    def test_json_root_must_be_an_object(self):
        for payload in (None, [], ["unexpected"], "unexpected", 42, True):
            with self.subTest(payload=payload):
                self.assert_invalid_response(payload)

    def test_missing_or_empty_route_is_rejected(self):
        for payload in ({}, {"code": "Ok"}, {"code": "Ok", "routes": []}):
            with self.subTest(payload=payload):
                self.assert_invalid_response(payload)

    def test_leg_count_must_match_requested_waypoint_count(self):
        for legs in ([], [{"distance": 40000}, {"distance": 45000}]):
            with self.subTest(legs=legs):
                payload = valid_payload()
                payload["routes"][0]["legs"] = legs
                self.assert_invalid_response(payload)

    def test_snapped_waypoint_count_must_match_request(self):
        payload = valid_payload()
        payload["waypoints"].pop()
        self.assert_invalid_response(payload)

    def test_distance_sum_mismatch_is_rejected(self):
        payload = valid_payload()
        payload["routes"][0]["distance"] = 86000
        self.assert_invalid_response(payload)

    def test_negative_or_nonfinite_distances_and_duration_are_rejected(self):
        for field in ("distance", "duration"):
            for value in (-1, float("nan"), float("inf"), "not a number"):
                with self.subTest(field=field, value=value):
                    payload = valid_payload()
                    payload["routes"][0][field] = value
                    self.assert_invalid_response(payload)
        for value in (-1, float("nan"), float("inf"), "not a number"):
            with self.subTest(leg_distance=value):
                payload = valid_payload()
                payload["routes"][0]["legs"][0]["distance"] = value
                self.assert_invalid_response(payload)

    def test_geometry_requires_a_linestring_with_at_least_two_points(self):
        for geometry in (
            {"type": "Point", "coordinates": [-100, 40]},
            {"type": "LineString", "coordinates": []},
            {"type": "LineString", "coordinates": [[-100, 40]]},
        ):
            with self.subTest(geometry=geometry):
                payload = valid_payload()
                payload["routes"][0]["geometry"] = geometry
                self.assert_invalid_response(payload)

    def test_route_and_snapped_coordinates_must_be_valid_geographic_pairs(self):
        bad_points = [
            None,
            [0],
            [0, 0, 0],
            [float("nan"), 40],
            [0, float("inf")],
            [181, 40],
            [-181, 40],
            [0, 91],
            [0, -91],
            [True, 40],
            ["-100", 40],
            ["invalid", 40],
        ]
        for location in bad_points:
            for source in ("geometry", "snapped"):
                with self.subTest(source=source, location=location):
                    payload = valid_payload()
                    if source == "geometry":
                        payload["routes"][0]["geometry"]["coordinates"][0] = deepcopy(location)
                    else:
                        payload["waypoints"][0]["location"] = deepcopy(location)
                    self.assert_invalid_response(payload)

    def test_waypoint_count_validation_happens_before_network_access(self):
        for points in ([], [(0, 0)], [(0, 0)] * 101):
            with self.subTest(count=len(points)):
                with self.assertRaises(PlanningError) as caught:
                    self.client.route(points)
                self.assertEqual(caught.exception.code, "invalid_waypoints")
        self.http.assert_not_called()
        self.assertEqual(self.client.calls, 0)

    def test_invalid_requested_coordinates_fail_before_network_access(self):
        for location in (
            None,
            [0],
            [0, 0, 0],
            [float("nan"), 40],
            [181, 40],
            [0, 91],
            [True, 40],
            ["-100", 40],
        ):
            with self.subTest(location=location):
                with self.assertRaises(PlanningError) as caught:
                    self.client.route([POINTS[0], location])
                self.assertEqual(caught.exception.code, "invalid_waypoints")
        self.http.assert_not_called()
        self.assertEqual(self.client.calls, 0)
