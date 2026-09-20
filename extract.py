#!/usr/bin/env python3
"""
Extract Utah vehicle registration counts by fuel type from source XLSX files.

Outputs:
  data/registrations_by_county.csv  (2015-2025)
  data/registrations_by_county.json
  data/registrations_by_city.csv    (2018-2025, city-level fuel tables only)
  data/registrations_by_city.json
  data/meta.json

Run:
  pip install openpyxl
  python extract.py
"""

import csv
import io
import json
import pathlib
import re
import zipfile
from typing import Iterator

import openpyxl
from openpyxl.worksheet.worksheet import Worksheet

# ---------------------------------------------------------------------------
# Fuel mapping: raw label (stripped) → (canonical fuel, ev_class)
# Raise on any label not found here — no silent fall-through.
# ---------------------------------------------------------------------------
FUEL_MAP: dict[str, tuple[str, str]] = {
    # BEV
    "Electric": ("BEV", "BEV"),
    "ELECTRIC": ("BEV", "BEV"),
    # PHEV
    "Plug In Hybrid": ("PHEV", "PHEV"),
    "Plug-in Hybrid": ("PHEV", "PHEV"),
    # Conventional hybrid
    "Hybrid": ("Hybrid", "Hybrid"),
    "HYBRID": ("Hybrid", "Hybrid"),
    # Gasoline
    "Gasoline": ("Gasoline", "Non-electrified"),
    "GASOLINE": ("Gasoline", "Non-electrified"),
    # Diesel
    "Diesel": ("Diesel", "Non-electrified"),
    "DIESEL": ("Diesel", "Non-electrified"),
    # Flex fuel
    "Flexible": ("Flex Fuel", "Non-electrified"),
    "FLEXIBLE": ("Flex Fuel", "Non-electrified"),
    # Natural gas (all variants)
    "Natural Gas": ("Natural Gas", "Non-electrified"),
    "NATURAL GAS": ("Natural Gas", "Non-electrified"),
    "Compressed Natural Gas": ("Natural Gas", "Non-electrified"),
    "Liquefied Natural Gas": ("Natural Gas", "Non-electrified"),
    # Propane / LPG
    "Propane": ("Propane/LPG", "Non-electrified"),
    "PROPANE": ("Propane/LPG", "Non-electrified"),
    "Butane": ("Propane/LPG", "Non-electrified"),
    # Hydrogen
    "Hydrogen": ("Hydrogen", "Non-electrified"),
    # Converted (aftermarket alt-fuel)
    "Converted": ("Converted", "Non-electrified"),
    "CONVERTED": ("Converted", "Non-electrified"),
    # Other / noise
    "Alcohol": ("Other", "Non-electrified"),
    "Steam": ("Other", "Non-electrified"),
    "Other": ("Other", "Non-electrified"),
    "OTHER": ("Other", "Non-electrified"),
    "Unknown": ("Other", "Non-electrified"),
    "UNKNOWN": ("Other", "Non-electrified"),
}

# Row-A sentinel strings that mark the end of useful data
SKIP_PLACE = re.compile(
    r"^(STATE TOTAL|GRAND TOTAL|\*|City is determined|Converted and Propane)",
    re.I,
)


# ---------------------------------------------------------------------------
# Workbook loading
# ---------------------------------------------------------------------------

def load_workbook_safe(path: pathlib.Path) -> openpyxl.Workbook:
    """Load workbook, working around the corrupt docProps/core.xml in 2024."""
    try:
        return openpyxl.load_workbook(path, data_only=True)
    except TypeError:
        # Strip the offending entry and reload from a memory buffer
        src = zipfile.ZipFile(path)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as out:
            for item in src.infolist():
                if item.filename != "docProps/core.xml":
                    out.writestr(item, src.read(item.filename))
        buf.seek(0)
        return openpyxl.load_workbook(buf, data_only=True)


# ---------------------------------------------------------------------------
# Legacy parser: 2015-2017
# ---------------------------------------------------------------------------

LEGACY_SHEET = {
    "2015": "OH Reg by VType and Fuel Type",
    "2016": "On Highway Registrations by Fue",
    "2017": "OH Reg by VType and Fuel Type",
}

LEGACY_SKIP_PLACE = re.compile(r"^(GRAND|STATE TOTAL|\*|Converted)", re.I)
LEGACY_SKIP_VTYPE = re.compile(r"^TOTAL$", re.I)


