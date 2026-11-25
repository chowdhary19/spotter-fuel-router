# Verification scope and evidence

Reviewed November 25, 2026. The current implementation passes the automated and live checks below. **Complete CSV row accounting is established; complete US station utilization and unrestricted nationwide route feasibility are not.** The API reports coverage failures explicitly. These are assessment-scale checks, not a production SLA or an all-roads optimality proof.

## Automated checks

The final local run passed **2,255 Python tests and 712 subtests in 26.82 seconds**. All **10 JavaScript checks**, Ruff lint/format checks and Django system checks passed. The Python run includes 188 instances of an existing Shapely transform deprecation warning; no test failed.

There are **2,533 independent oracle comparisons**: 1,800 directed-graph instances, 600 fixed-route instances and 133 HTTP integration scenarios. The graph oracle directly enumerates buying one unit of fuel and traversing original graph edges. It does not reuse the production shortest-distance closure or critical-fuel-state exchange rule. Its integer fixtures admit an integer optimum; separate regression cases exercise continuous precision boundaries.

The **200-case HTTP suite** exercises Django input handling, real bundled city/boundary lookup, candidate geometry, the OSRM byte/JSON parser, graph optimization, final-route reconciliation, serialization, cache and saved maps. Only its road transport and private station catalog are synthetic. It does not make 200 requests to the public routing service.

| Integration category | Scenarios |
|---|---:|
| Seeded directed station graphs | 120 |
| Targeted range, detour, reserve and transit cases | 20 |
| Provider-stage faults | 30 |
| Invalid inputs | 20 |
| Cache and concurrency lifecycles | 10 |

Observed primary responses were **113 HTTP 200** and **87 expected error responses**: 37 × 422, 24 × 502, 20 × 400, 5 × 503 and 1 × 404. Expected errors are not presented as successful trips. Earlier fixture failures and the counterexamples that prompted fixes remain in the local review evidence.

Important regressions include:

- A lower pump price with a longer detour costs $60, while another reachable itinerary costs $31. The graph planner selects the $31 itinerary.
- Exact 500-mile feasibility and a distance just beyond it, including metre-to-mile rounding boundaries.
- Directed/unreachable segments, cheaper-station revisits, transit without purchasing and destination transit.
- Malformed provider geometry with valid US waypoints but an unrelated European line. Geometry endpoints now must match snapped endpoints within one metre.
- A station hundreds of metres off its routed road point is excluded before fuel optimization.
- Reopening a saved coordinate route preserves its full coordinate precision, starting fuel and reserve.
- Unknown-city errors clear previously displayed totals and route layers.
- Exact source identities, many-to-one conflicts, null locator fields, snapshot hash mismatches and original price provenance.

## Reproduce offline tests

```sh
uv sync --locked --group data
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run python manage.py collectstatic --noinput
uv run --group data pytest -q
node --test tests/*.test.cjs
```

Tests use synthetic prices/stations and require neither the employer's CSV nor external routing. CI runs the same checks on a clean checkout. Passing this suite cannot establish availability of external feeds or a live provider.

## Live road verification

Sixteen sequential local HTTP scenarios ran on **November 25, 2026, 18:57:05–18:58:12 IST** using macOS, Gunicorn with one worker/four threads, Django 6.1.3, the real public OSRM service and the expanded 2,363-station catalog. There were **14 independently reconciled trips and two expected coverage/range refusals**, with no verification assertion or transport failure.

| Route | Road miles / result | Fuel stops | New fuel cost | Provider calls | Client HTTP ms |
|---|---:|---:|---:|---:|---:|
| Chicago → Los Angeles | 2033.993 | 6 | $477.56 | 3 | 4,535.12 |
| Los Angeles → Chicago | 2034.539 | 6 | $477.74 | 3 | 4,380.98 |
| New York → Miami, 5-gal reserve | 1283.495 | 5 | $245.23 | 3 | 3,937.04 |
| Dallas → Houston | 238.253 | 0 | $0.00 | 1 | 998.56 |
| Houston → Dallas | 238.263 | 0 | $0.00 | 1 | 936.31 |
| Boise → Nashville | 1935.002 | 6 | $438.23 | 3 | 4,433.45 |
| Nashville → Dallas | 662.747 | 2 | $49.10 | 3 | 3,473.81 |
| Seattle → Portland | 174.373 | 0 | $0.00 | 1 | 1,000.81 |
| Denver → Phoenix | 821.498 | 2 | $111.66 | 3 | 4,269.31 |
| Boston → Washington DC | 437.036 | 0 | $0.00 | 1 | 1,391.22 |
| Atlanta → New Orleans | 459.768 | 0 | $0.00 | 1 | 1,213.70 |
| Minneapolis → Denver | 854.638 | 2 | $105.31 | 3 | 7,253.44 |
| San Francisco → Los Angeles | 380.435 | 0 | $0.00 | 1 | 1,083.94 |
| SF → LA, only 30 gal initially | HTTP 422 | No route | No price invented | — | 1,307.48 |
| Dallas → Houston, road coordinates | 238.253 | 0 | $0.00 | 1 | 2,417.89 |
| Dallas → Houston, empty tank | HTTP 422 | No route | No price invented | — | 2,104.67 |

All successful city-based examples start with 50 gallons; New York–Miami additionally requires five gallons on arrival. The coordinate case also starts full. Short-trip $0 results mean no **new** fuel purchases, not zero fuel consumed.

Successful responses were independently checked for:

