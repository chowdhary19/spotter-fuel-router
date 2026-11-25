# Four-minute walkthrough

Keep the recording below five minutes. Show the running application and the actual repository. The prepared Postman collection is in `docs/spotter.postman_collection.json`; the browser's API request/response panel is also a working API client.

## 0:00–0:30 — State the contract

“This is a Django API for a vehicle with a 500-mile range at 10 miles per gallon. It takes US start and finish locations and returns a road route, fuel stops and itemized purchase costs. I made the starting tank explicit: it defaults to 50 gallons, and the total is new fuel purchases during the trip.”

## 0:30–1:30 — Demonstrate the actual API

Run Chicago, IL → Los Angeles, CA. Show `distance_miles`, `fuel_stops`, `total_fuel_cost_usd`, `road_leg_distances_miles` and `performance` in the response. Open `map_url` or show the browser map and a station popup. Use the displayed values, since routing data can change.

“The selected stations are actual route waypoints. Fuel quantities are recalculated from the returned road legs. This run needed two routing calls. Repeating the request returns the cached plan with zero additional calls.”

Repeat the request. Then show Dallas → Houston: no purchase is required with the full initial tank, but consumed fuel is still reported. If time permits, show the prepared invalid-coordinate request returning a structured 400.

## 1:30–2:20 — Explain the difficult data decision

Open `scripts/prepare_data.py` and the README's data table.

“The CSV has no coordinates, and most addresses are highway exits. I didn't put stations at city centers. Preparation matches official chain identities and store numbers, rejects ambiguous matches, and records source hashes. This snapshot has 1,179 accepted stations. The rest are excluded, so a coverage failure is explicit.”

“Some OPIS IDs have conflicting prices without dates or fuel grades. I preserve all quotes and use the maximum supplied quote as a documented conservative estimate.”

## 2:20–3:15 — Show the core code

Open `fuel/service.py`, then `fuel/optimizer.py`.

“The first route supplies a corridor. Candidate projection proposes an itinerary, and a second route validates the real station legs. There's one bounded repair attempt, so the request never silently exceeds three routing calls.”

“For the fixed itinerary, buy enough to reach the first cheaper reachable station. Otherwise fill up, capped by the remaining trip and reserve. Fuel and money use Decimal arithmetic. This optimizes purchases on the selected route; it does not claim a globally cheapest road-network route.”

## 3:15–4:15 — Show evidence and close

Run `uv run --group data pytest -q` and show `tests/test_optimizer.py`'s independent exhaustive oracle. Show `docs/verification.md` briefly.

“The oracle enumerates feasible purchases using rational arithmetic, so it doesn't repeat the greedy implementation. Tests also exercise exact range boundaries, real-road recalculation through a mocked provider, reserves, failure responses and cache recovery. I tested several real routes and independently reconciled fuel balances and charge totals.”

“The remaining limits are explicit: incomplete station coverage, undated prices, and a car routing profile rather than truck-specific restrictions. The repository includes setup commands, data preparation and the Postman collection.”

Stop the recording. Verify that the shared Loom link opens for a reviewer without requiring access to your account. The assessment submission needs that link and the GitHub repository URL.