def _to_int(v) -> int:
    if v is None or v == "" or v == ".":
        return 0
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().replace("'", "").replace(",", "")
    if s in (".", ""):
        return 0
    return int(s)


def parse_legacy(ws: Worksheet, year: int) -> Iterator[dict]:
    """
    2015-2017 layout:
      Row 7 = header (County | Vehicle Type | GASOLINE | ... | TOTAL)
      Rows 8+ = data; col A repeats the county on every row; zero = '.'
      Vehicle type 'TOTAL' rows are subtotals — skip them.
    """
    # Locate header row (contains TOTAL in the last occupied column)
    hdr_row = 7
    max_col = ws.max_column
    header = [str(ws.cell(hdr_row, c).value or "").strip() for c in range(1, max_col + 1)]
    # Fuel columns are between index 2 (col C) and the TOTAL column
    total_col = next(i for i, h in enumerate(header) if h.upper() == "TOTAL")
    fuel_cols = [(c + 1, header[c].strip()) for c in range(2, total_col)]

    for row in range(hdr_row + 1, ws.max_row + 1):
        place_raw = ws.cell(row, 1).value
        if place_raw is None:
            continue
        place_s = str(place_raw).strip()
        if not place_s or LEGACY_SKIP_PLACE.match(place_s):
            continue

        vtype_raw = ws.cell(row, 2).value
        if vtype_raw is None:
            continue
        vtype = str(vtype_raw).strip()
        if not vtype or LEGACY_SKIP_VTYPE.match(vtype):
            continue

        county, county_code = normalize_county(place_s)
        row_total = _to_int(ws.cell(row, total_col + 1).value)
        running = 0

        for col, fuel_raw in fuel_cols:
            count = _to_int(ws.cell(row, col).value)
            if count == 0:
                continue
            running += count
            fuel, ev_class = normalize_fuel(fuel_raw, year, row, col)
            yield {
                "year": year,
                "county": county,
                "county_code": county_code,
                "vehicle_type": vtype,
                "fuel_raw": fuel_raw,
                "fuel": fuel,
                "ev_class": ev_class,
                "count": count,
            }

        if row_total and running != row_total:
            raise ValueError(
                f"{year} legacy row {row} county={county!r} vtype={vtype!r}: "
                f"sum={running} but TOTAL={row_total}"
            )


# ---------------------------------------------------------------------------
# Modern parser: 2018-2025 Table 5 (county) and Table 6 (city)
# ---------------------------------------------------------------------------

MODERN_SKIP_PLACE = re.compile(
    r"^(STATE TOTAL|GRAND TOTAL|\*|City is determined|Converted and Propane)",
    re.I,
)


def parse_modern(
    ws: Worksheet, year: int, place_field: str
) -> Iterator[dict]:
    """
    2018+ layout:
      Header row: scanned for the TOTAL cell; fuel cols are between col B and TOTAL.
      Col A carries the place label only on the first row of each block (merged cells);
      we carry it forward until the next non-blank value.
      Zero cells are empty strings (not '.').
    """
    max_col = ws.max_column

    # Find the header row (contains a cell whose stripped value is exactly 'TOTAL')
    hdr_row = None
    for r in range(1, 20):
        for c in range(1, max_col + 1):
            v = ws.cell(r, c).value
            if isinstance(v, str) and v.strip().upper() == "TOTAL":
                hdr_row = r
                total_col = c
                break
        if hdr_row:
            break
    if hdr_row is None:
        raise ValueError(f"{year} {place_field}: could not find header row")

    header = [str(ws.cell(hdr_row, c).value or "").strip() for c in range(1, max_col + 1)]
    fuel_cols = [(c + 1, header[c].strip()) for c in range(2, total_col - 1)]

    current_place = None

    for row in range(hdr_row + 1, ws.max_row + 1):
        # Col A: carry forward, but reset on sentinel rows
        a = ws.cell(row, 1).value
        if a is not None and str(a).strip():
            a_str = str(a).strip()
            if MODERN_SKIP_PLACE.match(a_str):
                # Null out so subsequent blank-A sub-rows (e.g. STATE TOTAL breakdown)
                # don't get attributed to the previous county/city.
                current_place = None
                continue
            current_place = a_str
        if current_place is None:
            continue

        vtype_raw = ws.cell(row, 2).value
        if vtype_raw is None or not str(vtype_raw).strip():
            continue
        vtype = str(vtype_raw).strip()

        if place_field == "county":
            place, county_code = normalize_county(current_place)
        else:
            place = current_place  # city name verbatim
            county_code = None

        row_total_cell = ws.cell(row, total_col).value
        row_total = _to_int(row_total_cell)
        running = 0

        for col, fuel_raw in fuel_cols:
            if not fuel_raw:
                continue
            count = _to_int(ws.cell(row, col).value)
            if count == 0:
                continue
            running += count
            fuel, ev_class = normalize_fuel(fuel_raw, year, row, col)
            rec = {
                "year": year,
                "vehicle_type": vtype,
                "fuel_raw": fuel_raw,
                "fuel": fuel,
                "ev_class": ev_class,
                "count": count,
            }
            if place_field == "county":
                rec["county"] = place
                rec["county_code"] = county_code
            else:
                rec["city"] = place
            yield rec

        if row_total and running != row_total:
            raise ValueError(
                f"{year} modern {place_field} row {row} place={current_place!r} "
                f"vtype={vtype!r}: sum={running} but TOTAL={row_total}"
            )


