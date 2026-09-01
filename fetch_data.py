"""
fetch_data.py
=============
Pulls water-quality sampling points and observations for Greater London from the
Environment Agency's Water Quality Archive API -- the NEW REST API (v1.3.1,
OpenAPI 3.1), documented interactively at:

    https://environment.data.gov.uk/water-quality/api-docs

This is a different, newer service than the old Linked-Data-API
(environment.data.gov.uk/water-quality/id/... , .../data/measurement.csv)
that appears in most older blog posts and tutorials -- that one now 404s.
Parameters below were confirmed directly against the live Swagger UI
"Try it out" panels (the docs pages themselves block automated fetching via
robots.txt, so this was checked by hand rather than scraped).

Confirmed endpoints and behaviour
----------------------------------
Base URL: https://environment.data.gov.uk/water-quality

GET  /sampling-point
    List sampling points. Confirmed response shape is a JSON-LD "hydra:Collection"
    with a `member` array; each member has `notation`, `prefLabel`, `altLabel`,
    `geometry.asWKT` (a WKT POINT, in whatever CRS the `Accept-Crs` header asked
    for), `samplingPointType`, `samplingPointStatus`, `region`, `area`, `subArea`.
    No CSV format was confirmed for this endpoint, so it's parsed as JSON-LD.
    Pagination: `skip` / `limit` (default limit 100).

POST /data/observation
    List/filter observations across multiple sampling points in one call.
    Confirmed query parameters (from the live "Parameters" panel):
        skip, limit                                  (pagination; limit default 250
                                                        for JSON-LD, up to 2500 for
                                                        CSV/JSONLINES)
        determinand, samplingPurpose, sampleMaterialType   (comma-separated codes)
        sampleSamplingNotation
        date | dateFrom & dateTo                     (ISO YYYY-MM-DD; date is
                                                        mutually exclusive with the
                                                        dateFrom/dateTo pair)
        complianceOnly                                (bool)
        easting, northing  |  latitude, longitude, radius   (radius-based geo search,
                                                        radius in km)
        precannedArea                                 ("area_type,area_notation")
        pointNotation, prefLabel, region, area, subArea,
        samplingPointStatus, samplingPointType
    Confirmed headers:
        Accept-Crs   -- e.g. http://www.opengis.net/def/crs/EPSG/0/4326 (WGS84
                        lat/long) or .../EPSG/0/27700 (British National Grid).
                        We request 4326 so geometry comes back as lon/lat and we
                        don't have to reproject BNG ourselves.
        CSV-Header   -- "present" to include a header row when Accept: text/csv.
        API-Version  -- confirmed example value "1".
    Confirmed request body: a bare GeoJSON `Polygon` object (`type`, `coordinates`,
    optional `bbox`) -- NOT wrapped in another object. This is optional: geographic
    filtering can instead be done entirely via the latitude/longitude/radius query
    parameters, which is what this script uses by default (simpler, and confirmed
    to exist). Polygon support is included as an opt-in for a tighter London shape.
    The endpoint's own description notes CSV/JSONLINES responses are capped at
    2500 rows per request; JSON-LD responses are capped at 250.

Because the exact JSON-LD shape of an *observation* record (as opposed to a
*sampling point* record, which was confirmed) wasn't directly confirmed, this
script requests CSV first (`Accept: text/csv`), which is explicitly documented
as a supported bulk format for this endpoint, and falls back to parsing the
JSON-LD `member` array with a tolerant, keyword-based field finder if CSV isn't
served. Either way, the first raw record fetched is dumped to
`data/_debug_first_observation.(csv|json)` so a parsing mismatch is easy to
diagnose from the cache alone, without needing another round of screenshots.

Run this as a script to (re)build a local cache under ./data/:

    python fetch_data.py --years 5 --radius-km 30
    python fetch_data.py --probe          # fetch a handful of rows and print
                                           # the raw response, for debugging

Then `app.py` (the Streamlit app) reads that cache instead of hitting the API
on every page load.
"""

from __future__ import annotations

import argparse
import io
import json
import re
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import requests

BASE = "https://environment.data.gov.uk/water-quality"
SAMPLING_POINT_LIST_URL = f"{BASE}/sampling-point"
OBSERVATION_URL = f"{BASE}/data/observation"

