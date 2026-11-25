# Data provenance and rebuild

Prices come only from the assessment CSV. Direct official directories and optional pinned AllThePlaces locator snapshots supply station coordinates and identity evidence, never replacement prices. Station preparation happens before serving requests.

## Rebuild locally

```sh
uv sync --locked --group data
uv run --group data python scripts/prepare_data.py \
  --fuel-csv /path/to/fuel-prices-for-be-assessment.csv --download-supplemental-sources
```

| Source | Evidence used |
|---|---|
| [Pilot network directory](https://pilotcompany.com/locations/directory) | Official location ID across Pilot, Flying J, ONE9, Mr. Fuel and listed dealer/licensed businesses; open public diesel business; diesel routing/entrance coordinates |
| [Love's location feed](https://www.loves.com/api/fetch_stores) | Store number, city/state, facility type, locator coordinates |
| [TA / Petro location sheet](https://www.ta-petro.com/api/locationsdata/download/) | Site ID or distinctive exact location name, city/state, coordinates |

The base preparation performs three bounded HTTPS downloads, records hashes and timestamps, parses the embedded Pilot array without executing JavaScript, and reads XLSX values without evaluating formulas. `.data-source-cache/source_manifest.json` preserves source provenance. A missing source or changed schema can prevent setup; fresh downloads can change coverage. The supplemental flag downloads ten fixed, hash-verified AllThePlaces snapshots; [supplemental matching](supplemental-data.md) documents their attribution and stricter identity checks. Omit the flag for the smaller direct-directory-only build.

The assessment attachment, commercial feeds, caches, and derived station catalogs are excluded from the public repository. Public accessibility is not asserted to grant redistribution rights. Keep the local source cache to reproduce that particular snapshot:

```sh
uv run --group data python scripts/prepare_data.py \
  --fuel-csv /path/to/fuel-prices-for-be-assessment.csv \
  --offline --source-dir .data-source-cache \
  --supplemental-source-dir .data-source-cache/alltheplaces
```

An independently supplied coordinate file can be used instead:

```sh
uv run python scripts/build_catalog.py \
  --fuel-csv /path/to/fuel-prices-for-be-assessment.csv \
  --coordinates /path/to/verified-station-coordinates.csv --output-dir data
```

That file requires OPIS ID, latitude, longitude, source URL, match method, confidence, and an explicit verification flag. Invalid, conflicting, unverified, and city-centroid records are quarantined. `--gazetteer` is optional when rebuilding station files; omitting it retains the bundled city lookup. Restart server workers after rebuilding.

## Identity and geographic checks

Station identity requires a unique explicit chain/network store ID plus state and city. TA/Petro records without a store number require a unique exact normalized official/source location name. City spelling differences are accepted only through ordinary normalization or one identical sourced Census place identity. There is no fuzzy city or station matching.

Every official point must lie within the matching source-state polygon. A same-name/state Census place, when available, adds a 50-km plausibility check. Missing Census town entries do not invalidate an otherwise exact identity inside its confirmed state. Neither state polygons nor city representative points become station coordinates.

Pilot's primary public business and site must both be open. Love's and TA records must be present in their official feed/sheet; those sources do not provide an equivalent explicit operating-status field. Multiple OPIS IDs resolving to one physical chain/store identity remain quarantined. The verification flag means these deterministic evidence checks passed; it does not mean manual site inspection, current fuel availability, or proven driveway access.

## Audited inventory and current build

| Measure | Count |
|---|---:|
| Source rows / distinct OPIS IDs | 8,151 / 6,738 |
| Excluded Canadian rows / IDs | 620 / 112 |
| US rows / IDs | 7,531 / 6,626 |
| US IDs with conflicting quotes | 487 |
| Accepted stations / represented contiguous states | 2,363 / 48 |
| US rows represented by accepted station IDs | 2,853 |
| Unlocated US IDs / their quote rows | 4,263 / 4,678 |

The 1,394 directly sourced records comprise 409 Pilot, 170 Flying J, 69 ONE9, 15 Mr. Fuel, five other explicitly listed Pilot-network partners, 523 Love's, 158 TA, and 45 Petro stations. The supplemental match adds 969 records, preserving all original station objects and price evidence. These are source-identity categories, not a claim of current ownership.

The raw CSV covers the 48 contiguous states but has no AK/HI/DC prices. Its eight California station IDs are confined to El Centro, Calexico, Thermal, Jacumba, Coachella, Brawley, and Westmorland. It supplies no Bay Area or central/northern California prices. Full CSV geocoding would still not establish unrestricted nationwide coverage. The expanded subset includes Delaware, Maine and Vermont, but some regions still lack usable stations. Missing geography or prices can cause a coverage error or hide a cheaper feasible alternative.

All 8,151 CSV rows are classified: 2,853 US quote rows belong to located stations, 4,678 US quote rows belong to unresolved stations, and 620 Canadian rows are excluded. This is complete row accounting, not complete station utilization. The audit retains all 7,531 US quotes and their provenance.

Source CSV SHA-256: `c704371f141ded9c54df6c32d488a0ba2ceb589f88c936c967daa5330e0cd241`.

| Local generated file | Purpose |
|---|---|
| `data/stations.json` | Runtime prices, coordinates, and evidence |
| `data/price_audit.json` | All canonical US stations, aliases, quote variants and source-row references |
| `data/station_coordinates.csv` | Accepted OPIS-to-coordinate mapping |
| `data/coordinate_match_audit.json` | Match/rejection decisions, input hashes, state/place checks |

## Bundled reference geography

- [Census 2026 national places gazetteer](https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_place_national.zip): `data/cities.json` retains 32,071 place records in the 50 states and DC. Representative points resolve endpoints only.
- [Census 2025 state boundaries, 1:500,000](https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip): `data/us-boundary.geojson` supports geographic membership checks. These simplified NAD83 boundaries are treated as longitude/latitude for this coarse check, not survey-grade or parcel-level evidence.
- [GeoNames cities15000](https://download.geonames.org/export/dump/cities15000.zip): nine documented populated-place corrections in `data/city_overrides.json` replace offshore Census references. Twelve smaller unresolved offshore-reference places require explicit road-accessible coordinates.
- `data/city_aliases.json`: sourced common names for Boise, Nashville, Macon, Pecos, La Salle and Utica IL, with municipality/GeoNames links and exact Census GEOIDs. `cities.json` records the alias file's hash; coordinates remain unchanged.

Contains [GeoNames](https://www.geonames.org/) data under [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). Selected records are linked to Census identities; no source coordinate values are altered. Land membership alone does not prove road access.

## Prices, units, and missing information

OPIS ID is the canonical price identity. The CSV has no timestamp, fuel grade, currency, volume unit, or validity period. US prices are interpreted as USD per US gallon, and the maximum observed quote per OPIS ID is a disclosed conservative planning convention. Every original quote remains available in the audit. The maximum is not claimed to be the current price, the lowest offered quote, or an upper bound on future prices. Averaging or treating the last row as newest would introduce unsupported assumptions.

Provider road distances are metres, converted using exactly 1,609.344 metres per mile. The graph optimizer uses exact scales derived from decimal inputs; final fuel/money reconciliation preserves input precision. Geographic projection selects candidates only. Output rounding never determines fuel feasibility.