- Fuel conservation, every stop's arrival/departure inventory, 50-gallon capacity and destination reserve.
- Distance/consumption agreement and consistency between serialized route legs and total distance.
- Per-stop gallons × original CSV price, cent-rounding bounds and total invoice charges.
- Identity, price and coordinates against the prepared catalog, and prices against a separate full CSV parser.
- Fuel-stop markers and distance from each station to the returned road polyline, allowing only documented coordinate-rounding tolerance.
- Retrievable saved-map HTML, provider-call budget and exact cache reuse.

The API also reported matrix/final-leg agreement within one metre. The public response does not expose its raw matrix; that live diagnostic is **application-reported**, not an independent raw-matrix recomputation. Fault-injection tests exercise the underlying guard with inconsistent matrices and routes.

The two refusal cases remain material evidence:

1. **San Francisco → Los Angeles with 30 gallons:** no matched priced station exists in the 75-mile candidate corridor. HTTP 422 is an honest coverage failure, not a completed journey. The original CSV has no Bay Area or central/northern California prices.
2. **Dallas → Houston with an empty tank:** no zero-driving-distance verified pump exists at the city reference point. The planner refuses to assign an invented origin fuel price or drive on negative fuel.

Two immediate cached Chicago/LA repeats took **55.69 ms and 48.95 ms**, using zero new provider calls. Cold successful requests ranged from **936.31 to 7,253.44 ms**. This is a small sequential functional sample, not a capacity benchmark, percentile guarantee or SLA. The assessment specifies no numerical SLA. The run conservatively budgeted 34 provider calls (each error charged as three) and did not load-test the public service.

## Browser verification

Chrome executed both a real successful request and an invalid-city request through the self-hosted Swagger UI at `/api/docs/`. A long-trip response displayed HTTP 200 with its returned route URL, six purchases and $477.56 total. The map displayed the road line and six numbered markers; the first marker showed its matching quantity and $95.23 purchase.

A separate coordinate-input request used **49.123456 gallons initially and 5.123456 gallons reserve**. The saved map preserved those exact inputs, resubmitted successfully, and reused its cache. A following invalid-city request cleared the previous map and totals; a valid request restored them. The browser map contains an abbreviated geometry display for readability; the API returns full GeoJSON.

Swagger is an API client equivalent for the assessment's “Postman or similar” demonstration. Its JavaScript/CSS are vendored, its schema uses the same origin, and the remote schema validator is disabled. Routing still sends trip coordinates to the configured OSRM provider. The optional Postman collection remains available.

## Container packaging smoke

The current Docker image `spotter-fuel-router:final-review` was built and launched only on `127.0.0.1:18765`. After an initial early readiness disconnect, health returned ready with **2,363 stations**. Home, the Swagger client/schema, app JavaScript and vendored Swagger assets all returned HTTP 200; served asset hashes matched the source files. An invalid POST returned the expected HTTP 400. No live routing requests were made by this packaging check, and the temporary container was removed.

Image ID: `sha256:5fc58f9666f9fd685c69785320a679d5b7d352303e33ce60349b61ef9479ff9e`. This verifies packaging/startup, not route latency or load capacity. Wait for `/health/` to report ready; a listening process alone is insufficient.

## Full-file accounting and reproducibility

All **8,151 CSV rows** are classified: **2,853** US quote rows belong to located station IDs, **4,678** US quote rows belong to unresolved station IDs, and **620** Canadian rows are excluded. Every US quote and its row provenance survives canonicalization. Conflicting undated quotes use the disclosed maximum-observed-price convention.

The catalog contains **2,363 / 6,626 US station IDs (35.66%)**, with at least one matched station in every contiguous US state. The original 1,394 station objects were preserved exactly; the 969 supplemental matches import no external prices. All 4,263 unresolved stations remain in the local audit but cannot participate in routing. No city center was substituted for an unknown station.

An independent offline rebuild from the saved direct feeds and pinned supplemental snapshots reproduced **stations.json, station_coordinates.csv, price_audit.json and coordinate_match_audit.json byte-for-byte**. Live direct feeds may change; exact reproduction requires those same snapshots. See [data preparation](data.md) and [supplemental attribution](supplemental-data.md).

| Reviewed input | SHA-256 |
|---|---|
| Assessment CSV | `c704371f141ded9c54df6c32d488a0ba2ceb589f88c936c967daa5330e0cd241` |
| Runtime station catalog | `1802685900a5a94cfded339603e9608db225b9ecc264c0c9424aa4d3399086d1` |
| Accepted coordinate CSV | `6f82ed09d9e97c7166a3822d95a39160cc37ee3c573b2d6da6b9d9b6b5a2ece7` |
| Fuel modules used in live run | `aaf15a9c54a7c5be74355b0357d894e6d4afbed9f82643df448e0bd7711e93cd` |

The module fingerprint hashes sorted `fuel/*.py` basename + NUL + file bytes. The live server was restarted before the run; health reported the intended 2,363-point catalog.

## Remaining limits and submission

The optimum is over the selected directed station graph and provider segments. The 75-mile candidate corridor, 62-station limit, incomplete coordinates and provider fastest-path choices prevent an all-roads/all-stations minimum-cost claim. Road snaps establish proximity, not driveway legality, opening hours or current fuel availability. The car profile does not enforce commercial-truck restrictions.

No source prices exist for Alaska, Hawaii or DC, and California is sparse even before matching. Closing nationwide coverage needs reliable missing station coordinates and additional regional price coverage, not a larger test count. Initial-tank acquisition cost remains explicitly excluded; prices are undated and fuel grade is unspecified.

The code and browser demo can be reviewed with these limits. The required **actual Loom video of at most five minutes** is still a separate submission artifact. It must demonstrate the API and briefly explain the code. A written guide is not a substitute for the recording.
