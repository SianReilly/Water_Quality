"""
fetch_data.py
=============
Pulls water-quality sampling points and measurements for Greater London from the
Environment Agency's Water Quality Archive API.

Official API reference (fetched directly from the live docs):
    https://environment.data.gov.uk/water-quality/view/doc/reference

Key facts from that reference that matter for this script:

* Sampling points:  GET {base}/id/sampling-point
    Filters: search={x} | lat={x}&long={y}&dist={d} | easting={x}&northing={y}&dist={d}
             | area={id} | subArea={id} | samplingPointType={id} | samplingPointStatus={open|closed}
    -- There is NO min-/max- bounding-box filter on this endpoint. Area filtering is
       radius-based (roughly a {d}km square/circle around a point) or by EA
       area/sub-area code. (An earlier version of this script incorrectly tried
       min-easting/max-easting params, which 404'd.)

* Measurements:     GET {base}/data/measurement  (or .csv / .json / .html suffix)
    Filters: lat/long/dist | easting/northing/dist | area | subArea | startDate & endDate
             | year | purpose | isComplianceSample | samplingPoint={id} | samplingPointType
             | sampledMaterialType | determinand={id} | determinandGroup={id} | _sorted
    View/paging modifiers: _view={compact|default|full}, _limit={n}, _offset={n}, _sort={prop}
    -- Default _limit is small (50); we set it explicitly.
    -- `_view=full` is required to get sample.samplingPoint.lat / .long directly on each
       measurement row, which saves us from re-projecting British National Grid
       easting/northing ourselves.
    -- The query APIs have a ~2 minute server-side timeout. For very large pulls the
       API offers an async /batch/measurement endpoint instead; this script avoids
       needing that by chunking requests one calendar year at a time and paging with
       _offset, which keeps each request small.

The API is public and meant to be queried programmatically (that's the point of a
data archive API) even though the human-readable *documentation* pages block
crawlers via robots.txt.

Run this as a script to (re)build a local cache under ./data/:

    python fetch_data.py --years 5 --radius-km 30

Then `app.py` (the Streamlit app) reads that cache instead of hitting the API on
every page load.
"""

from __future__ import annotations

import argparse
import io
import sys
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd
import requests

BASE = "https://environment.data.gov.uk/water-quality"
SAMPLING_POINT_URL = f"{BASE}/id/sampling-point.json"
MEASUREMENT_CSV_URL = f"{BASE}/data/measurement.csv"

DATA_DIR = Path(__file__).parent / "data"
SAMPLING_POINTS_FILE = DATA_DIR / "london_sampling_points.parquet"
MEASUREMENTS_FILE = DATA_DIR / "london_measurements.parquet"

HEADERS = {
    "User-Agent": "london-water-quality-dashboard/1.0 (streamlit demo; contact via github issue)"
}

# Centre of Greater London (Trafalgar Square) in WGS84. The API's `dist` radius
# filter is documented as "approximately {d}km ... may return those within a
# square ... rather than a true circle", so a single generous radius from the
# centre comfortably covers the GLA boundary (~30km corner-to-centre).
LONDON_CENTER = {"lat": 51.5074, "long": -0.1278}
DEFAULT_RADIUS_KM = 30

REQUEST_TIMEOUT = 100  # the API itself has a ~2 minute server-side timeout
PAGE_SIZE = 2000                 # sampling-point paging
MEASUREMENT_PAGE_SIZE = 5000     # measurement paging (kept well under the server timeout)
MAX_PAGES_PER_YEAR = 200         # safety valve: stop after this many pages (=1M rows) per year


@dataclass
class FetchConfig:
    years_back: int = 5
    radius_km: float = DEFAULT_RADIUS_KM
    sleep_between_requests: float = 0.2


# --------------------------------------------------------------------------- #
# Sampling points
# --------------------------------------------------------------------------- #

