# Fuel route planner

A Django API that returns a US driving route, a map of fuel stops, and an itemized fuel purchase plan for a 50-gallon vehicle traveling at 10 mpg. Selected stops are routed as actual road waypoints before a plan is returned.

The main design decision is to separate **finding a plausible station itinerary** from **proving its fuel feasibility**. A station close to a highway on a map is not necessarily reachable from that highway. Projected distances select candidates; routed leg distances determine the final purchases.

## Run locally

Python 3.13+ and [uv](https://docs.astral.sh/uv/) are required. Django is pinned to **6.1.3**, with all dependencies pinned in `uv.lock`.

```sh
uv sync --locked --group data
uv run --group data python scripts/prepare_data.py --fuel-csv /path/to/fuel-prices-for-be-assessment.csv
uv run python manage.py collectstatic --noinput
uv run gunicorn config.wsgi:application --bind 127.0.0.1:8765 \
  --workers 1 --threads 4 --timeout 60 --no-control-socket
```

Open **http://127.0.0.1:8765/**. No database, paid API, routing API key or runtime geocoding service is needed. Data preparation is a separate, bounded offline-build step; it downloads the published station directories once and records source hashes. Supply the CSV attached to the assessment. The repository does not redistribute that attachment or the commercial station-directory datasets.

If the source directories change, rerunning preparation can change station coverage. The measured local build and its limitations are recorded below. A user-supplied verified coordinate file can also be used by `scripts/build_catalog.py`; city centroids are rejected as station evidence.

## API

```sh
curl --fail-with-body http://127.0.0.1:8765/api/route/ \
  -H 'Content-Type: application/json' \
  -d '{"start":"Chicago, IL","finish":"Los Angeles, CA"}'
```

Both endpoints accept `"City, ST"` or an object such as `{"latitude":41.8781,"longitude":-87.6298}`. Explicit coordinates are checked against the 50 US states and Washington, DC. City inputs resolve locally to Census representative place points, with nine documented GeoNames overrides for offshore references. These are not street addresses. Ambiguous names require coordinates. Territories and non-road island-to-mainland trips are outside this implementation's supported road network.

Optional inputs:

| Field | Default | Meaning |
|---|---:|---|
| `initial_fuel_gallons` | 50 | Fuel already in the tank, between 0 and 50 |
| `reserve_gallons` | 0 | Minimum fuel required **at the destination**, between 0 and 50 |

The response contains the actual road distance, total purchased fuel cost, purchases with arrival/departure fuel, routed leg distances, full GeoJSON, and a `map_url`. Decimal quantities are JSON strings so price precision is not lost. Purchase charges are rounded individually to cents using half-up rounding; the total is the sum of those displayed charges. Distances and gallons are rounded only for output, never before feasibility checks.

**Cost means fuel bought during this trip.** The assignment does not state the initial tank level or what that fuel cost, so the default is an already-full tank and its historical acquisition cost is excluded. A 200-mile trip can therefore consume 20 gallons while requiring $0 of new purchases. Both quantities are returned explicitly. An empty initial tank is supported only if a verified fueling location is reachable at the start; the API never invents an origin pump price.

Additional endpoints:

- `GET /health/`: local catalog readiness and station count, without calling the routing provider.
- `GET /routes/<id>/map/`: interactive map for a cached result. Maps expire after one hour or a process restart; repeat the API request to regenerate them.
- `GET /`: browser demo and API request/response viewer.

Import [`docs/spotter.postman_collection.json`](docs/spotter.postman_collection.json) into Postman to run the demonstration and response checks.

Errors have a consistent `{"error":{"code":"...","message":"...","details":{...}}}` shape. Invalid inputs return 400, unsupported content types 415, oversized bodies 413, unavailable coverage/road routes 422, and provider failures 502/503. A coverage failure is reported rather than fabricating a station or returning an infeasible trip.

## How the plan is built

1. Resolve endpoints locally and request one base route from [OSRM](https://project-osrm.org/docs/v5.24.0/api/), including its road geometry.
2. Project verified stations into a metre-based local coordinate system. Search a 2-mile corridor, expanding locally to 5 and then 10 miles only if necessary. Projected progress is an estimate used for candidate selection.
3. Run the fuel optimizer over the ordered candidates. Route its selected stations as waypoints in a second request. Reject excessive road snaps and recompute all fuel purchases from the returned road legs.
4. If detours break feasibility, insert candidate waypoints into over-range legs and spend at most one final routing call on validation. If that still fails, return an explicit error. No retry silently exceeds the three-call budget.

Short trips need **one routing call**. Normal multi-stop trips need **two**. Bounded repair can use **three**. Cached requests need **zero**. There is no per-station directions call.

### Fuel optimizer

For a fixed ordered route, at each station buy only enough to reach the first cheaper reachable station. If no cheaper station is within range, fill up, capped by the remaining trip and destination reserve. Existing fuel is retained and never sold. A monotonic stack finds the next cheaper station; sorting dominates at O(n log n), with an O(n) scan afterward.

The exchange argument is simple: buying extra fuel before a cheaper reachable station cannot reduce cost. When all reachable stations are more expensive, replacing a later purchase with fuel bought now cannot increase cost, up to tank capacity. This gives minimum purchase cost **for the fixed station itinerary**.

It does **not** prove the cheapest route across the entire road network. Choosing roads and station detours is a bounded heuristic under the routing-call budget. Only 1,179 of 6,626 US stations had accepted coordinates in the measured build, so cheaper unlocated alternatives may exist. The objective is fuel spend, not minimum stop count or driver time; small top-ups can be optimal under that model.

## Data decisions

The attached CSV was read in full, not sampled:

| Observation | Treatment |
|---|---|
| 8,151 rows; 6,738 distinct OPIS IDs | Preserve station identity and source-row provenance |
| 620 Canadian rows / 112 station IDs | Exclude from US pricing and routing |
| 7,531 US rows / 6,626 US station IDs | Canonical US inventory |
| 487 US station IDs have conflicting prices | Use the maximum supplied quote per station as a conservative estimate |
| No timestamp, fuel grade, currency or unit | No “latest” quote claim; explicitly assume USD per US gallon for US rows |
| No latitude/longitude; most addresses are highway exits | Match official chain identity, store number, state and city; reject ambiguity |

Using the cheapest duplicate would systematically understate costs without knowing which price is valid. Averaging would invent a price. The maximum policy is explicit and replaceable; all quotes remain in the local audit.

The measured coordinate build matched 1,179 stations across 43 states: 506 Love's, 330 Pilot, 152 Flying J, 147 TA and 44 Petro. Matching uses official locator coordinates, with a broad Census city-proximity sanity check. It rejects ambiguous identifiers, conflicting store-number aliases, explicit closures and duplicate physical identities. Coordinates are evidence of a location, not proof of current opening hours or fuel availability. The remaining 5,447 US station IDs are excluded from routing.

Source CSV SHA-256: `c704371f141ded9c54df6c32d488a0ba2ceb589f88c936c967daa5330e0cd241`.

Sources and generated files are documented in [`docs/data.md`](docs/data.md). Source downloads and restricted derived datasets are ignored by Git. Government and separately licensed reference data retain their own provenance and attribution.

## Verification

```sh
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run --group data pytest -q
node --test tests/postman_contract.test.cjs
```

Tests include an independent exhaustive dynamic-programming oracle using rational arithmetic for 600 seeded small instances. It enumerates every feasible purchase instead of reproducing the greedy algorithm. Boundary cases include exactly 500 miles, a distance just above 500 miles, partial purchases, equal prices, empty tanks, reserves, conflicting quotes and malformed inputs. Mocked routing tests cover actual-road recalculation, the three-call repair path, provider failures, fuel/cost reconciliation and cached-map recovery.

See [`docs/verification.md`](docs/verification.md) for measured live-route checks and their limits. Tests do not require the external routing service or the employer's CSV.

## Operational scope

- The free OSRM demo service has no availability guarantee. Outbound requests are rate-limited to approximately one per second, with bounded connect/read timeouts and no automatic retries. See the [demo-server policy](https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server). `OSRM_BASE_URL` can point to a self-hosted instance.
- Run **one worker process** for this demo. The bounded cache, request serialization and outbound rate gate are process-local. Multiple processes require shared equivalents or a dedicated routing deployment.
- The car routing profile does not enforce truck height, weight, hazardous-material or commercial-access restrictions. The response and map say so explicitly.
- Road snapping proves proximity only. It does not establish driveway legality, station opening hours or fuel availability.
- No background jobs, database or geocoding calls run on the request path. A busy planner returns 503 with `Retry-After` instead of accumulating unbounded work.
- The local secret key is for this stateless demonstration. Before public deployment, set a private `DJANGO_SECRET_KEY`, restrict hosts, use HTTPS and add an appropriate shared rate limiter. This repository is an assessment implementation, not a production truck-dispatch system.

After preparing the local data, Docker is an alternative: `docker build -t spotter-fuel-router .` then `docker run --rm -p 8765:8000 spotter-fuel-router`. The image includes the locally prepared catalog; do not publish that image without reviewing redistribution rights for its data.
