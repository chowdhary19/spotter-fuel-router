# Supplemental station coordinates

The assessment CSV supplies prices and station identities but no coordinates. Preparation supports an optional, pinned set of AllThePlaces business-location snapshots alongside the direct Pilot, Love's and TA directories. The resulting catalog still has incomplete station coverage; preparation reports the exact counts and retains all unmatched source IDs.

## Reproduce the supplemental match

From the repository root, with the assessment CSV supplied separately:

```bash
uv sync --group data
uv run --group data python scripts/prepare_data.py \
  --fuel-csv /path/to/fuel-prices-for-be-assessment.csv \
  --download-supplemental-sources
```

This downloads the three direct directories and the ten pinned supplemental files, verifies each supplemental file's size and SHA-256 against `data/supplemental_sources.json`, then matches them locally. Requests use the existing bounded downloader: one request per source, a 20 MiB per-file cap, finite socket timeouts, a 60-second streaming deadline, and TLS verification. The supplemental files together are approximately 22 MB. Nothing downloads during an API request.

The supplemental URLs identify fixed per-spider snapshots; they do not follow `latest`. Direct official directories remain live sources, so their counts may change. To reproduce saved snapshots entirely offline:

```bash
uv run --group data python scripts/prepare_data.py \
  --fuel-csv /path/to/fuel-prices-for-be-assessment.csv \
  --offline \
  --source-dir /path/to/direct-snapshots \
  --supplemental-source-dir /path/to/alltheplaces-snapshots
```

The direct directory must contain `pilot-directory.html`, `loves-locations.json`, and `ta-locations.xlsx`. The supplemental directory must contain the ten filenames in the checked-in manifest, with exactly the pinned bytes. Without `--supplemental-source-dir` or `--download-supplemental-sources`, preparation uses only the original direct sources. Supplying local supplemental files never implies permission to redownload them. `--offline` and `--download-supplemental-sources` cannot be combined.

The source CSV, downloaded feeds, coordinate CSV, catalog and per-station audits stay local and are gitignored. The checked-in manifest contains only public URLs, hashes, byte counts and attribution. `data/coordinate_match_audit.json` records the resolved direct sources, pinned supplemental manifest, original price-file hash, source features, successful matches, and rejection evidence.

## Attribution and source meaning

[AllThePlaces](https://github.com/alltheplaces/alltheplaces) publishes its location data under **CC0-1.0**, while the project's crawler code is MIT licensed. This implementation reads the published data; it does not execute downloaded spiders. See the project's [data API documentation](https://github.com/alltheplaces/alltheplaces/blob/master/API.md) for the archive structure and [license information](https://github.com/alltheplaces/alltheplaces#license).

The snapshots are a third-party aggregation of official store locators, not a fresh direct official feed. Every accepted point retains its immutable snapshot URL, SHA-256, spider name, feature ID, raw external reference and official locator URL. Most pinned datasets were published in the November 14, 2026 run; CEFCO and Roady's use older successful snapshots, as their pinned URLs show. They do not provide a reliable, current operating-status field for every business.

The added brands are Kwik Trip, Kwik Star, Circle K, Casey's, Maverik, Sheetz, Rutter's, CEFCO, 7-Eleven, Stripes and independently named Roady's network stations. Speedway is included in the source manifest but no match is accepted by guessing a changed store number.

## Identity and geography checks

A supplemental match requires an explicit supported brand and store number, source state and normalized city. Roady's independent station names require a unique exact normalized name, state and city. City spelling alternatives are accepted only when both names resolve to one identical, explicitly sourced Census place. The coordinate must lie inside the original CSV state polygon. Where a Census reference exists, the point must be within 50 km of that place reference. Census coordinates are only sanity checks and are never used as pump coordinates.

The matcher accounts for actual source semantics:

- Rutter's `ref` is a CMS identifier, so its explicit `#store` name supplies the store number.
- Casey's has separate store and fuel features; the fuel feature is required.
- 7-Eleven publishes branding on a separate shop feature. It is joined to a fuel feature only when store ID, city, state, source URI and geometry all agree. Both features are retained as evidence.
- Arbitrary QuikTrip Yext IDs, Allsup's WordPress IDs and unverified RaceTrac IDs are not treated as store numbers.
- Conflicting source aliases, conflicting external coordinates and multiple OPIS IDs claiming one external station are rejected. Existing direct-source coordinates are preserved.

`verified=true` means those deterministic identity and source checks passed. It does not mean a person inspected the pump, that fuel is available today, or that a business-location point is a legal road entrance. The routing layer still checks snapping, directed road distance, country boundaries and tank range. This data work cannot establish feasibility for every US origin/destination pair.

## Price preservation and observed coverage

No price is imported from AllThePlaces. OPIS IDs, original price strings, source-row references and conflicting quote variants remain attached to the original CSV. The existing conservative maximum-observed-quote policy is unchanged; a historical quote is not claimed to be today's pump price.

With the reviewed November 25 direct-source snapshot and the pinned supplemental files, preparation produced:

| Metric | Count |
|---|---:|
| Original CSV rows accounted for | 8,151 |
| US rows / unique US station IDs | 7,531 / 6,626 |
| Canadian rows excluded from the US catalog | 620 |
| Preserved direct-source station coordinates | 1,394 |
| Supplemental station coordinates | 969 |
| Combined station coordinates | 2,363 |
| US quote rows represented by matched IDs | 2,853 |
| Remaining unlocated US station IDs | 4,263 |
| States containing matched points | All 48 contiguous states |

This is **35.663% of US station IDs**, not 100% station utilization. Every CSV row is accounted for, but 4,263 US station prices still cannot participate in routing because their coordinates are unresolved. Geographic presence in all 48 states does not prove nationwide route feasibility. Future enrichment requires reliable identity evidence; it must not replace unknown stations with city centers just to increase the count.