def fetch_london_sampling_points(radius_km: float = DEFAULT_RADIUS_KM) -> pd.DataFrame:
    """Page through the sampling-point endpoint, radius-filtered around central London.

    Returns a DataFrame with one row per sampling point: notation, label,
    lat, long, easting, northing, sampling point type, status.
    """
    rows = []
    offset = 0
    while True:
        params = {
            "lat": LONDON_CENTER["lat"],
            "long": LONDON_CENTER["long"],
            "dist": radius_km,
            "_view": "full",
            "_limit": PAGE_SIZE,
            "_offset": offset,
        }
        resp = requests.get(SAMPLING_POINT_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        if resp.status_code == 404:
            raise RuntimeError(
                f"404 from {resp.url} -- the API path or parameters may have changed. "
                "Check https://environment.data.gov.uk/water-quality/view/doc/reference"
            )
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        if isinstance(items, dict):  # a single-item response, just in case
            items = [items]
        if not items:
            break

        for item in items:
            rows.append(
                {
                    "notation": item.get("notation") or _notation_from_id(item.get("@id")),
                    "label": item.get("label"),
                    "comment": item.get("comment"),
                    "lat": item.get("lat"),
                    "long": item.get("long"),
                    "easting": item.get("easting"),
                    "northing": item.get("northing"),
                    "sampling_point_type": _safe_label(item.get("samplingPointType")),
                    "sampling_point_status": _safe_label(item.get("samplingPointStatus")),
                }
            )

        if len(items) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.1)

    df = pd.DataFrame(rows).dropna(subset=["notation"]).drop_duplicates(subset=["notation"])
    df = df.dropna(subset=["lat", "long"])
    return df.reset_index(drop=True)


def _notation_from_id(uri: str | None) -> str | None:
    """Fall back to parsing the notation out of a sampling point's @id URI,
    e.g. '.../id/sampling-point/AN-WOODTON' -> 'AN-WOODTON'."""
    if not uri:
        return None
    return uri.rstrip("/").rsplit("/", 1)[-1]


def _safe_label(value):
    if isinstance(value, dict):
        return value.get("label") or value.get("_value")
    if isinstance(value, list) and value:
        return _safe_label(value[0])
    return value


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #

def fetch_measurements(
    start_date: date,
    end_date: date,
    radius_km: float = DEFAULT_RADIUS_KM,
    page_size: int = MEASUREMENT_PAGE_SIZE,
    sleep_between_requests: float = 0.2,
) -> pd.DataFrame:
    """Fetch measurement records (one row per determinand result) for the London
    radius, between start_date and end_date inclusive.

    Chunked one calendar year at a time and paged with `_offset`, so no single
    request risks the API's ~2 minute timeout. `_view=full` is used so each row
    already carries the sampling point's lat/long, avoiding a separate
    British-National-Grid reprojection step.
    """
    frames: list[pd.DataFrame] = []
    year_ranges = _year_ranges(start_date, end_date)

    for y_start, y_end in year_ranges:
        offset = 0
        page = 0
        year_rows = 0
        while page < MAX_PAGES_PER_YEAR:
            params = {
                "lat": LONDON_CENTER["lat"],
                "long": LONDON_CENTER["long"],
                "dist": radius_km,
                "startDate": y_start.isoformat(),
                "endDate": y_end.isoformat(),
                "_view": "full",
                "_limit": page_size,
                "_offset": offset,
            }
            try:
                resp = requests.get(
                    MEASUREMENT_CSV_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT
                )
                if resp.status_code == 404:
                    raise RuntimeError(
                        f"404 from {resp.url} -- check "
                        "https://environment.data.gov.uk/water-quality/view/doc/reference"
                    )
                resp.raise_for_status()
            except requests.RequestException as exc:
                print(f"  [warn] request failed for {y_start}..{y_end} offset={offset}: {exc}", file=sys.stderr)
                break

            if not resp.text.strip():
                break

            df = pd.read_csv(io.StringIO(resp.text))
            if df.empty:
                break

            frames.append(df)
            year_rows += len(df)
            print(
                f"  {y_start.year}: fetched {year_rows:,} rows so far (page {page + 1})",
                end="\r",
                file=sys.stderr,
            )

            if len(df) < page_size:
                break  # last page

            offset += page_size
            page += 1
            time.sleep(sleep_between_requests)

        print("", file=sys.stderr)
        if page >= MAX_PAGES_PER_YEAR:
            print(
                f"  [warn] hit the {MAX_PAGES_PER_YEAR}-page safety cap for {y_start.year}; "
                "results for this year may be incomplete. Narrow the radius or date range "
                "if you need the full set.",
                file=sys.stderr,
            )

    if not frames:
        return pd.DataFrame()

    raw = pd.concat(frames, ignore_index=True)
    return _clean_measurements(raw)