DATA_DIR = Path(__file__).parent / "data"
SAMPLING_POINTS_FILE = DATA_DIR / "london_sampling_points.parquet"
MEASUREMENTS_FILE = DATA_DIR / "london_measurements.parquet"
DEBUG_OBSERVATION_FILE_CSV = DATA_DIR / "_debug_first_observation.csv"
DEBUG_OBSERVATION_FILE_JSON = DATA_DIR / "_debug_first_observation.json"

CRS_WGS84 = "http://www.opengis.net/def/crs/EPSG/0/4326"

COMMON_HEADERS = {
    "User-Agent": "london-water-quality-dashboard/1.0 (streamlit demo; contact via github issue)",
    "Accept-Crs": CRS_WGS84,
    "API-Version": "1",
}

LONDON_CENTER = {"lat": 51.5074, "long": -0.1278}
DEFAULT_RADIUS_KM = 30

REQUEST_TIMEOUT = 100
SAMPLING_POINT_PAGE_SIZE = 100     # this endpoint's confirmed default limit
OBSERVATION_PAGE_SIZE_CSV = 2500   # confirmed cap for CSV/JSONLINES
OBSERVATION_PAGE_SIZE_JSONLD = 250  # confirmed cap for JSON-LD
MAX_PAGES_PER_YEAR = 400           # safety valve


@dataclass
class FetchConfig:
    years_back: int = 5
    radius_km: float = DEFAULT_RADIUS_KM
    sleep_between_requests: float = 0.2


def _raise_for_status_verbose(resp: requests.Response):
    """Like resp.raise_for_status(), but prints the API's own error detail
    (this API returns a structured `{"detail": [...]}` body on 422) so a bad
    parameter is diagnosable from one failed run instead of a blind traceback."""
    if resp.status_code >= 400:
        detail = resp.text[:2000]
        try:
            detail = json.dumps(resp.json(), indent=2)[:2000]
        except ValueError:
            pass
        raise RuntimeError(f"{resp.status_code} from {resp.url}\n{detail}")


# --------------------------------------------------------------------------- #
# Sampling points  (GET /sampling-point, JSON-LD)
# --------------------------------------------------------------------------- #

_WKT_POINT_RE = re.compile(r"POINT\s*\(\s*([-\d.]+)\s+([-\d.]+)\s*\)")


def _parse_wkt_point(wkt: str | None) -> tuple[float | None, float | None]:
    """Parse 'POINT(x y) <crs-uri>' -> (x, y). With Accept-Crs set to EPSG:4326,
    x=longitude, y=latitude."""
    if not wkt:
        return None, None
    m = _WKT_POINT_RE.search(wkt)
    if not m:
        return None, None
    return float(m.group(1)), float(m.group(2))


def _label(d) -> str | None:
    if isinstance(d, dict):
        return d.get("prefLabel") or d.get("label")
    return None


def _notation(d) -> str | None:
    if isinstance(d, dict):
        return d.get("notation")
    return None


def fetch_london_sampling_points(radius_km: float = DEFAULT_RADIUS_KM) -> pd.DataFrame:
    """Page through GET /sampling-point, radius-filtered around central London."""
    rows = []
    skip = 0
    while True:
        params = {
            "latitude": LONDON_CENTER["lat"],
            "longitude": LONDON_CENTER["long"],
            "radius": radius_km,
            "skip": skip,
            "limit": SAMPLING_POINT_PAGE_SIZE,
        }
        resp = requests.get(
            SAMPLING_POINT_LIST_URL, params=params, headers=COMMON_HEADERS, timeout=REQUEST_TIMEOUT
        )
        _raise_for_status_verbose(resp)
        payload = resp.json()
        members = payload.get("member", [])
        if not members:
            break

        for m in members:
            lon, lat = _parse_wkt_point((m.get("geometry") or {}).get("asWKT"))
            rows.append(
                {
                    "notation": m.get("notation"),
                    "label": m.get("prefLabel") or m.get("altLabel"),
                    "lat": lat,
                    "long": lon,
                    "sampling_point_type": _label(m.get("samplingPointType")),
                    "sampling_point_status": _label(m.get("samplingPointStatus")),
                    "region": _label(m.get("region")),
                    "area": _label(m.get("area")),
                }
            )

        if len(members) < SAMPLING_POINT_PAGE_SIZE:
            break
        skip += SAMPLING_POINT_PAGE_SIZE
        time.sleep(0.1)

    df = pd.DataFrame(rows).dropna(subset=["notation"]).drop_duplicates(subset=["notation"])
    df = df.dropna(subset=["lat", "long"])
    return df.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Observations  (POST /data/observation)
