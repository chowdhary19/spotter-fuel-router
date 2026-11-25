# Loom walkthrough: target 4 minutes 30 seconds

The assessment requires an actual Loom recording of **no more than five minutes**, showing the API, map and code. This guide is not the recording. Use values and test results from the current run; do not quote older measurements or claim nationwide completeness.

Before recording, prepare the source data, restart the single-worker server, run the checks in [verification.md](verification.md), and test a long route successfully on the current revision. Have the browser demo, Postman collection, repository, and terminal output open. Keep unrelated tabs, private messages, secrets, and signed URLs out of view. Do not wait through installation or the entire test suite on camera; show the actual completed output and its command.

## 0:00–0:30: contract and assumptions

Show the README and API input.

“This Django API plans a US road trip for a vehicle with a 500-mile range at 10 miles per gallon. It returns the route, fuel-stop locations and an itemized purchase plan. That gives us a 50-gallon tank. I made the starting fuel explicit: it defaults to a full tank, and the cost shown is fuel purchased during this trip.”

## 0:30–1:30: actual API and map

Run a verified long route, for example Chicago to Los Angeles, through Postman or the browser request panel. Show `fuel_stops`, `total_fuel_cost_usd`, `road_leg_distances_miles`, `validation`, and `performance`. Open `map_url` and one fuel-stop popup.

“The API uses one call for the base road route, one directed distance table for station alternatives, and one final route through the selected waypoints. The final legs and road snaps are checked against what the optimizer used. This response reports the actual call count and selected-station scope.”

Repeat the successful request and show the cache hit with zero new provider calls. If time permits, show a short full-tank trip: zero new purchases is valid even though fuel is consumed. Read the displayed result instead of promising a particular dollar amount or latency.

## 1:30–2:15: station evidence and coverage

Show the README data counts and `scripts/prepare_data.py`.

“The supplied CSV has no coordinates and many addresses are highway exits. Preparation joins explicit official store identities to real station points and checks their state polygons. It also preserves every original price quote and source row. The current local catalog has 1,394 of 6,626 US stations. The remainder stay excluded rather than being placed at city centers.”

“Quotes have no timestamps or fuel-grade field. I use the maximum observed quote as a stated conservative assumption. Some locations have no source prices at all, so I do not claim complete US coverage.”

## 2:15–3:15: the optimization decision

Show `fuel/route_planner.py`, `fuel/station_candidates.py`, then `fuel/fuel_route_optimizer.py`. Location/reference loading is in `fuel/location_catalog.py`; bounded network handling is in `fuel/routing_client.py`.

“The candidate stage uses a 75-mile corridor and caps the matrix at 62 stations, preserving geographic and price alternatives. Projection only chooses candidates. The optimizer uses directed road distances, so a cheaper pump with a long detour can lose to a more expensive pump on a shorter path.”

“It searches exact critical fuel states, preserving transit waypoints and tank inventory. Then a separate fixed-route optimizer recomputes purchases from the final road legs. The optimality claim is limited to the selected graph and its provider segments, not every road or unlocated station in the country.”

## 3:15–4:15: verification and failure behavior

Show the current completed test command/output and `tests/test_fuel_route_optimizer.py`. Show a structured invalid-input or coverage error without calling it a successful trip.

“The independent oracle enumerates buying fuel and traversing edges on the original graph. It does not copy the optimizer's shortcut. Other tests check one-way roads, unavailable segments, exact range boundaries, partial purchases, reserve, source matching, and final-route consistency.”

“The response includes data and optimization limitations. Road snapping checks proximity, not driveway legality or current pump availability. OSRM's car profile also does not establish commercial-truck legality.”

## 4:15–4:30: close

Show the setup commands and repository structure.

“The repository includes reproducible setup, source audit scripts, the Postman collection, and verification instructions. The routing dependency is free and configurable. The remaining limits are incomplete station data, undated prices, and the bounded candidate search.”

Stop before five minutes. Check the duration, playback and reviewer access to the shared Loom link. Submit the actual GitHub repository URL and actual Loom URL; do not substitute the guide or a written transcript.
