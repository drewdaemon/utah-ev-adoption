#!/usr/bin/env python3
"""
Rebuild the data constants embedded in index.html.

index.html is the single source of truth for HTML/CSS/JS. This script only
recomputes and injects the data constants (COUNTY_PIVOT, COUNTY_FUEL, etc.),
so running it is safe after any hand-edit to the page logic.

Run after extract.py whenever source data changes:

    python extract.py
    python build_dashboard.py
"""

import csv
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).parent
DATA = ROOT / "data"
GEO  = ROOT / "geo"

# Canonical fuel display labels (folds minor categories into "Other")
DISPLAY_FUEL = {
    "BEV":         "BEV",
    "PHEV":        "PHEV",
    "Hybrid":      "Hybrid",
    "Gasoline":    "Gasoline",
    "Diesel":      "Diesel",
    "Natural Gas": "Natural Gas",
}
OTHER_LABEL = "Other"

# Stack order bottom→top; also used as color domain for consistent legend
FUEL_ORDER = ["Gasoline", "Diesel", "Natural Gas", OTHER_LABEL, "Hybrid", "PHEV", "BEV"]

# Validated reference palette slots (adjacent-pairs / stacked area, PASS in both modes)
FUEL_COLORS_LIGHT = ["#eda100","#e87ba4","#008300","#4a3aa7","#1baf7a","#eb6834","#2a78d6"]
FUEL_COLORS_DARK  = ["#c98500","#d55181","#008300","#9085e9","#199e70","#d95926","#3987e5"]

EV_FUELS = {"BEV", "PHEV"}

COUNTY_START = 2015
CITY_START   = 2018


def latest_year() -> int:
    """Infer the latest year from XLSX files in the project root."""
    years = [int(p.stem[:4]) for p in ROOT.glob("20[12][0-9]registrations.xlsx")]
    if not years:
        raise FileNotFoundError("No *registrations.xlsx files found in project root")
    return max(years)


def year_range(start: int) -> list[int]:
    return list(range(start, latest_year() + 1))


def _norm_city(name: str) -> str:
    s = name.upper().strip()
    s = s.replace(".", "")
    s = re.sub(r"\bSAINT\b", "ST", s)
    s = re.sub(r"\s+(CITY|TOWN)$", "", s)
    return re.sub(r"\s+", " ", s).strip()


def _display_fuel(f: str) -> str:
    return DISPLAY_FUEL.get(f, OTHER_LABEL)


# ---------------------------------------------------------------------------
# Data aggregation
# ---------------------------------------------------------------------------

def county_aggregates():
    """
    Returns:
      share  — [{fips, place, year, ev_count, total, ev_share}, ...]
      fuel   — [{place, year, fuel, count}, ...]
      state  — [{year, ev_share, ev_count, total}, ...]
    """
    raw: dict[tuple, dict[str, int]] = {}
    fips_map: dict[str, str] = {}

    with open(DATA / "registrations_by_county.csv") as f:
        for row in csv.DictReader(f):
            county = row["county"].strip()
            if county == "OUT OF STATE":
                continue
            code = row["county_code"]
            if county and code:
                try:
                    fips_map[county] = str(49000 + int(code) * 2 - 1)
                except ValueError:
                    pass
            key = (county, int(row["year"]))
            df = _display_fuel(row["fuel"])
            raw.setdefault(key, {}).setdefault(df, 0)
            raw[key][df] += int(row["count"])

    share, fuel = [], []
    by_year: dict[int, dict] = {}

    for (place, year), fuels in raw.items():
        ev_count = sum(fuels.get(f, 0) for f in EV_FUELS)
        total    = sum(fuels.values())
        ev_share = round(ev_count / total, 6) if total else 0
        share.append({"fips": fips_map.get(place, ""), "place": place,
                       "year": year, "ev_count": ev_count, "total": total,
                       "ev_share": ev_share})
        for f, cnt in fuels.items():
            if cnt:
                fuel.append({"place": place, "year": year, "fuel": f, "count": cnt})
        yr = by_year.setdefault(year, {"ev": 0, "total": 0})
        yr["ev"] += ev_count; yr["total"] += total

    state = [{"year": yr, "place": "Utah statewide",
               "ev_count": v["ev"], "total": v["total"],
               "ev_share": round(v["ev"] / v["total"], 6) if v["total"] else 0}
             for yr, v in sorted(by_year.items())]
    return share, fuel, state


