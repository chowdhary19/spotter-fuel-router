# Fuel route planner

A Django API that accepts US start and finish locations and returns a road route, mapped fuel stops, and itemized fuel purchases for a vehicle with a **500-mile range at 10 mpg**. The resulting tank capacity is 50 US gallons.

The planner compares station itineraries using a directed road-distance matrix, then verifies the selected itinerary against a final road route. Its optimum covers the selected station graph and the provider's road segments. It does not claim complete station coverage or the cheapest route across every US road.

## Run locally

Use Python 3.13–3.15 and [uv](https://docs.astral.sh/uv/). The project pins **Django 6.1.3**, the latest stable release verified on November 25, 2026, and locks dependencies in `uv.lock`. See the [official release notes](https://docs.djangoproject.com/en/6.1/releases/6.1.3/) and [package metadata](https://pypi.org/project/Django/6.1.3/).

```sh
uv sync --locked --group data
uv run --group data python scripts/prepare_data.py --fuel-csv /path/to/fuel-prices-for-be-assessment.csv
uv run python manage.py collectstatic --noinput
uv run gunicorn config.wsgi:application --bind 127.0.0.1:8765 \
  --workers 1 --threads 4 --timeout 60 --no-control-socket
```

Open **http://127.0.0.1:8765/**. No database or paid API key is needed. Preparation requires the assessment CSV and internet access to three official station directories; request-time routing uses the free OSRM service. The public repository excludes the employer's CSV and commercial station feeds and derived catalogs. [Data preparation and provenance](docs/data.md) explains how to rebuild them locally.

Restart the server after rebuilding data: reference catalogs are cached in process. `GET /health/` checks local data readiness without calling the routing provider. Wait for it to report `ready` before sending demo traffic; cold initialization can take several seconds.

## API contract

```sh
curl --fail-with-body http://127.0.0.1:8765/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start":"Chicago, IL","finish":"Los Angeles, CA"}'
```

`start` and `finish` accept `"City, ST"` or an object such as `{"latitude":41.8781,"longitude":-87.6298}`. Coordinates must lie within the bundled 50-state/DC boundary. City inputs use Census representative points, sourced common-name aliases, and documented GeoNames corrections for offshore references. They are not street-address geocoding. Ambiguous or unresolved places require explicit coordinates.

| Optional field | Default | Meaning |
|---|---:|---|
| `initial_fuel_gallons` | 50 | Existing fuel, from 0 to 50 gallons |
| `reserve_gallons` | 0 | Minimum fuel at the destination, from 0 to 50 gallons |

The response includes route miles, GeoJSON, `map_url`, fuel-stop locations and prices, gallons bought, arrival/departure fuel, road-leg distances, total purchase cost, data provenance, and provider-call counts. Monetary and fuel quantities are decimal strings. Each purchase is rounded half-up to cents; the total is the sum of displayed purchase charges. Feasibility uses unrounded quantities.

**Cost is fuel bought during this trip.** The initial tank's historical cost is unknown and excluded. A short trip can consume fuel while requiring $0 of new purchases. An empty tank requires a verified pump reachable with zero driving distance; the origin never receives an invented price.

`GET /` provides a browser demo. `GET /routes/<id>/map/` displays a cached map. Maps expire after one hour or process restart; repeat the API request to regenerate one. Import the [Postman collection](docs/spotter.postman_collection.json) for API demonstrations and response checks.

Errors use `{"error":{"code":"...","message":"...","details":{...}}}`. Invalid inputs return 400, oversized requests 413, unsupported content types 415, unavailable route/coverage 422, and provider or capacity failures 502/503/504. The API reports missing evidence rather than inventing a fuel stop or road distance.

## Planning and optimization

An uncached trip requiring fuel uses **three provider calls**:

1. **Base route:** obtain road geometry between the endpoints.
2. **Distance table:** consider stations within a broad 75-mile geometric corridor. Retain at most 62 stations, preserving geographic spread, near-route access, endpoint alternatives, and low prices. One directed matrix covers these stations plus the two endpoints, at most 64 nodes / 4,096 cells. Unreachable cells remain unavailable; estimated fallback edges are rejected.
3. **Final route:** route every waypoint in the optimized graph path, including transit-only visits. Verify each leg against its matrix distance and independently recompute purchases on the returned road legs.

A trip covered by its initial fuel and reserve needs **one call**; a cached request needs **zero**. Redirects and automatic retries are disabled, so a failure cannot silently exceed the three-call budget.

Stations may enter the matrix with a broad search radius, but only points snapped within **25 metres** enter optimization. Final station waypoints also use that limit. Explicit endpoint coordinates use 25 metres; representative city points allow up to 5 km. Final legs must agree with the matrix within one metre, and the corresponding snapped points must remain consistent. These checks establish road-distance consistency and proximity, not driveway legality or fuel availability.

[`fuel/fuel_route_optimizer.py`](fuel/fuel_route_optimizer.py) minimizes purchase cost over the supplied directed graph, with distance and road-leg count as tie-breakers. It uses exact arithmetic and finite critical fuel states rather than rounding fuel into fixed buckets. Shortest-distance closure can concatenate provider segments, and every required transit waypoint is preserved. A separate [fixed-route optimizer](fuel/fixed_route_fuel_optimizer.py) recomputes purchases on the final itinerary.

The fuel search uses Dijkstra over critical inventory states, following the [gas-station exchange rule](https://www.cs.umd.edu/users/samir/grant/gas-j.pdf): before a pricier next refill, fill up; before a cheaper refill, buy only enough to reach it. Floyd–Warshall first computes a directed distance closure because the provider's fastest-route distances need not satisfy the shortest-distance triangle inequality. This considers the graph's feasible refill choices without enumerating every possible trip. Both solvers keep road-distance feasibility in exact metre units; miles are presentation values.

OSRM reports lengths of its fastest paths, not every possible road alternative. The 75-mile corridor, 62-station cap, missing station coordinates, and provider path choices bound the optimization claim. The response exposes candidate counts, whether the subset was capped, excluded station snaps, and the precise scope.

## Requirement mapping

| Assessment requirement | Implementation and boundary |
|---|---|
| US start and finish | City/state or explicit coordinates; geographic validation, with explicit unknown/ambiguous-place errors |
| Route map and fuel-stop locations | Road GeoJSON and interactive map; stations are actual routed waypoints |
| Cost-effective stops and total fuel cost | Graph-based purchase optimization, followed by final-road reconciliation; bounded graph rather than nationwide optimum |
| 500-mile range; 10 mpg | 50-gallon capacity, tank inventory, partial purchases, optional destination reserve |
| Supplied CSV prices | Full-file audit, OPIS identity, retained quote precision/provenance, explicit conflict policy |
| Few free routing calls | One call without purchases; three with matrix planning; zero for cache hits; no API key |
| Fast responses | Local reference lookup, bounded matrix/search, cache, bounded provider work; latency depends on the free service |
| Latest stable Django | Django 6.1.3 pinned and verified as of November 25, 2026 |
| GitHub and Loom submission | Repository plus an actual Loom recording of **no more than five minutes** |

## Data coverage and assumptions

The full CSV contains 8,151 rows and 6,738 OPIS IDs. Excluding 620 Canadian rows leaves 7,531 rows for **6,626 US stations**. There are 487 US station IDs with conflicting prices and no timestamps or fuel-grade field. The planner preserves every quote and uses the maximum observed quote as a conservative planning convention. USD per US gallon is an explicit assumption; the source does not declare those units.

The current local build has **1,394 accepted stations across 45 states**, leaving **5,232 unlocated US stations**. Matching uses official store/network identity, state and city, actual state polygons, and additional city-distance checks when available. City centers are never substituted for station locations.

The source itself has no Alaska, Hawaii, or DC station rows, and only eight California stations, all in the southeast. Better geocoding alone cannot supply absent prices. A valid US endpoint therefore does not guarantee a feasible priced route. Missing stations can also hide cheaper alternatives. See [the data record](docs/data.md) for counts, sources, aliases, and rejection policy.

## Verify and demonstrate

```sh
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run --group data pytest -q
node --test tests/postman_contract.test.cjs
```

The offline suite includes independent exhaustive graph and fixed-route oracles, fuel/charge conservation, exact range boundaries, asymmetric/unreachable roads, detour choices, transit waypoints, source matching, API errors, and cache behavior. It does not require the assessment CSV or external routing. Current verification scope and the live-run recording procedure are in [docs/verification.md](docs/verification.md).

## Operating limits

Use **one worker process** for this demo: request serialization, the bounded one-hour cache, and the outbound rate gate are process-local. Requests to the [free OSRM demo service](https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server) are spaced approximately one second apart. Its availability and latency are not guaranteed. `OSRM_BASE_URL` supports a separate compatible routing deployment.

Provider responses have byte, socket-timeout, and elapsed-time checks. The elapsed budget is not a hard operating-system deadline: a blocking read can overrun it by its read timeout, and DNS may have platform-specific behavior. A busy planner fails explicitly instead of creating an unbounded queue.

The OSRM car profile does not enforce commercial-truck height, weight, hazardous-material, or access restrictions. Prices are undated; opening hours and current pump availability are not verified. Before public deployment, configure a private secret key, hosts, HTTPS, and appropriate shared rate controls.

Docker is optional after local data preparation: `docker build -t spotter-fuel-router .` and `docker run --rm -p 8765:8000 spotter-fuel-router`. The image contains the prepared catalog; source-data redistribution rights remain separate from the application code.