# --------------------------------------------------------------------------- #

def _fetch_observation_page(
    y_start: date, y_end: date, radius_km: float, skip: int, limit: int, fmt: str
) -> requests.Response:
    params = {
        "latitude": LONDON_CENTER["lat"],
        "longitude": LONDON_CENTER["long"],
        "radius": radius_km,
        "dateFrom": y_start.isoformat(),
        "dateTo": y_end.isoformat(),
        "skip": skip,
        "limit": limit,
    }
    headers = dict(COMMON_HEADERS)
    headers["Accept"] = "text/csv" if fmt == "csv" else "application/ld+json"
    if fmt == "csv":
        headers["CSV-Header"] = "present"

    # No geometry body needed -- latitude/longitude/radius above already filter.
    resp = requests.post(OBSERVATION_URL, params=params, headers=headers, timeout=REQUEST_TIMEOUT)
    return resp


def fetch_measurements(
    start_date: date,
    end_date: date,
    radius_km: float = DEFAULT_RADIUS_KM,
    sleep_between_requests: float = 0.2,
) -> pd.DataFrame:
    """Fetch observation records for the London radius, between start_date and
    end_date inclusive, chunked one calendar year at a time and paged with
    `skip`/`limit`. Tries CSV first (documented 2500-row cap); falls back to
    JSON-LD (250-row cap) if CSV isn't actually served for this endpoint.
    """
    fmt = "csv"
    page_size = OBSERVATION_PAGE_SIZE_CSV
    frames: list[pd.DataFrame] = []
    debug_saved = False

    year_ranges = _year_ranges(start_date, end_date)

    for y_start, y_end in year_ranges:
        skip = 0
        page = 0
        year_rows = 0
        while page < MAX_PAGES_PER_YEAR:
            resp = _fetch_observation_page(y_start, y_end, radius_km, skip, page_size, fmt)

            if resp.status_code >= 400:
                detail = resp.text[:1000]
                if fmt == "csv":
                    # CSV may not actually be implemented for this endpoint --
                    # switch to JSON-LD and retry the whole fetch.
                    print(
                        f"  [info] CSV request failed ({resp.status_code}); "
                        f"falling back to JSON-LD. Server said: {detail}",
                        file=sys.stderr,
                    )
                    fmt = "json"
                    page_size = OBSERVATION_PAGE_SIZE_JSONLD
                    skip = 0
                    continue
                print(f"  [warn] request failed for {y_start}..{y_end} skip={skip}: {detail}", file=sys.stderr)
                break

            content_type = resp.headers.get("Content-Type", "")

            if fmt == "csv" and "csv" not in content_type and "text/csv" not in content_type:
                # Server accepted the request but didn't actually give us CSV
                # (some FastAPI setups ignore an unsupported Accept and return
                # JSON anyway). Treat this page as JSON-LD instead.
                page_df, n = _parse_observation_jsonld_page(resp, debug=not debug_saved)
            elif fmt == "csv":
                page_df, n = _parse_observation_csv_page(resp, debug=not debug_saved)
            else:
                page_df, n = _parse_observation_jsonld_page(resp, debug=not debug_saved)

            debug_saved = True

            if n == 0:
                break

            frames.append(page_df)
            year_rows += n
            print(f"  {y_start.year}: fetched {year_rows:,} rows so far (page {page + 1})", end="\r", file=sys.stderr)

            if n < page_size:
                break  # last page

            skip += page_size
            page += 1
            time.sleep(sleep_between_requests)

        print("", file=sys.stderr)
        if page >= MAX_PAGES_PER_YEAR:
            print(
                f"  [warn] hit the {MAX_PAGES_PER_YEAR}-page safety cap for {y_start.year}; "
                "results for this year may be incomplete.",
                file=sys.stderr,
            )

    if not frames:
        return pd.DataFrame()

    raw = pd.concat(frames, ignore_index=True)
    return _finalise_measurements(raw)


def _parse_observation_csv_page(resp: requests.Response, debug: bool) -> tuple[pd.DataFrame, int]:
    if debug:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_OBSERVATION_FILE_CSV.write_text(resp.text[:5000])
    if not resp.text.strip():
        return pd.DataFrame(), 0
    df = pd.read_csv(io.StringIO(resp.text))
    return df, len(df)