def city_aggregates(places_topo: dict):
    """
    Returns:
      share        — [{census_name, place, year, ev_count, total, ev_share}, ...]
      fuel         — [{place, year, fuel, count}, ...]
      unmapped     — [city_name, ...]
    """
    feat_key  = list(places_topo["objects"].keys())[0]
    feats     = places_topo["objects"][feat_key]["geometries"]
    census_by_norm = {_norm_city(f["properties"]["NAME"]): f["properties"]["NAME"]
                      for f in feats}

    raw: dict[tuple, dict[str, int]] = {}
    with open(DATA / "registrations_by_city.csv") as f:
        for row in csv.DictReader(f):
            city = row["city"].strip()
            key  = (city, int(row["year"]))
            df   = _display_fuel(row["fuel"])
            raw.setdefault(key, {}).setdefault(df, 0)
            raw[key][df] += int(row["count"])

    city_to_census: dict[str, str] = {}
    unmapped: set[str] = set()
    for (city, _) in raw:
        if city in city_to_census or city in unmapped:
            continue
        cn = census_by_norm.get(_norm_city(city))
        if cn:
            city_to_census[city] = cn
        else:
            unmapped.add(city)

    share, fuel = [], []
    for (place, year), fuels in raw.items():
        if place in unmapped:
            continue
        ev_count = sum(fuels.get(f, 0) for f in EV_FUELS)
        total    = sum(fuels.values())
        ev_share = round(ev_count / total, 6) if total else 0
        share.append({"census_name": city_to_census[place], "place": place,
                       "year": year, "ev_count": ev_count, "total": total,
                       "ev_share": ev_share})
        for f, cnt in fuels.items():
            if cnt:
                fuel.append({"place": place, "year": year, "fuel": f, "count": cnt})

    return share, fuel, sorted(unmapped)


def pivot_share(rows, key_field, years):
    """
    Convert long share rows to one row per place with year-indexed fields.
    This allows a Vega-Lite signal (year_param) to dynamically select the right year
    via `datum['ev_share_' + year_param]` in a calculate transform — avoiding the
    lookup-multiple-matches problem when the FROM data has one row per (place, year).
    """
    by_key: dict[str, dict] = {}
    for r in rows:
        k = r[key_field]
        if k not in by_key:
            entry = {key_field: k, "place": r["place"]}
            # Pre-fill all year slots with 0 (so calculate never returns undefined)
            for yr in years:
                entry[f"ev_share_{yr}"] = 0
                entry[f"ev_count_{yr}"] = 0
                entry[f"total_{yr}"]    = 0
            by_key[k] = entry
        yr = r["year"]
        by_key[k][f"ev_share_{yr}"] = r["ev_share"]
        by_key[k][f"ev_count_{yr}"] = r["ev_count"]
        by_key[k][f"total_{yr}"]    = r["total"]
    return list(by_key.values())




def pivot_fields(years: list[int]) -> list[str]:
    return ["place"] + [f"{m}_{y}" for y in years
                        for m in ["ev_share", "ev_count", "total"]]


# ---------------------------------------------------------------------------
# Data injection — index.html is the template
# ---------------------------------------------------------------------------

