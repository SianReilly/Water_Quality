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
python fetch_data.py --years 5 --max-points 400

# 2. Launch the app
streamlit run app.py
```

If you skip step 1, the app will offer a "Quick-start" button that fetches a
small live sample (60 sites, 2 years) directly from the API so you can try it
immediately — but for a proper "worst water quality over time" analysis, run
the full `fetch_data.py` pull first (see options below).

## `fetch_data.py` options

```bash
python fetch_data.py --years 8 --max-points 0   # 8 years, no cap on sampling points (largest pull)
python fetch_data.py --years 3 --max-points 150  # smaller, faster pull
```

- `--years` — how many years of history to fetch (measurement requests are chunked one calendar year at a time to stay within the API's row limits).
- `--max-points` — caps how many London sampling points are queried, so a demo run stays fast. Use `0` for no cap (this can be slow — Greater London typically has several hundred to a few thousand sampling points across rivers, groundwater, coastal/tidal and discharge monitoring).

Data is written to `data/london_sampling_points.parquet` and
`data/london_measurements.parquet`, which `app.py` reads on startup
(`.gitignore` excludes these — regenerate them locally rather than committing
them, since the archive updates regularly).

## How the API is used

The Water Quality Archive is a Linked-Data-API-style service that returns
JSON for reference entities (sampling points) and CSV for bulk data
(measurements). Two endpoints matter here:

**Sampling points**, filtered to a Greater London bounding box in British
National Grid (EPSG:27700) metres:

```
https://environment.data.gov.uk/water-quality/id/sampling-point.json
    ?min-easting=502000&max-easting=563000
    &min-northing=155000&max-northing=202000
    &_limit=2000&_offset=0
```

**Measurements**, filtered by sampling point and date range:

```
https://environment.data.gov.uk/water-quality/data/measurement.csv
    ?samplingPoint=TH-1234&samplingPoint=TH-5678&...
    &startDate=2023-01-01&endDate=2023-12-31&_limit=50000
```

`fetch_data.py` batches sampling points (25 per request) and pages one
calendar year at a time, so no single request tries to pull an unbounded
amount of data. Column names in the EA's CSV export can shift slightly
between exports, so `_clean_measurements()` matches on keywords
(`"samplingpoint" + "notation"`, `"determinand" + "label"`, etc.) instead of a
hardcoded header list, and normalises everything to a stable internal schema.

> **Note on the docs site:** the human-readable documentation pages under
> `environment.data.gov.uk/.../doc/` and `/view/` block automated crawling via
> `robots.txt`. The `/id/` and `/data/` endpoints used by this project are the
> actual public data API (JSON/CSV/RDF), which is what it's there for. See
> the Environment Agency's own published examples for the same endpoint
> patterns, e.g. the [Water Quality training exercise
> notes](https://catchmentbasedapproach.org/wp-content/uploads/2018/08/Water-Quality-OpenWIMS-Exercise-Answers.pdf).

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
