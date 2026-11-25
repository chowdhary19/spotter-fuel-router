# Verification record

Measured locally on November 25, 2026, using the supplied price snapshot, the 1,179-station prepared catalog and the public OSRM driving service. These are bounded functional checks, not a load test or latency guarantee.

## Real road routes

| Route | Road miles | Fuel stops | New fuel spend | Arrival reserve | Routing calls | API processing time |
|---|---:|---:|---:|---:|---:|---:|
| Dallas → Houston | 238.253 | 0 | $0.00 | 26.174696 gal remaining | 1 | 954.39 ms |
| Chicago → Los Angeles | 2,036.015 | 7 | $520.71 | 0 gal | 2 | 2,958.82 ms |
| New York → Miami | 1,287.609 | 4 | $279.44 | 5 gal | 2 | 2,400.55 ms |
| San Francisco → Los Angeles | 380.435 | 0 | $0.00 | 11.956530 gal remaining | 1 | 1,936.78 ms |

All start with 50 gallons. The Dallas case needs no new purchases; it consumes 23.825304 gallons of existing fuel. The Chicago itinerary adds 7.090 miles to the original base road route. Its longest routed leg is 479.570 miles. The New York request explicitly requires five gallons at the destination.

A separate check reconstructed tank contents from the returned route positions and purchases, checked nonnegative arrival fuel, capacity ≤50 gallons, destination reserve, line-item money rounding and the total charge. It also reconciled the sum of road legs with the reported distance. These checks allowed only the documented output rounding tolerance; optimization itself uses unrounded distances.

Ten consecutive cached Chicago requests made **zero external routing calls**. Client-side median latency was 31.06 ms, minimum 30.13 ms and maximum 41.83 ms on this machine. This sample does not establish concurrent throughput or an SLA.

## Automated checks

Final local checks: **127 Python tests and 684 subtests passed**, including the 600 oracle instances. Seven offline Postman-contract checks passed, including deliberately incorrect responses that must fail. Ruff lint/format and Django system checks passed. A clean checkout without the private station files also passed the offline suite; the bundled synthetic fixtures supply test station data.

The optimizer is compared against an independent exhaustive dynamic-programming oracle on 600 seeded integer-distance cases. That oracle uses rational arithmetic and enumerates all feasible purchases, rather than copying the greedy rule. Cases just above and below the 500-mile boundary are checked separately at Decimal precision.

HTTP and service tests cover provider validation, road-distance recalculation, bounded repair, insufficient station coverage, initial fuel and reserve, money/fuel conservation, cache invalidation, map-cache recovery, malformed requests and bounded JSON errors. Offline tests never need the external provider.

Data preparation preserves quote precision and row identity. Matching regressions reject ambiguous store identities, explicit closures, implausible city distances and duplicate physical identities. Rebuilding with the saved directory snapshots reproduced the three main derived data files byte-for-byte.

## Found and fixed during review

- Separate map-cache eviction could leave a cached API result with an expired map. Cache hits now republish the map entry.
- Oversized bodies and extreme JSON numbers could escape the normal JSON error contract. They now return bounded JSON errors.
- Census place reference points can be offshore. Nine supported corrections retain explicit GeoNames provenance; unresolved cases ask for coordinates instead of routing to the ocean.
- A failed browser request could leave the previous successful route visible. The old summary and route layer are cleared on failure.
- Django's default same-origin referrer policy prevented compliant cross-site map-tile requests. The map now sends only its origin cross-site, as required by the OSM tile policy, without exposing route paths or query data.

## Limits retained

Station coverage is partial and prices are undated. Global cheapest-road-route optimality is not claimed. Road snaps do not verify driveways or opening hours. The OSRM car profile is not truck-legal routing. The free service can fail or be rate-limited, and the one-process cache is intentionally a local-demo implementation.
