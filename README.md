# London Water Quality Explorer

A Streamlit app that maps how water quality across Greater London has changed
over time, using the Environment Agency's open **Water Quality Archive** API
(`https://environment.data.gov.uk/water-quality/`).

- `fetch_data.py` — pulls London sampling points + measurements from the EA API and caches them locally as Parquet.
- `app.py` — Streamlit app: an animated map of "severity" by sampling point over time, a trend chart of the worst sites, and a ranking table, all filterable by determinand (Ammonia, Dissolved Oxygen, Nitrate, etc.), year range and sampling point type.

## Quick start

```bash
git clone <this-repo>
cd london-water-quality
pip install -r requirements.txt

# 0. Sanity-check the API against a handful of records first (fast, no cache written)
python fetch_data.py --probe

# 1. Build the local data cache (recommended: run this once from the CLI)
python fetch_data.py --years 5 --radius-km 30

# 2. Launch the app
streamlit run app.py
```

If you skip step 1, the app will offer a "Quick-start" button that fetches a
small live sample (15km radius, 2 years) directly from the API so you can try
it immediately — but for a proper "worst water quality over time" analysis,
run the full `fetch_data.py` pull first (see options below).

**Run `--probe` first.** This project targets a genuinely new EA API
(v1.3.1, OpenAPI 3.1, docs at
`https://environment.data.gov.uk/water-quality/api-docs`) that replaced an
older Linked-Data-API service most existing tutorials still reference. Its
exact request parameters were confirmed by hand against the live Swagger UI,
but the exact JSON shape of an individual *observation* record was not — see
"How the API is used" below. `--probe` fetches a handful of sampling points
and observations and prints the raw response, so you can catch any mismatch
in seconds rather than after a multi-minute full pull.

## `fetch_data.py` options

```bash
python fetch_data.py --probe                    # quick sanity check, no cache written
python fetch_data.py --years 8 --radius-km 40    # more history, wider area (largest pull)
python fetch_data.py --years 2 --radius-km 15    # smaller, faster pull
```

- `--years` — how many years of history to fetch (observation requests are chunked one calendar year at a time, and paged with `skip`/`limit`, to stay under the API's per-request row caps).
- `--radius-km` — geographic filtering uses the confirmed `latitude`/`longitude`/`radius` (km) query parameters. The default of 30km from central London (Trafalgar Square) comfortably covers the Greater London boundary corner-to-centre (~25-30km at the furthest edges). The API also accepts a GeoJSON Polygon body for a tighter shape (confirmed request format: a bare `{"type": "Polygon", "coordinates": [...], "bbox": [...]}`, not wrapped) — not wired up by default here, but the shape is documented below if you want a precise Greater London boundary instead of a circle.

Data is written to `data/london_sampling_points.parquet` and
`data/london_measurements.parquet`, which `app.py` reads on startup
(`.gitignore` excludes these — regenerate them locally rather than committing
them, since the archive updates regularly).

## How the API is used

**This project targets the new Water Quality Archive REST API** (v1.3.1,
OpenAPI 3.1), with interactive docs at
`https://environment.data.gov.uk/water-quality/api-docs`. This is a
different, more recently built service than the older Linked-Data-API
(`environment.data.gov.uk/water-quality/id/...`, `.../data/measurement.csv`)
that most existing blog posts and tutorials reference — that older one now
404s. Because the docs *pages* block automated fetching via `robots.txt`
(the same as most `environment.data.gov.uk` documentation, though the actual
`/sampling-point` and `/data/*` data endpoints are the public API and meant
to be queried programmatically), the parameters below were confirmed by hand
against the live Swagger UI rather than scraped.

**Sampling points** — `GET /sampling-point`, radius-filtered:

```
https://environment.data.gov.uk/water-quality/sampling-point
    ?latitude=51.5074&longitude=-0.1278&radius=30
    &skip=0&limit=100
```
Header `Accept-Crs: http://www.opengis.net/def/crs/EPSG/0/4326` requests
WGS84 lat/long geometry instead of the default British National Grid, so no
reprojection is needed. Confirmed response shape is a JSON-LD
`hydra:Collection` with a `member` array; each member has `notation`,
`prefLabel`, `geometry.asWKT` (a WKT `POINT(lon lat) <crs-uri>` string),
`samplingPointType`, `samplingPointStatus`, `region`, `area`, `subArea` —
this was confirmed directly against a live example response.

**Observations** — `POST /data/observation`, for many sampling points at once:

```
https://environment.data.gov.uk/water-quality/data/observation
    ?latitude=51.5074&longitude=-0.1278&radius=30
    &dateFrom=2023-01-01&dateTo=2023-12-31
    &skip=0&limit=2500
```
with headers `Accept: text/csv`, `CSV-Header: present`,
`Accept-Crs: .../EPSG/0/4326`, `API-Version: 1`. All of the query parameter
names above (`latitude`/`longitude`/`radius`, `dateFrom`/`dateTo`, `skip`,
`limit`, plus `determinand`, `samplingPurpose`, `complianceOnly`,
`pointNotation`, `precannedArea`, and others) were confirmed directly from
the endpoint's Swagger "Parameters" panel. The endpoint also accepts a bare
GeoJSON `Polygon` request body (`{"type": "Polygon", "coordinates": [...],
"bbox": [...]}`, not wrapped in another object) as an alternative to the
radius filter — confirmed from the "Try it out" request-body editor — but
`fetch_data.py` uses the simpler radius parameters by default. Per the
endpoint's own description, **CSV/JSONLINES responses are capped at 2500
rows per request; JSON-LD responses at 250** — `fetch_data.py` pages with
`skip`/`limit` accordingly and chunks one calendar year at a time.

**What wasn't confirmed:** the exact JSON-LD field names for an individual
*observation* record (as opposed to a sampling point or sampling record,
both of which were confirmed from live examples). `fetch_data.py` therefore
requests CSV first, since the API's own docs explicitly call out CSV as a
supported bulk format for this endpoint; if the server doesn't actually
serve CSV, it falls back to a tolerant, keyword-based field finder over the
JSON-LD response (`_flatten_observation_member` / `_OBS_FIELD_CANDIDATES` in
`fetch_data.py`). Either way, the first raw page fetched is written to
`data/_debug_first_observation.csv` or `.json` — if `fetch_data.py` reports
zero rows parsed, look at that file first; it'll show you the real shape,
and the field-candidate list is a single, clearly-commented block to edit.
**Run `python fetch_data.py --probe` before a full pull** to catch this in
seconds rather than after a multi-minute run.

## Defining "worst"

Determinands don't all point the same direction — e.g. high Ammonia is bad,
but low Dissolved Oxygen is bad. `app.py` keeps a small keyword-based
direction table (`DETERMINAND_DIRECTION`) and computes, per year, a 0–100
**severity score** per sampling point: a percentile rank of that site's mean
reading among all sites that year, oriented so 100 = worst. This keeps "worst"
comparable across very different determinands and units without needing
official water-quality standard thresholds baked in (which vary by water body
type and are worth adding if you want regulatory-accurate compliance
classification rather than a relative ranking).

## Customising

- **Bounding box** — edit `LONDON_BBOX` in `fetch_data.py` to widen/narrow the area (e.g. to include the whole Thames catchment).
- **Determinands** — the app auto-populates its dropdown from whatever determinands are present in your cached data; no need to know EA's internal determinand codes.
- **PPTX export** — every chart has a "Download slide" button (requires `kaleido`).

## Licensing

Data: © Environment Agency copyright and/or database right, released under
the [Open Government Licence v3](https://www.nationalarchives.gov.uk/doc/open-government-licence/version/3/).