def _parse_observation_jsonld_page(resp: requests.Response, debug: bool) -> tuple[pd.DataFrame, int]:
    payload = resp.json()
    if debug:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        DEBUG_OBSERVATION_FILE_JSON.write_text(json.dumps(payload, indent=2)[:5000])
    members = payload.get("member", [])
    if not members:
        return pd.DataFrame(), 0

    rows = [_flatten_observation_member(m) for m in members]
    return pd.DataFrame(rows), len(rows)


# Candidate key names for each logical field, tried in order, at the top level
# of an observation record and (if not found) one level deep inside common
# container keys. This is deliberately tolerant since the exact JSON-LD
# observation schema wasn't directly confirmed (only the sampling-point and
# sampling schemas were) -- see the module docstring.
_OBS_FIELD_CANDIDATES = {
    "determinand_label": ["observedProperty.prefLabel", "determinand.prefLabel", "determinand.label"],
    "determinand_notation": ["observedProperty.notation", "determinand.notation"],
    "unit": ["hasResult.unit.prefLabel", "hasResult.unit", "unit.prefLabel", "unit"],
    "result": ["hasResult.value", "hasResult.numericValue", "result.value", "result"],
    "sample_datetime": ["resultTime", "phenomenonTime", "resultDate", "startTime", "sampleDate"],
    "sampling_point_notation": [
        "hasFeatureOfInterest.notation",
        "madeBySampling.hasFeatureOfInterest.notation",
        "samplingPoint.notation",
    ],
    "sampling_point_label": [
        "hasFeatureOfInterest.prefLabel",
        "madeBySampling.hasFeatureOfInterest.prefLabel",
        "samplingPoint.prefLabel",
    ],
    "sampling_point_wkt": [
        "hasFeatureOfInterest.geometry.asWKT",
        "madeBySampling.hasFeatureOfInterest.geometry.asWKT",
        "samplingPoint.geometry.asWKT",
    ],
}


def _dig(d: dict, dotted_path: str):
    cur = d
    for part in dotted_path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


def _flatten_observation_member(m: dict) -> dict:
    out = {}
    for target, candidates in _OBS_FIELD_CANDIDATES.items():
        value = None
        for path in candidates:
            value = _dig(m, path)
            if value is not None:
                break
        out[target] = value
    return out


def _year_ranges(start_date: date, end_date: date) -> list[tuple[date, date]]:
    ranges = []
    y = start_date.year
    while y <= end_date.year:
        y_start = max(start_date, date(y, 1, 1))
        y_end = min(end_date, date(y, 12, 31))
        ranges.append((y_start, y_end))
        y += 1
    return ranges


