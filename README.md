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

# 1. Build the local data cache (recommended: run this once from the CLI)
python fetch_data.py --years 5 --radius-km 30

# 2. Launch the app
streamlit run app.py
```

If you skip step 1, the app will offer a "Quick-start" button that fetches a
small live sample (15km radius, 2 years) directly from the API so you can try
it immediately — but for a proper "worst water quality over time" analysis,
run the full `fetch_data.py` pull first (see options below).

## `fetch_data.py` options

```bash
python fetch_data.py --years 8 --radius-km 40   # more history, wider area (largest pull)
python fetch_data.py --years 2 --radius-km 15   # smaller, faster pull
```

- `--years` — how many years of history to fetch (measurement requests are chunked one calendar year at a time, and paged with `_offset`, to stay within the API's ~2 minute per-request timeout).
- `--radius-km` — the EA API only supports radius (`lat`/`long`/`dist`) or EA-area-code filtering for this dataset, **not** a bounding box — an earlier version of this script assumed bounding-box params (`min-easting`/`max-easting`) and got 404s, since that filter doesn't exist on this endpoint. The default of 30km from central London (Trafalgar Square) is chosen because the API docs describe `dist` as filtering "within approximately a square" of that half-width, which comfortably covers the Greater London boundary corner-to-centre. Widen it if you want to include the wider Thames catchment.

Data is written to `data/london_sampling_points.parquet` and
`data/london_measurements.parquet`, which `app.py` reads on startup
(`.gitignore` excludes these — regenerate them locally rather than committing
them, since the archive updates regularly).

## How the API is used

The Water Quality Archive is a Linked-Data-API-style service that returns
JSON/CSV/RDF, documented at
https://environment.data.gov.uk/water-quality/view/doc/reference. Two
endpoints matter here (parameter names taken directly from that reference):

**Sampling points**, filtered by radius around central London:

```
https://environment.data.gov.uk/water-quality/id/sampling-point.json
    ?lat=51.5074&long=-0.1278&dist=30
    &_view=full&_limit=2000&_offset=0
```

**Measurements**, filtered by the same radius and a date range, one calendar
year at a time:

```
https://environment.data.gov.uk/water-quality/data/measurement.csv
    ?lat=51.5074&long=-0.1278&dist=30
    &startDate=2023-01-01&endDate=2023-12-31
    &_view=full&_limit=5000&_offset=0
```

Important things this API does **not** support, that are easy to assume by
analogy with other EA/Defra APIs (e.g. the bathing-water API used elsewhere
in this project's development): there is **no bounding-box filter**
(`min-easting`/`max-easting`) on the sampling-point or measurement endpoints
— only `lat`/`long`/`dist`, `easting`/`northing`/`dist`, or `area`/`subArea`
EA-organisational-unit codes. Using bounding-box params returns a 404.

`fetch_data.py` pages one calendar year at a time via `_offset`/`_limit`, so
no single request risks the API's ~2 minute server-side timeout (for a much
bigger pull than this covers, the API also has an async `/batch/measurement`
endpoint — see the docs). `_view=full` is used so each measurement row
already carries its sampling point's `lat`/`long` directly, avoiding a
separate British-National-Grid reprojection step. Column names in the CSV
export are matched against the documented `_view=full` property paths
first (e.g. `sample.samplingPoint.label`, `determinand.label`), with a
keyword-based fallback in `_clean_measurements()` in case a live export
differs slightly.

> **Note on the docs site:** the human-readable documentation pages under
> `environment.data.gov.uk/.../doc/` and `/view/` block automated crawling via
> `robots.txt`. The `/id/` and `/data/` endpoints used by this project are the
> actual public data API (JSON/CSV/RDF), which is what it's there for.

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