def _year_ranges(start_date: date, end_date: date) -> list[tuple[date, date]]:
    ranges = []
    y = start_date.year
    while y <= end_date.year:
        y_start = max(start_date, date(y, 1, 1))
        y_end = min(end_date, date(y, 12, 31))
        ranges.append((y_start, y_end))
        y += 1
    return ranges


# Expected CSV headers under `_view=full`, per the documented property paths.
# Matched case-insensitively, exact match first; falls back to keyword search
# for the (fairly likely) case that the live export differs slightly.
EXPECTED_COLUMNS = {
    "sampling_point_label": "sample.samplingpoint.label",
    "sample_datetime": "sample.sampledatetime",
    "determinand_label": "determinand.label",
    "determinand_notation": "determinand.notation",
    "determinand_definition": "determinand.definition",
    "result": "result",
    "result_qualifier": "resultqualifier.notation",
    "unit": "determinand.unit.label",
    "lat": "sample.samplingpoint.lat",
    "long": "sample.samplingpoint.long",
    "easting": "sample.samplingpoint.easting",
    "northing": "sample.samplingpoint.northing",
    "is_compliance": "sample.iscompliancesample",
    "sample_purpose": "sample.purpose.label",
    "sampling_point_uri": "sample.samplingpoint",
}


def _clean_measurements(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise column names against the documented `_view=full` schema, with a
    keyword-based fallback in case the live export's headers differ slightly."""
    cols = {c.lower(): c for c in raw.columns}

    def find(*keywords, exclude: str | None = None):
        for lower, original in cols.items():
            if all(k in lower for k in keywords) and (exclude is None or exclude not in lower):
                return original
        return None

    out = pd.DataFrame()
    for target, expected_lower in EXPECTED_COLUMNS.items():
        source = cols.get(expected_lower)
        if source is None:
            # Fall back to keyword matching on the meaningful parts of the expected name
            keywords = [k for k in expected_lower.replace(".", " ").split() if k not in {"label"}]
            source = find(*keywords) if keywords else None
        out[target] = raw[source] if source and source in raw.columns else pd.NA

    # Derive a stable sampling-point notation from its URI if we have one
    if "sampling_point_uri" in out.columns:
        out["sampling_point_notation"] = out["sampling_point_uri"].apply(
            lambda u: _notation_from_id(u) if isinstance(u, str) else pd.NA
        )
    else:
        out["sampling_point_notation"] = pd.NA

    # If notation extraction failed everywhere, fall back to the label as a grouping key
    if out["sampling_point_notation"].isna().all():
        out["sampling_point_notation"] = out["sampling_point_label"]

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

def main():
    parser = argparse.ArgumentParser(description="Fetch London water-quality data into a local cache.")
    parser.add_argument("--years", type=int, default=5, help="How many years of history to pull (default 5).")
    parser.add_argument(
        "--radius-km",
        type=float,
        default=DEFAULT_RADIUS_KM,
        help=f"Search radius in km from central London (default {DEFAULT_RADIUS_KM}, "
        "chosen to cover Greater London -- the API filters by a square of "
        "roughly this half-width, not a strict bounding box).",
    )
    args = parser.parse_args()

    print(f"Fetching London sampling points (radius {args.radius_km}km) ...", file=sys.stderr)
    sp = fetch_london_sampling_points(radius_km=args.radius_km)
    print(f"  found {len(sp)} sampling points", file=sys.stderr)

    end = date.today()
    start = date(end.year - args.years, end.month, end.day)
    print(f"Fetching measurements from {start} to {end} ...", file=sys.stderr)
    ms = fetch_measurements(start, end, radius_km=args.radius_km)
    print(f"  fetched {len(ms)} measurement rows", file=sys.stderr)

    save_cache(sp, ms)
    print(f"Saved cache to {DATA_DIR}/", file=sys.stderr)


if __name__ == "__main__":
    main()
