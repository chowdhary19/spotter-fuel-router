# Data provenance and rebuild

The API's prices come only from the assessment CSV. Official directories supply coordinates and matching evidence, never replacement prices.

## Local data preparation

```sh
uv sync --locked --group data
uv run --group data python scripts/prepare_data.py \
  --fuel-csv /path/to/fuel-prices-for-be-assessment.csv
```

This performs one bounded download from each of these observed official sources:

| Source | What is read | How it is matched |
|---|---|---|
| [Pilot / Flying J directory](https://pilotcompany.com/locations/directory) | Embedded location array, parsed as JSON5 without executing JavaScript | Chain, store number, state, exact normalized city |
| [Love's location feed](https://www.loves.com/api/fetch_stores) | Store number, city/state, coordinates | Chain, store number, state, exact normalized city |
| [TA / Petro download](https://www.ta-petro.com/api/locationsdata/download/) | Official directory spreadsheet, including coordinates | Chain/name identity, state, city |

Downloads have byte, timeout and redirect limits. A changed source schema fails preparation instead of silently producing invented coordinates. `.data-source-cache/source_manifest.json` records URLs, timestamps and SHA-256 hashes. The source caches and generated station files are intentionally excluded from the public repository. Public availability is not asserted to be a redistribution license; each source retains its own terms.

To reproduce the **same downloaded snapshot**, keep that local cache and run:

```sh
uv run --group data python scripts/prepare_data.py \
  --fuel-csv /path/to/fuel-prices-for-be-assessment.csv \
  --offline --source-dir .data-source-cache
```

Offline preparation was compared byte-for-byte against the measured `stations.json`, `station_coordinates.csv` and `price_audit.json`. All three matched. Fresh downloads can legitimately change the station set.

## Generated files

| Local file | Purpose |
|---|---|
| `data/stations.json` | Runtime coordinates, source prices and evidence for accepted matches |
| `data/price_audit.json` | Every canonical US station, all quoted prices, aliases and original row references |
| `data/station_coordinates.csv` | Explicit verified coordinate contract keyed by OPIS ID |
| `data/coordinate_match_audit.json` | Accepted/rejected match decisions and source hashes |

No geography is inferred from an interstate exit alone. The broad 50-km city proximity check is an identity sanity check, not proof that a station is on the route. Actual route-leg and snap checks happen on each uncached API plan.

The November 25 build includes 1,179 stations across 43 states. It leaves 5,447 US OPIS IDs unlocated. Coverage is uneven, and routes requiring a missing station can fail with `insufficient_verified_stations`. This is an explicit data limitation; “43 states” does not mean complete coverage within those states.

## Bundled reference geography

- **City/state lookup:** [US Census 2026 national places gazetteer](https://www2.census.gov/geo/docs/maps-data/data/gazetteer/2026_Gazetteer/2026_Gaz_place_national.zip). Converted to `data/cities.json`, retaining 32,071 place records in the 50 states and DC. Exact ambiguous city/state names require explicit coordinates. These points resolve trip endpoints only.
- **Land boundary:** [US Census 2025 cartographic state boundaries, 1:500,000](https://www2.census.gov/geo/tiger/GENZ2025/shp/cb_2025_us_state_500k.zip), converted to `data/us-boundary.geojson`. This is a coarse geographic check, not parcel-level or legal border evidence. Source coordinates use NAD83, treated as longitude/latitude for this coarse check.
- **Offshore endpoint corrections:** [GeoNames cities15000](https://download.geonames.org/export/dump/cities15000.zip), licensed [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). `data/city_overrides.json` contains nine exact city/state populated-place matches, original Census points, attribution and source hashes. Coordinates are selected without alteration. This fixes common offshore Census reference points such as San Francisco and Boston. Twelve smaller offshore-reference places have no unambiguous match in that dataset and require explicit road-accessible coordinates instead.

Contains GeoNames data, https://www.geonames.org/, licensed CC BY 4.0. The selected records are matched to Census GEOIDs; source coordinate values are unchanged. GeoNames is supplied as-is, and being on land does not itself prove road access.

## Units and identity

US source prices are interpreted as USD per US gallon. The CSV does not specify fuel grade, currency, volume unit, or collection time. Quote differences are not treated as chronological updates. OPIS ID is the canonical identity, and the maximum supplied quote is a conservative planning policy rather than a claim about the current pump price.

Road distances are supplied by OSRM in metres and converted using exactly 1,609.344 metres per mile. Decimal arithmetic is used for fuel and money; geographic projection uses floating point only for candidate selection. Rounded API values are presentation values, not inputs to feasibility decisions.
