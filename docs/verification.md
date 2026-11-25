# Verification scope and reproduction

The live results below were measured on November 25, 2026 against the local matrix-planner working tree and its 1,394-station catalog. They are bounded functional checks, not a throughput test, nationwide-coverage proof, or latency guarantee. The final local automated run is recorded below; neither that run nor the route samples establishes production readiness.

## Final local automated run

On the reviewed local working tree, **2,030 Python tests and 710 subtests passed in 34.62 seconds**. All **seven Node contract tests**, Ruff lint/format checks, and Django system checks passed. The Python run reported 19 existing `shapely.ops.transform` deprecation warnings and no test failures.

Rebuilding from the explicitly saved source cache reproduced `stations.json` and `station_coordinates.csv` byte-for-byte at the current 1,394-station count. This confirms reproduction from those inputs; a future live feed can differ.

## Published revision and runtime smoke

The code revision `f683cac1902e17f46b20be07ed47c7799e3e9464` passed [GitHub CI](https://github.com/chowdhary19/spotter-fuel-router/actions/runs/45617457269). A clean archive without the private station files also passed 2,030 tests and 710 subtests. Its additional warnings concerned uncollected static files in that test-only archive; the documented setup and CI both collect static files.

The rebuilt Docker image passed readiness (1,394 stations), home-page, static-JavaScript and malformed-input checks. A five-second first-health timeout was too short during observed cold initialization. A subsequent container run returned ready in 6.565 seconds, then the home page in 36.6 ms, static JavaScript in 7.6 ms and an invalid-location JSON response in 10.5 ms. These are local startup/smoke observations, not route latency or a production SLA. Wait for `/health/` to report `ready`; a listening Gunicorn process is not proof that reference data has loaded. The temporary smoke container was removed.

The browser displayed the revised Chicago–Los Angeles road line and five station markers. A subsequent invalid-city request cleared the preceding route and totals, and a valid request restored them.

## Offline checks

```sh
uv sync --locked --group data
uv run ruff check .
uv run ruff format --check .
uv run python manage.py check
uv run --group data pytest -q
node --test tests/postman_contract.test.cjs
```

The suite uses synthetic station/road fixtures and does not require network access or the employer's CSV. A passing clean-checkout suite therefore verifies application behavior, not availability of the live station feeds or routing service.

| Area | Evidence exercised |
|---|---|
| Directed graph optimizer | Independent exhaustive buy-one-unit/traverse-edge oracle; asymmetric and unreachable roads; cheap-pump detours; non-purchasing transit; revisits; initial inventory; reserve; continuous decimal boundaries |
| Fixed-route optimizer | Independent exhaustive oracle and fuel conservation; exact 500-mile range and just-over-range cases; partial purchases and equal prices |
| Candidate selection | Full bent road geometry, geographic spread, endpoint alternatives, deterministic cap and honest subset metadata |
| Routing and itinerary | Directed distance table, no estimated fallback edges, consistent source/destination snaps, final route/matrix agreement, explicit provider errors and call budget |
| Data preparation | Price precision, quote provenance, excluded non-US rows, state polygons, sourced city aliases, explicit store/network identity, duplicate/closed/ambiguous records |
| API and map | Input validation, bounded errors, fuel/charge output contracts, cache reuse and map recovery |

The graph oracle enumerates fuel-buying and edge-traversal actions on the original graph. It does not share the implementation's shortest-distance closure or critical-state exchange rule. The fixed-route oracle independently enumerates feasible purchases. These checks provide meaningful counterexamples to implementation errors rather than merely asserting copied formulas.

## Measured live checks

Recorded at 2026-11-25 12:44:57 UTC on local macOS, Gunicorn with one worker and four threads, using the public OSRM service. These were sequential cold HTTP requests. All successful cases start with 50 gallons; New York–Miami also requires a five-gallon destination reserve.

| Route | Road miles | Purchase stops | New fuel cost | Provider calls | Client HTTP time |
|---|---:|---:|---:|---:|---:|
| Chicago → Los Angeles | 2,032.751 | 5 | $493.70 | 3 | 6,756.00 ms |
| New York → Miami, 5-gal reserve | 1,285.566 | 4 | $266.54 | 3 | 8,036.32 ms |
| Nashville → Dallas | 662.940 | 2 | $50.92 | 3 | 3,705.03 ms |
| Boise → Seattle | 490.377 | 0 | $0.00 | 1 | 1,120.29 ms |
| Dallas → Houston | 238.253 | 0 | $0.00 | 1 | 1,027.70 ms |
| San Francisco → Los Angeles | 380.435 | 0 | $0.00 | 1 | 1,230.95 ms |
| Boise → Nashville | 1,934.250 | 6 | $448.18 | 3 | 4,658.35 ms |

A separate San Francisco–Los Angeles request starting with **30 gallons** returned HTTP 422 / `insufficient_verified_stations` in 1,159.07 ms. There were zero accepted priced stations in the 75-mile candidate corridor. This is an observed coverage limitation, not a successful trip; the API did not invent a stop to bridge it.

Ten subsequent cached Chicago requests had client HTTP times of **52.50–76.07 ms**, median **60.17 ms**. Cache hits avoid new provider calls. This serial sample does not establish concurrent capacity.

Price CSV SHA-256: `c704371f141ded9c54df6c32d488a0ba2ceb589f88c936c967daa5330e0cd241`.
Accepted-coordinate CSV SHA-256: `5bdf98114724088e82479cf3682c33163860af36c1875c54a89c86876b9ace28`.

## Reproduce and reconcile a live run

Start the documented single-worker server after preparing the local catalog. Record the Git revision, source/coordinate hashes, station count, provider URL, UTC time, requests and responses. At minimum:

1. A long trip with refueling, showing three provider calls and a matrix-consistent final route.
2. A short trip covered by the initial tank, showing one call and zero new purchase cost.
3. A destination reserve, with independently reconstructed arrival fuel.
4. The same successful request repeated, showing a cache hit and zero new provider calls.
5. A meaningful coverage/provider failure, verifying the structured error without presenting it as success.

For each successful response independently reconcile:

- Initial fuel + purchased gallons − road miles / 10 = final fuel.
- Fuel stays between zero and 50 gallons, and the destination reserve is met.
- Road legs sum to the reported route distance.
- Each purchase charge matches gallons × supplied price at documented rounding precision; total equals the displayed charge sum.
- Matrix/final-route leg differences stay within the one-metre tolerance, and station snaps stay within 25 metres.

Open the returned map and inspect the real road line, stop markers, units, labels, and error-state clearing. A JSON success response alone does not prove the map renders correctly.

## Data evidence and geographic limits

The current local catalog contains 1,394 of 6,626 US station IDs across 45 states. Its source hashes and all match decisions are recorded in local audit files. The original 1,179 accepted station records were retained unchanged when 215 further official matches were added; this is data-preservation evidence, not a runtime benchmark.

Common-name regressions verify Boise, Nashville, Macon, Pecos, La Salle and Utica IL against their exact Census identities. State checks recover exact official sites whose towns are absent from the place gazetteer, while wrong-state points and excessive known-city distances remain rejected.

The CSV lacks AK/HI/DC prices and most California geography. The station subset and 62-candidate graph remain incomplete. Passing the checked routes cannot establish nationwide coverage or an all-roads/all-stations optimum.

## Performance interpretation

Uncached refueling plans need a base route, one matrix, and one final route. The default matrix is at most 64×64 and the graph search is bounded. Provider calls are spaced approximately one second apart, so their pacing and network work materially affect latency. Cache hits avoid those calls.

Report measured per-case server/client times with sample sizes and cold/warm status. Do not reuse earlier two-call timings, infer concurrent capacity from serial samples, or describe a few passing routes as an SLA. Provider response-size/time checks bound work but are not a hard DNS/network wall-clock guarantee. The free routing service can fail, throttle, or impose a lower matrix limit.

## Submission evidence

The assessment requires both a GitHub repository URL and an actual Loom video **no longer than five minutes**. The [walkthrough guide](walkthrough.md) is preparation for that recording, not a substitute for it. Confirm that both links are accessible to the reviewer and that the recording shows the revision whose checks are reported.
