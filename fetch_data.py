"""
fetch_data.py
=============
Pulls water-quality sampling points and measurements for Greater London from the
Environment Agency's Water Quality Archive API (the same open, LDA-style API that
sits behind https://environment.data.gov.uk/water-quality/).

API reference (Environment Agency, Open Government Licence):
    Landing page : https://environment.data.gov.uk/water-quality/view/landing
    Docs         : https://environment.data.gov.uk/water-quality/view/doc/reference
    Sampling pts : https://environment.data.gov.uk/water-quality/id/sampling-point
    Measurements : https://environment.data.gov.uk/water-quality/data/measurement

The API returns JSON (sampling points) and CSV (measurements). Note the doc/*.html
pages block automated crawlers via robots.txt, but the /id and /data JSON/CSV
endpoints are the actual public data API and are meant to be queried
programmatically -- that's the whole point of the service.

Run this as a script to (re)build a local cache under ./data/:

    python fetch_data.py --years 5 --max-points 400

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

# Greater London bounding box, British National Grid (EPSG:27700) metres.
# Comfortably covers the GLA boundary (Heathrow in the west to Romford/Havering
# in the east, Enfield in the north to Croydon in the south) with a small buffer.
LONDON_BBOX = {
    "min_easting": 502000,
    "max_easting": 563000,
    "min_northing": 155000,
    "max_northing": 202000,
}

REQUEST_TIMEOUT = 60
PAGE_SIZE = 2000          # sampling-point paging
SP_CHUNK_SIZE = 25        # how many samplingPoint= params per measurement request
MEASUREMENT_LIMIT = 50000  # per-request row cap requested from the API


@dataclass
class FetchConfig:
    years_back: int = 5
    max_points: int | None = 400   # cap for a manageable demo; set None for "all"
    sleep_between_requests: float = 0.2


# --------------------------------------------------------------------------- #
# Sampling points
# --------------------------------------------------------------------------- #

def fetch_london_sampling_points() -> pd.DataFrame:
    """Page through the sampling-point endpoint for the London bounding box.

    Returns a DataFrame with one row per sampling point: notation, label,
    lat, long, easting, northing, sampling point type.
    """
    rows = []
    offset = 0
    while True:
        params = {
            "min-easting": LONDON_BBOX["min_easting"],
            "max-easting": LONDON_BBOX["max_easting"],
            "min-northing": LONDON_BBOX["min_northing"],
            "max-northing": LONDON_BBOX["max_northing"],
            "_limit": PAGE_SIZE,
            "_offset": offset,
        }
        resp = requests.get(SAMPLING_POINT_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        payload = resp.json()
        items = payload.get("items", [])
        if not items:
            break

        for item in items:
            rows.append(
                {
                    "notation": item.get("notation"),
                    "label": item.get("label"),
                    "lat": item.get("lat"),
                    "long": item.get("long"),
                    "easting": item.get("easting"),
                    "northing": item.get("northing"),
                    "sampling_point_type": _safe_label(item.get("samplingPointType")),
                }
            )

        if len(items) < PAGE_SIZE:
            break
        offset += PAGE_SIZE
        time.sleep(0.1)

    df = pd.DataFrame(rows).dropna(subset=["notation"]).drop_duplicates(subset=["notation"])
    df = df.dropna(subset=["lat", "long"])
    return df.reset_index(drop=True)


def _safe_label(value):
    if isinstance(value, dict):
        return value.get("label") or value.get("_value")
    if isinstance(value, list) and value:
        return _safe_label(value[0])
    return value


# --------------------------------------------------------------------------- #
# Measurements
# --------------------------------------------------------------------------- #

def _chunk(seq, size):
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


def fetch_measurements(
    sampling_point_codes: list[str],
    start_date: date,
    end_date: date,
    limit: int = MEASUREMENT_LIMIT,
    sleep_between_requests: float = 0.2,
) -> pd.DataFrame:
    """Fetch measurement records (one row per determinand result) for a list of
    sampling point notations, between start_date and end_date inclusive.

    The API is queried in small batches of sampling points to stay under URL
    length limits, and is chunked one calendar year at a time so no single
    request tries to pull an unbounded amount of data.
    """
    frames: list[pd.DataFrame] = []
    year_ranges = _year_ranges(start_date, end_date)

    total_batches = len(list(_chunk(sampling_point_codes, SP_CHUNK_SIZE))) * len(year_ranges)
    done = 0

    for code_batch in _chunk(sampling_point_codes, SP_CHUNK_SIZE):
        for y_start, y_end in year_ranges:
            params = [("samplingPoint", c) for c in code_batch]
            params += [
                ("startDate", y_start.isoformat()),
                ("endDate", y_end.isoformat()),
                ("_limit", limit),
            ]
            try:
                resp = requests.get(
                    MEASUREMENT_CSV_URL, params=params, headers=HEADERS, timeout=REQUEST_TIMEOUT
                )
                resp.raise_for_status()
                if resp.text.strip():
                    df = pd.read_csv(io.StringIO(resp.text))
                    if len(df) == limit:
                        print(
                            f"  [warn] batch hit the {limit}-row request cap for "
                            f"{y_start}..{y_end}; some results may be missing. "
                            f"Consider narrowing the date range or sampling points.",
                            file=sys.stderr,
                        )
                    if not df.empty:
                        frames.append(df)
            except requests.RequestException as exc:
                print(f"  [warn] request failed for batch starting {code_batch[0]}: {exc}", file=sys.stderr)

            done += 1
            print(f"  fetched batch {done}/{total_batches}", end="\r", file=sys.stderr)
            time.sleep(sleep_between_requests)

    print("", file=sys.stderr)
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


def _clean_measurements(raw: pd.DataFrame) -> pd.DataFrame:
    """Normalise column names. The EA CSV export flattens nested LDA properties
    with dots (e.g. 'sample.samplingPoint.notation'); the exact header set can
    vary slightly by export, so we match on keywords rather than hardcoding a
    fixed schema.
    """
    cols = {c.lower(): c for c in raw.columns}

    def find(*keywords, exclude: str | None = None):
        """Return the original column name whose lowercased header contains all
        `keywords`, and does not contain `exclude` (if given)."""
        for lower, original in cols.items():
            if all(k in lower for k in keywords) and (exclude is None or exclude not in lower):
                return original
        return None

    result_col = find("result", exclude="qualif") or find("value") or find("concentration")
    determinand_label_col = find("determinand", "label") or find("determinand", "def") or find("determinand")

    mapping = {
        "sampling_point_notation": find("samplingpoint", "notation") or find("smpt_code") or find("samplingpoint", "code"),
        "sampling_point_label": find("samplingpoint", "label") or find("smpt_name"),
        "sample_datetime": find("sampledatetime") or find("sample_datetime") or find("date"),
        "determinand_label": determinand_label_col,
        "determinand_notation": find("determinand", "notation") or find("detcode"),
        "result": result_col,
        "result_qualifier": find("qualifier") or find("less_than"),
        "unit": find("unit"),
        "easting": find("easting"),
        "northing": find("northing"),
        "is_compliance": find("compliance"),
        "sample_purpose": find("purpose"),
    }

    out = pd.DataFrame()
    for target, source in mapping.items():
        if source and source in raw.columns:
            out[target] = raw[source]
        else:
            out[target] = pd.NA

    out["result_numeric"] = pd.to_numeric(out["result"], errors="coerce")
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
        "--max-points",
        type=int,
        default=400,
        help="Cap the number of sampling points queried, for a manageable demo run (default 400, use 0 for no cap).",
    )
    args = parser.parse_args()

    print("Fetching London sampling points ...", file=sys.stderr)
    sp = fetch_london_sampling_points()
    print(f"  found {len(sp)} sampling points in the London bounding box", file=sys.stderr)

    if args.max_points and len(sp) > args.max_points:
        # Favour river/freshwater points if the type is available, otherwise just cap.
        sp = sp.head(args.max_points)
        print(f"  capped to {args.max_points} sampling points for this run", file=sys.stderr)

    end = date.today()
    start = date(end.year - args.years, end.month, end.day)
    print(f"Fetching measurements from {start} to {end} ...", file=sys.stderr)
    ms = fetch_measurements(sp["notation"].tolist(), start, end)
    print(f"  fetched {len(ms)} measurement rows", file=sys.stderr)

    save_cache(sp, ms)
    print(f"Saved cache to {DATA_DIR}/", file=sys.stderr)


if __name__ == "__main__":
    main()
