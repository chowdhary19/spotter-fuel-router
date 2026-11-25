"""Django HTTP endpoints for trip planning, cached maps and catalog readiness."""

import json
import logging
import re
from functools import lru_cache

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import RequestDataTooBig
from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_GET

from .errors import PlanningError
from .location_catalog import city_index, station_catalog, us_boundary
from .route_planner import plan_request

logger = logging.getLogger(__name__)


@require_GET
def home(request):
    return render(request, "index.html")


@require_GET
def health(request):
    try:
        catalog = station_catalog()
        city_index()
        us_boundary()
        return JsonResponse(
            {
                "status": "ready" if catalog["stations"] else "missing_station_coordinates",
                "routable_stations": len(catalog["stations"]),
            },
            status=200 if catalog["stations"] else 503,
        )
    except PlanningError as exc:
        return JsonResponse({"status": "not_ready", "error": exc.code}, status=503)


@csrf_exempt  # Stateless, unauthenticated JSON read API; no session/cookie writes.
def route(request):
    if request.method != "POST":
        response = JsonResponse(
            {"error": {"code": "method_not_allowed", "message": "Use POST with a JSON body."}},
            status=405,
        )
        response["Allow"] = "POST"
        return response
    if request.content_type != "application/json":
        return JsonResponse(
            {
                "error": {
                    "code": "unsupported_media_type",
                    "message": "Use Content-Type: application/json.",
                }
            },
            status=415,
        )
    try:
        if len(request.body) > 16_384:
            raise PlanningError("request_too_large", "Request exceeds 16 KB.", status=413)
        try:
            data = json.loads(request.body)
        except (ValueError, RecursionError, UnicodeDecodeError):
            raise PlanningError(
                "invalid_json", "The body must be a bounded, valid JSON document.", status=400
            ) from None
        result = plan_request(data)
        result["map_url"] = request.build_absolute_uri(result["map_url"])
        return JsonResponse(result, json_dumps_params={"allow_nan": False})
    except RequestDataTooBig:
        return JsonResponse(
            {"error": {"code": "request_too_large", "message": "Request exceeds 16 KB."}},
            status=413,
        )
    except PlanningError as exc:
        response = JsonResponse(
            {"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            status=exc.status,
        )
        if exc.status == 503:
            response["Retry-After"] = "3"
        return response


@require_GET
def route_map(request, route_id):
    if not re.fullmatch(r"[0-9a-f]{32}", route_id):
        return JsonResponse(
            {"error": {"code": "invalid_route_id", "message": "Invalid route ID."}}, status=404
        )
    result = cache.get("route:" + route_id)
    if result is None:
        return JsonResponse(
            {
                "error": {
                    "code": "map_expired",
                    "message": "Map expired or this server restarted. Repeat the API request.",
                }
            },
            status=404,
        )
    return render(request, "index.html", {"saved_route": result})


@lru_cache(maxsize=1)
def _openapi_schema():
    """Read the versioned contract used by the local API client."""
    return json.loads((settings.BASE_DIR / "docs" / "openapi.json").read_text())


@require_GET
def api_schema(request):
    return JsonResponse(_openapi_schema())


@require_GET
def api_docs(request):
    return render(request, "api_docs.html")