def inject_data(html: str, county_years: list[int], city_years: list[int],
                county_pivot, county_fuel, county_pf,
                city_pivot, city_fuel, city_pf,
                state_share) -> str:
    """Replace each const X = ...; line in index.html with freshly computed data."""
    J = lambda v: json.dumps(v, separators=(",", ":"))

    replacements = {
        "COUNTY_PIVOT": J(county_pivot),
        "COUNTY_FUEL":  J(county_fuel),
        "COUNTY_PF":    J(county_pf),
        "CITY_PIVOT":   J(city_pivot),
        "CITY_FUEL":    J(city_fuel),
        "CITY_PF":      J(city_pf),
        "STATE_SHARE":  J(state_share),
        "COUNTY_YEARS": J(county_years),
        "CITY_YEARS":   J(city_years),
    }
    for name, value in replacements.items():
        pattern = rf'(const {name}\s*=\s*)(\[.*?\]|\{{.*?\}})(?=;)'
        html, n = re.subn(pattern, rf'\g<1>{value}', html, count=1, flags=re.DOTALL)
        if n == 0:
            raise ValueError(f"Could not find 'const {name} = ...;' in index.html")

    # Subtitle year range  e.g. "2015–2025" → "2015–2026"
    html = re.sub(r'Annual snapshots \d{4}–\d{4}',
                  f'Annual snapshots {county_years[0]}–{county_years[-1]}', html)

    # Default selected year
    last = county_years[-1]
    html = re.sub(r'let curYear\s*=\s*\d+;',  f'let curYear  = {last};', html)
    html = re.sub(r'curYear\s*=\s*\d+;',       f'curYear  = {last};',    html)

    return html


# ---------------------------------------------------------------------------
# Geo script (documentation only — assets are already committed)
# ---------------------------------------------------------------------------

GEO_SCRIPT = """\
#!/usr/bin/env bash
# One-time geometry preparation. Outputs committed to geo/.
# Requires: node, npx (mapshaper), curl
set -e

# County topology: filter us-atlas@3 to Utah, simplify
curl -sS https://cdn.jsdelivr.net/npm/us-atlas@3/counties-10m.json -o /tmp/us_counties.json
node -e "
  const t=require('/tmp/us_counties.json'),fs=require('fs');
  const ut={type:'Topology',
    objects:{counties:{type:'GeometryCollection',
      geometries:t.objects.counties.geometries
        .filter(d=>d.id.startsWith('49'))
        .map(d=>({...d,properties:{name:d.properties.name}}))}},
    arcs:t.arcs,transform:t.transform};
  fs.writeFileSync('/tmp/ut_raw.json',JSON.stringify(ut));
"
npx --yes mapshaper /tmp/ut_raw.json -simplify 12% -o format=topojson geo/ut_counties.topo.json

# City (place) topology: Census TIGER cartographic boundaries
curl -sS https://www2.census.gov/geo/tiger/GENZ2023/shp/cb_2023_49_place_500k.zip -o /tmp/places.zip
cd /tmp && unzip -oq places.zip && cd -
npx --yes mapshaper /tmp/cb_2023_49_place_500k.zip \\
  -each 'this.properties={NAME:this.properties.NAME}' \\
  -simplify 5% \\
  -o format=topojson geo/ut_places.topo.json
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    county_years = year_range(COUNTY_START)
    city_years   = year_range(CITY_START)
    print(f"Years: county {county_years[0]}–{county_years[-1]}, "
          f"city {city_years[0]}–{city_years[-1]}")

    places_topo = json.loads((GEO / "ut_places.topo.json").read_text())

    print("Aggregating county data …")
    county_share, county_fuel, state_share = county_aggregates()

    print("Aggregating city data …")
    city_share, city_fuel, unmapped = city_aggregates(places_topo)

    print(f"  County: {len(county_share)} share rows, {len(county_fuel)} fuel rows")
    print(f"  City:   {len(city_share)} share rows, {len(city_fuel)} fuel rows")
    print(f"  Statewide years: {len(state_share)}  Unmapped cities: {len(unmapped)}")

    county_pivot = pivot_share(county_share, "fips",        county_years)
    city_pivot   = pivot_share(city_share,   "census_name", city_years)
    county_pf    = pivot_fields(county_years)
    city_pf      = pivot_fields(city_years)

    print("Injecting data into index.html …")
    index = ROOT / "index.html"
    html  = index.read_text(encoding="utf-8")
    html  = inject_data(html, county_years, city_years,
                        county_pivot, county_fuel, county_pf,
                        city_pivot,   city_fuel,   city_pf,
                        state_share)
    index.write_text(html, encoding="utf-8")
    kb = index.stat().st_size // 1024
    print(f"Wrote {index}  ({kb} KB)")

    geo_sh = GEO / "prepare_geo.sh"
    geo_sh.write_text(GEO_SCRIPT)
    geo_sh.chmod(0o755)
    print(f"Wrote {geo_sh}")


if __name__ == "__main__":
    main()