# ---------------------------------------------------------------------------
# Normalizers
# ---------------------------------------------------------------------------

COUNTY_RE = re.compile(r"^(\d+)\s*-\s*(.+)$")


def normalize_county(raw: str) -> tuple[str, int | None]:
    """Return (county_name, county_code). Handles 'NN - NAME' and bare 'OUT OF STATE'."""
    s = raw.strip()
    m = COUNTY_RE.match(s)
    if m:
        code = int(m.group(1))
        name = m.group(2).strip()
        return name, code
    # Legacy 2015 format: bare 'OUT OF STATE' without a code prefix
    if s.upper() == "OUT OF STATE":
        return "OUT OF STATE", 99
    return s, None


def normalize_fuel(raw: str, year: int, row: int, col: int) -> tuple[str, str]:
    """Map a stripped fuel label to (canonical fuel, ev_class). Raises on unknown."""
    key = raw.strip()
    if key not in FUEL_MAP:
        raise ValueError(
            f"Unknown fuel label {key!r} at year={year} row={row} col={col}. "
            "Add it to FUEL_MAP before running again."
        )
    return FUEL_MAP[key]


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

XLSX_DIR = pathlib.Path(__file__).parent
DATA_DIR = XLSX_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

COUNTY_FIELDS = ["year", "county", "county_code", "vehicle_type", "fuel_raw", "fuel", "ev_class", "count"]
CITY_FIELDS   = ["year", "city", "vehicle_type", "fuel_raw", "fuel", "ev_class", "count"]


def collect() -> tuple[list[dict], list[dict]]:
    county_rows: list[dict] = []
    city_rows: list[dict] = []

    years_vocab: dict[str, list[str]] = {}

    for xlsx in sorted(XLSX_DIR.glob("20[12][0-9]registrations.xlsx")):
        year = int(xlsx.stem[:4])
        print(f"  {xlsx.name} ...", end=" ", flush=True)
        wb = load_workbook_safe(xlsx)

        if year <= 2017:
            sheet_name = LEGACY_SHEET[str(year)]
            rows = list(parse_legacy(wb[sheet_name], year))
            county_rows.extend(rows)
            vocab = sorted({r["fuel_raw"] for r in rows})
        else:
            # County
            ws5 = wb["Table 5"]
            rows5 = list(parse_modern(ws5, year, "county"))
            county_rows.extend(rows5)

            # City
            ws6 = wb["Table 6"]
            rows6 = list(parse_modern(ws6, year, "city"))
            city_rows.extend(rows6)

            vocab = sorted({r["fuel_raw"] for r in rows5})

        years_vocab[str(year)] = vocab
        print(f"county {len(rows5 if year>2017 else rows):,}" +
              (f"  city {len(rows6):,}" if year > 2017 else ""))

    county_rows.sort(key=lambda r: (r["year"], r["county"], r["vehicle_type"], r["fuel"]))
    city_rows.sort(key=lambda r: (r["year"], r["city"], r["vehicle_type"], r["fuel"]))

    return county_rows, city_rows, years_vocab