def _finalise_measurements(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise whichever shape we ended up with (CSV or flattened JSON-LD)
    into a stable internal schema, matching on keywords in column names since
    the live CSV header set wasn't directly confirmed."""
    cols = {c.lower(): c for c in raw.columns}

    def find(*keywords, exclude: str | None = None):
        for lower, original in cols.items():
            if all(k in lower for k in keywords) and (exclude is None or exclude not in lower):
                return original
        return None

    def col(name, *fallback_keyword_sets, exclude: str | None = None):
        """fallback_keyword_sets is a list of keyword-tuples, tried in order
        (e.g. col('x', ('date',), ('time',)) tries a 'date' column, then a
        'time' column)."""
        if name in raw.columns:
            return raw[name]
        for keywords in fallback_keyword_sets:
            source = find(*keywords, exclude=exclude)
            if source:
                return raw[source]
        return pd.Series([pd.NA] * len(raw))

    out = pd.DataFrame()
    out["sampling_point_notation"] = col("sampling_point_notation", ("sampling", "notation"))
    out["sampling_point_label"] = col("sampling_point_label", ("sampling", "label"))
    out["sample_datetime"] = col("sample_datetime", ("date",), ("time",))
    out["determinand_label"] = col("determinand_label", ("determinand", "label"))
    out["result"] = col("result", ("result",), exclude="qualif")
    out["unit"] = col("unit", ("unit",))

    # Lat/long: either already columns (from JSON-LD WKT parsing below) or
    # embedded in a WKT point column from the CSV export.
    if "sampling_point_wkt" in raw.columns:
        lon_lat = raw["sampling_point_wkt"].apply(_parse_wkt_point)
        out["long"] = lon_lat.apply(lambda t: t[0])
        out["lat"] = lon_lat.apply(lambda t: t[1])
    else:
        wkt_col = find("wkt") or find("geometry")
        if wkt_col:
            lon_lat = raw[wkt_col].apply(_parse_wkt_point)
            out["long"] = lon_lat.apply(lambda t: t[0])
            out["lat"] = lon_lat.apply(lambda t: t[1])
        else:
            out["lat"] = col("lat", "lat")
            out["long"] = col("long", "long")

    out["result_numeric"] = pd.to_numeric(out["result"], errors="coerce")
    out["lat"] = pd.to_numeric(out["lat"], errors="coerce")
    out["long"] = pd.to_numeric(out["long"], errors="coerce")
    out["sample_datetime"] = pd.to_datetime(out["sample_datetime"], errors="coerce", utc=True)
    out["year"] = out["sample_datetime"].dt.year
    out["month"] = out["sample_datetime"].dt.tz_localize(None).dt.to_period("M").astype(str)

    out = out.dropna(subset=["sampling_point_notation", "sample_datetime"])
    return out.reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Cache I/O
# --------------------------------------------------------------------------- #

def save_cache(sampling_points: pd.DataFrame, measurements: pd.DataFrame) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    sampling_points.to_parquet(SAMPLING_POINTS_FILE, index=False)
    measurements.to_parquet(MEASUREMENTS_FILE, index=False)


def load_cache() -> tuple[pd.DataFrame, pd.DataFrame]:
    sp = pd.read_parquet(SAMPLING_POINTS_FILE) if SAMPLING_POINTS_FILE.exists() else pd.DataFrame()
    ms = pd.read_parquet(MEASUREMENTS_FILE) if MEASUREMENTS_FILE.exists() else pd.DataFrame()
    return sp, ms


def cache_exists() -> bool:
    return SAMPLING_POINTS_FILE.exists() and MEASUREMENTS_FILE.exists()


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #

def probe(radius_km: float = DEFAULT_RADIUS_KM):
    """Fetch a handful of sampling points and observations and print the raw
    response, to sanity-check the API shape before committing to a full pull."""
    print(">>> GET /sampling-point (first page)", file=sys.stderr)
    resp = requests.get(
        SAMPLING_POINT_LIST_URL,
        params={"latitude": LONDON_CENTER["lat"], "longitude": LONDON_CENTER["long"], "radius": radius_km, "limit": 3},
        headers=COMMON_HEADERS,
        timeout=REQUEST_TIMEOUT,
    )
    print(resp.status_code, resp.url, file=sys.stderr)
    print(json.dumps(resp.json(), indent=2)[:3000] if resp.ok else resp.text[:2000])

    print("\n>>> POST /data/observation (first page, CSV)", file=sys.stderr)
    resp = _fetch_observation_page(date.today().replace(year=date.today().year - 1), date.today(), radius_km, 0, 5, "csv")
    print(resp.status_code, resp.url, "content-type:", resp.headers.get("Content-Type"), file=sys.stderr)
    print(resp.text[:3000])


def main():
    parser = argparse.ArgumentParser(description="Fetch London water-quality data into a local cache.")
    parser.add_argument("--years", type=int, default=5, help="How many years of history to pull (default 5).")
    parser.add_argument(
        "--radius-km",
        type=float,
        default=DEFAULT_RADIUS_KM,
        help=f"Search radius in km from central London (default {DEFAULT_RADIUS_KM}).",
    )
    parser.add_argument(
        "--probe",
        action="store_true",
        help="Fetch a handful of records and print the raw API response, then exit (no cache written).",
    )
    args = parser.parse_args()

    if args.probe:
        probe(radius_km=args.radius_km)
        return

    print(f"Fetching London sampling points (radius {args.radius_km}km) ...", file=sys.stderr)
    sp = fetch_london_sampling_points(radius_km=args.radius_km)
    print(f"  found {len(sp)} sampling points", file=sys.stderr)

    end = date.today()
    start = date(end.year - args.years, end.month, end.day)
    print(f"Fetching observations from {start} to {end} ...", file=sys.stderr)
    ms = fetch_measurements(start, end, radius_km=args.radius_km)
    print(f"  fetched {len(ms)} observation rows", file=sys.stderr)
    if ms.empty:
        print(
            "  [warn] no observations parsed. Check data/_debug_first_observation.(csv|json) "
            "to see the raw shape returned by the API, and adjust _OBS_FIELD_CANDIDATES / "
            "_finalise_measurements in fetch_data.py to match. You can also run "
            "`python fetch_data.py --probe` for a smaller, faster look.",
            file=sys.stderr,
        )

    save_cache(sp, ms)
    print(f"Saved cache to {DATA_DIR}/", file=sys.stderr)


if __name__ == "__main__":
    main()
