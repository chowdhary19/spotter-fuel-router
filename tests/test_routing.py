"""Routing boundary tests: no network access or real rate-limit waits."""

from __future__ import annotations

import json
from copy import deepcopy
from decimal import Decimal
from unittest import TestCase
from unittest.mock import Mock, patch

import requests
from urllib3.exceptions import ReadTimeoutError

from fuel.errors import PlanningError
from fuel.routing_client import METERS_PER_MILE, OSRMClient

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


def valid_matrix_payload():
    return {
        "code": "Ok",
        "distances": [[0, 85000.1], [87000.2, 0]],
        "sources": [{"location": list(point)} for point in POINTS],
        "destinations": [{"location": list(point)} for point in POINTS],
    }


class OSRMClientTests(TestCase):
    def setUp(self):
        self.http = self.enterContext(patch("fuel.routing_client.requests.get"))
        self.sleep = self.enterContext(patch("fuel.routing_client.time.sleep"))
        self.clock = self.enterContext(
            patch("fuel.routing_client.time.monotonic", return_value=100.0)
        )
        self.lock = Mock()
        self.lock.acquire.return_value = True
        self.enterContext(patch("fuel.routing_client._provider_lock", self.lock))
        self.enterContext(patch("fuel.routing_client._last_call", 0.0))
        self.response = Mock()
        self.response.status_code = 200
        self.response.headers = {}
        self.response.raise_for_status.return_value = None
        self.response.json.return_value = valid_payload()
        self.raw_sent = False

        def read1(_size, *, decode_content):
            assert decode_content is True
            if self.raw_sent:
                return b""
            self.raw_sent = True
            return json.dumps(self.response.json()).encode()

        self.response.raw.read1.side_effect = read1
        self.response.close.side_effect = lambda: setattr(self, "raw_sent", False)
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
            {
                "overview": "full",
                "geometries": "geojson",
                "steps": "false",
                "continue_straight": "false",
            },
        )
        self.assertEqual(kwargs["timeout"], (3, 12))
        self.assertIs(kwargs["allow_redirects"], False)
        self.assertIs(kwargs["stream"], True)
        self.assertEqual(kwargs["headers"]["Accept-Encoding"], "identity")
        self.assertIn("User-Agent", kwargs["headers"])
        self.response.close.assert_called_once()

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
        with patch("fuel.routing_client._last_call", 99.5):
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
            for value in (-1, float("nan"), float("inf"), "not a number", True):
                with self.subTest(field=field, value=value):
                    payload = valid_payload()
                    payload["routes"][0][field] = value
                    self.assert_invalid_response(payload)
        for value in (-1, float("nan"), float("inf"), "not a number", True):
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

    def test_geometry_on_another_continent_is_not_a_valid_map_for_us_waypoints(self):
        payload = valid_payload()
        payload["routes"][0]["geometry"]["coordinates"] = [[10, 50], [11, 50]]
        self.assert_invalid_response(payload)

    def test_reversed_geometry_is_rejected_even_with_consistent_leg_distances(self):
        payload = valid_payload()
        payload["routes"][0]["geometry"]["coordinates"].reverse()
        self.assert_invalid_response(payload)

    def test_each_geometry_endpoint_must_match_its_snap_within_rounding_tolerance(self):
        for index in (0, -1):
            with self.subTest(endpoint=index):
                # Approximately 2.2 metres: more than coordinate rounding.
                payload = valid_payload()
                payload["routes"][0]["geometry"]["coordinates"][index][1] += 0.00002
                self.assert_invalid_response(payload)

    def test_submetre_geometry_rounding_is_accepted_without_moving_the_snaps(self):
        payload = valid_payload()
        payload["routes"][0]["geometry"]["coordinates"][0][1] += 0.000004
        payload["routes"][0]["geometry"]["coordinates"][-1][1] -= 0.000004
        self.response.json.return_value = payload
        route = self.client.route(POINTS)
        self.assertEqual(route.snapped_waypoints, tuple(POINTS))

    def test_geometry_is_compared_with_snapped_roads_not_unsnapped_input_points(self):
        payload = valid_payload()
        for index in (0, -1):
            payload["waypoints"][index]["location"][1] += 0.00005
            payload["routes"][0]["geometry"]["coordinates"][index][1] += 0.00005
        self.response.json.return_value = payload
        route = self.client.route(POINTS)
        self.assertEqual(route.coordinates, route.snapped_waypoints)

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

    def test_table_preserves_direction_and_decimal_distance_units(self):
        self.response.json.return_value = valid_matrix_payload()
        matrix = self.client.table(POINTS, radiuses=[5000, 500])
        self.assertEqual(
            matrix.distances_meters,
            ((Decimal(0), Decimal("85000.1")), (Decimal("87000.2"), Decimal(0))),
        )
        self.assertEqual(matrix.input_points, tuple(POINTS))
        self.assertEqual(matrix.source_waypoints, tuple(POINTS))
        self.assertEqual(matrix.destination_waypoints, tuple(POINTS))
        self.assertEqual(matrix.snapped_waypoints, tuple(POINTS))
        self.assertEqual(matrix.size, 2)
        args, kwargs = self.http.call_args
        self.assertIn("/table/v1/driving/", args[0])
        self.assertEqual(
            kwargs["params"],
            {
                "annotations": "distance",
                "sources": "all",
                "destinations": "all",
                "radiuses": "5000;500",
            },
        )
        self.assertNotIn("fallback_speed", kwargs["params"])
        self.assertEqual(self.client.calls, 1)

    def test_unreachable_matrix_edges_remain_none_and_are_not_estimated(self):
        payload = valid_matrix_payload()
        payload["distances"][0][1] = None
        self.response.json.return_value = payload
        matrix = self.client.table(POINTS)
        self.assertIsNone(matrix.distances_meters[0][1])
        self.assertEqual(matrix.distances_meters[1][0], Decimal("87000.2"))

    def test_matrix_estimated_fallback_cells_are_rejected(self):
        payload = valid_matrix_payload()
        payload["fallback_speed_cells"] = [[0, 1]]
        self.response.json.return_value = payload
        with self.assertRaises(PlanningError) as caught:
            self.client.table(POINTS)
        self.assertEqual(caught.exception.code, "invalid_routing_matrix")

    def test_matrix_shape_and_distance_semantics_are_validated(self):
        invalid = [
            None,
            [],
            [[0, 10]],
            [[0], [10, 0]],
            [[0, -1], [10, 0]],
            [[0, True], [10, 0]],
            [[0, float("nan")], [10, 0]],
            [[0, "not-distance"], [10, 0]],
            [[None, 10], [10, 0]],
            [[1, 10], [10, 0]],
        ]
        for distances in invalid:
            with self.subTest(distances=distances):
                payload = valid_matrix_payload()
                payload["distances"] = distances
                self.response.json.return_value = payload
                with self.assertRaises(PlanningError) as caught:
                    OSRMClient().table(POINTS)
                self.assertEqual(caught.exception.code, "invalid_routing_matrix")

    def test_matrix_source_and_destination_waypoints_must_be_compatible(self):
        for field, value in (
            ("sources", []),
            ("destinations", [None, {"location": POINTS[1]}]),
            ("destinations", [{"location": [-100.1, 40]}, {"location": POINTS[1]}]),
            ("sources", [{"location": [181, 40]}, {"location": POINTS[1]}]),
        ):
            with self.subTest(field=field, value=value):
                payload = valid_matrix_payload()
                payload[field] = value
                self.response.json.return_value = payload
                with self.assertRaises(PlanningError) as caught:
                    OSRMClient().table(POINTS)
                self.assertEqual(caught.exception.code, "invalid_routing_matrix")

    def test_small_waypoint_serialization_difference_is_retained_not_replaced(self):
        payload = valid_matrix_payload()
        payload["destinations"][0]["location"] = [-100.000001, 40]
        self.response.json.return_value = payload
        matrix = self.client.table(POINTS)
        self.assertEqual(matrix.source_waypoints[0], POINTS[0])
        self.assertEqual(matrix.destination_waypoints[0], (-100.000001, 40))

    def test_route_and_table_share_one_three_request_budget(self):
        self.client.route(POINTS)
        self.response.json.return_value = valid_matrix_payload()
        self.client.table(POINTS)
        self.response.json.return_value = valid_payload()
        self.client.route(POINTS, radiuses=[5000, 25])
        self.assertEqual(self.http.call_args.kwargs["params"]["radiuses"], "5000;25")
        with self.assertRaises(PlanningError) as caught:
            self.client.table(POINTS)
        self.assertEqual(caught.exception.code, "routing_budget_exhausted")
        self.assertEqual(self.http.call_count, 3)

    def test_table_cap_is_one_hundred_coordinates_and_ten_thousand_cells(self):
        points = [(-100 + index * 0.001, 40) for index in range(100)]
        payload = {
            "code": "Ok",
            "distances": [[abs(i - j) * 100 for j in range(100)] for i in range(100)],
            "sources": [{"location": point} for point in points],
            "destinations": [{"location": point} for point in points],
        }
        self.response.json.return_value = payload
        self.assertEqual(self.client.table(points).size, 100)
        with self.assertRaises(PlanningError) as caught:
            self.client.table([*points, points[0]])
        self.assertEqual(caught.exception.code, "invalid_waypoints")
        self.assertEqual(self.http.call_count, 1)

    def test_invalid_radiuses_fail_before_dispatch(self):
        for radiuses in ([25], [25, -1], [25, True], [25, float("inf")], [25, None]):
            with self.subTest(radiuses=radiuses):
                with self.assertRaises(PlanningError) as caught:
                    self.client.table(POINTS, radiuses=radiuses)
                self.assertEqual(caught.exception.code, "invalid_radiuses")
        self.http.assert_not_called()
        self.assertEqual(self.client.calls, 0)

    def test_structured_http_400_errors_keep_their_service_meaning(self):
        self.response.status_code = 400
        self.response.raise_for_status.side_effect = requests.HTTPError("400")
        for code, expected in (
            ("NoSegment", "no_road_route"),
            ("NoTable", "no_road_route"),
            ("NotImplemented", "routing_service_unsupported"),
            ("TooBig", "routing_provider_limit"),
        ):
            with self.subTest(code=code):
                self.response.json.return_value = {"code": code}
                with self.assertRaises(PlanningError) as caught:
                    OSRMClient().table(POINTS)
                self.assertEqual(caught.exception.code, expected)
        self.assertEqual(self.http.call_count, 4)

    def test_redirect_is_rejected_without_reading_or_following_it(self):
        self.response.status_code = 302
        self.response.headers = {"Location": "https://other.example.test/"}
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_redirect_rejected")
        self.http.assert_called_once()
        self.response.raw.read1.assert_not_called()
        self.response.close.assert_called_once()
        self.lock.release.assert_called_once()

    def test_content_length_over_cap_is_rejected_before_reading(self):
        self.response.headers = {"Content-Length": "65"}
        with patch("fuel.routing_client.MAX_RESPONSE_BYTES", 64):
            with self.assertRaises(PlanningError) as caught:
                self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_response_too_large")
        self.response.raw.read1.assert_not_called()
        self.response.close.assert_called_once()

    def test_streamed_body_cap_applies_without_content_length(self):
        self.response.raw.read1.side_effect = [b"x" * 40, b"x" * 40]
        with patch("fuel.routing_client.MAX_RESPONSE_BYTES", 64):
            with self.assertRaises(PlanningError) as caught:
                self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_response_too_large")
        self.assertEqual(self.response.raw.read1.call_count, 2)
        self.response.close.assert_called_once()
        self.lock.release.assert_called_once()

    def test_elapsed_budget_is_checked_after_an_available_chunk(self):
        self.clock.side_effect = [100, 100, 100, 100, 121]
        with self.assertRaises(PlanningError) as caught:
            self.client.route(POINTS)
        self.assertEqual(caught.exception.code, "routing_deadline_exceeded")
        self.assertEqual(caught.exception.status, 504)
        self.response.close.assert_called_once()
        self.lock.release.assert_called_once()

    def test_raw_socket_timeout_releases_resources_and_is_typed(self):
        self.response.raw.read1.side_effect = ReadTimeoutError(None, "/table", "timed out")
        with self.assertRaises(PlanningError) as caught:
            self.client.table(POINTS)
        self.assertEqual(caught.exception.code, "routing_unavailable")
        self.response.close.assert_called_once()
        self.lock.release.assert_called_once()