def validate(county_rows: list[dict], city_rows: list[dict]) -> None:
    print("\nValidating …")
    years = {r["year"] for r in county_rows}
    assert years == set(range(2015, 2026)), f"Missing county years: {set(range(2015,2026))-years}"
    city_years = {r["year"] for r in city_rows}
    assert city_years == set(range(2018, 2026)), f"Missing city years: {set(range(2018,2026))-city_years}"

    counties_per_year = {}
    for r in county_rows:
        counties_per_year.setdefault(r["year"], set()).add(r["county"])
    for yr, s in counties_per_year.items():
        assert len(s) >= 29, f"year {yr}: only {len(s)} counties"

    cities_per_year = {}
    for r in city_rows:
        cities_per_year.setdefault(r["year"], set()).add(r["city"])
    for yr, s in cities_per_year.items():
        assert 300 <= len(s) <= 310, f"year {yr}: {len(s)} cities (expected 305-306)"

    # BEV trend: statewide counts should rise
    bev_by_year = {}
    for r in county_rows:
        if r["fuel"] == "BEV" and r.get("county") != "OUT OF STATE":
            bev_by_year[r["year"]] = bev_by_year.get(r["year"], 0) + r["count"]
    prev = 0
    for yr in sorted(bev_by_year):
        cur = bev_by_year[yr]
        # Allow small dip only for the snapshot-date / registration-cycle noise
        assert cur > prev * 0.7, f"BEV statewide count FELL year {yr}: {prev}→{cur}"
        prev = cur
    print(f"  BEV statewide trend OK: {dict(sorted(bev_by_year.items()))}")

    # Spot-check known values
    checks = [
        (2018, "county", "BEAVER", "Light Truck", "Gasoline", 2746),
        (2015, "county", "BEAVER", "Heavy Truck", "DIESEL", 261),
    ]
    for yr, level, place, vtype, fuel_raw, expected in checks:
        src = county_rows
        place_field = "county"
        matches = [r for r in src
                   if r["year"] == yr and r[place_field].upper() == place.upper()
                   and r["vehicle_type"].startswith(vtype) and r["fuel_raw"] == fuel_raw]
        actual = sum(r["count"] for r in matches)
        assert actual == expected, f"Spot-check failed {yr} {place} {vtype} {fuel_raw}: got {actual} expected {expected}"
    print("  Spot-checks OK")
    print("Validation passed.\n")


def write_outputs(county_rows, city_rows, years_vocab) -> None:
    def write_csv(path, rows, fields):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fields)
            w.writeheader()
            w.writerows(rows)
        print(f"  Wrote {path}  ({len(rows):,} rows)")

    def write_json(path, rows):
        with open(path, "w") as f:
            json.dump(rows, f, separators=(",", ":"))
        print(f"  Wrote {path}  ({len(rows):,} rows)")

    write_csv(DATA_DIR / "registrations_by_county.csv", county_rows, COUNTY_FIELDS)
    write_json(DATA_DIR / "registrations_by_county.json", county_rows)
    write_csv(DATA_DIR / "registrations_by_city.csv", city_rows, CITY_FIELDS)
    write_json(DATA_DIR / "registrations_by_city.json", city_rows)

    meta = {
        "county_years": sorted({r["year"] for r in county_rows}),
        "city_years": sorted({r["year"] for r in city_rows}),
        "fuel_vocabulary_by_year": years_vocab,
        "fuel_canonical_map": {k: {"fuel": v[0], "ev_class": v[1]} for k, v in FUEL_MAP.items()},
        "caveats": [
            "City coverage is partial. The source states: 'City is determined by address as "
            "reported by vehicle owner. Only select cities are included.' City counts do NOT "
            "sum to the state total.",
            "The 2020 vocabulary break is a real discontinuity. 'Converted' and 'Flexible' "
            "vanish after 2019 and were most likely folded into Gasoline. Annotate this in "
            "any time-series chart rather than smoothing it.",
            "Hybrid/electric/CNG counts are only as accurate as manufacturer-provided data. "
            "Converted/Propane counts are only as accurate as owner-reported data.",
            "'Passenger - Low Speed' vehicle type disappears from 2023 onward.",
            "HEBER city is absent in 2025 (not renamed — no replacement row exists).",
            "These are registration snapshots (active as of ~mid-February each year), "
            "not new-registration flows. Year-over-year deltas are net fleet change.",
        ],
    }
    meta_path = DATA_DIR / "meta.json"
    with open(meta_path, "w") as f:
        json.dump(meta, f, indent=2)
    print(f"  Wrote {meta_path}")


if __name__ == "__main__":
    print("Extracting …")
    county_rows, city_rows, years_vocab = collect()
    validate(county_rows, city_rows)
    print("Writing outputs …")
    write_outputs(county_rows, city_rows, years_vocab)
    print("Done.")
