#!/usr/bin/env python3
"""
Generate index.html — a fully self-contained Vega-Lite dashboard for Utah EV
registrations. Run any time the source data or geo assets change:

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

COUNTY_YEARS = list(range(2015, 2026))
CITY_YEARS   = list(range(2018, 2026))


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


# ---------------------------------------------------------------------------
# HTML template
# ---------------------------------------------------------------------------

def build_html(
    county_share, county_fuel, state_share,
    city_share,   city_fuel,   unmapped_cities,
    county_topo,  places_topo,
) -> str:
    J = lambda v: json.dumps(v, separators=(",", ":"))

    county_pivot = pivot_share(county_share, "fips",        COUNTY_YEARS)
    city_pivot   = pivot_share(city_share,   "census_name", CITY_YEARS)

    # Fields to pull through the lookup (all year-specific metrics)
    def pivot_fields(years):
        return ["place"] + [f"{m}_{y}" for y in years
                            for m in ["ev_share","ev_count","total"]]

    county_pf = pivot_fields(COUNTY_YEARS)
    city_pf   = pivot_fields(CITY_YEARS)

    places_feat_key = list(places_topo["objects"].keys())[0]

    unmapped_html = (
        f'<details><summary>{len(unmapped_cities)} cities without map geometry '
        f'(0.48% of city registrations)</summary>'
        f'<p class="muted">{", ".join(sorted(unmapped_cities))}</p></details>'
    )

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Utah EV Registrations</title>
<script src="https://cdn.jsdelivr.net/npm/vega@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-lite@5"></script>
<script src="https://cdn.jsdelivr.net/npm/vega-embed@6"></script>
<style>
  *,*::before,*::after{{box-sizing:border-box;margin:0;padding:0}}
  :root{{
    --bg:#f9f9f7;--surface:#fcfcfb;--border:rgba(11,11,11,0.10);
    --ink:#0b0b0b;--ink2:#52514e;--muted:#898781;--grid:#e1e0d9;
    --blue:#2a78d6;--accent:#f59e42;
    font-family:system-ui,-apple-system,"Segoe UI",sans-serif;
    font-size:14px;color:var(--ink);background:var(--bg);
  }}
  @media(prefers-color-scheme:dark){{
    :root{{
      --bg:#0d0d0d;--surface:#1a1a19;--border:rgba(255,255,255,0.10);
      --ink:#fff;--ink2:#c3c2b7;--muted:#898781;--grid:#2c2c2a;
      --blue:#3987e5;--accent:#f59e42;
    }}
  }}
  body{{padding:16px 20px;min-height:100vh}}
  h1{{font-size:20px;font-weight:600;line-height:1.3}}
  h1 small{{display:block;font-size:12px;font-weight:400;color:var(--muted);margin-top:2px}}
  .controls{{display:flex;align-items:center;gap:20px;margin:12px 0;flex-wrap:wrap}}
  .ctrl-label{{font-size:12px;color:var(--ink2);margin-right:2px}}
  .pill-group{{display:flex;gap:4px}}
  .pill{{
    appearance:none;cursor:pointer;
    border:1px solid var(--border);background:var(--surface);color:var(--ink2);
    padding:4px 12px;border-radius:99px;font-size:12px;line-height:1.4;
    transition:background .12s,color .12s,border-color .12s;
  }}
  .pill.on{{background:var(--blue);border-color:var(--blue);color:#fff;font-weight:500}}
  .layout{{display:grid;grid-template-columns:1fr 1fr;gap:16px;align-items:start}}
  @media(max-width:760px){{.layout{{grid-template-columns:1fr}}}}
  .card{{background:var(--surface);border:1px solid var(--border);border-radius:8px;padding:14px}}
  .card-title{{font-size:11px;font-weight:600;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}}
  #detail-panel{{display:flex;flex-direction:column;gap:14px}}
  #sel-name{{font-size:17px;font-weight:600;color:var(--ink);min-height:24px;margin-bottom:8px}}
  .note{{font-size:11px;color:var(--muted);margin-top:8px;line-height:1.5}}
  details{{margin-top:8px}}
  summary{{cursor:pointer;font-size:12px;color:var(--muted)}}
  .muted{{font-size:11px;color:var(--muted);margin-top:4px;line-height:1.6}}
  /* Vega-Lite bind slider styling */
  .vega-bind{{margin-top:10px;display:flex;align-items:center;gap:8px}}
  .vega-bind-name{{font-size:12px;color:var(--ink2);white-space:nowrap}}
  .vega-bind input[type=range]{{flex:1;cursor:pointer;accent-color:var(--blue)}}
  .vega-embed{{display:block!important;width:100%}}
  .vega-embed .vega-actions{{display:none!important}}
  #place-sel{{
    appearance:none;background:var(--surface);border:1px solid var(--border);
    border-radius:6px;color:var(--ink);font-size:12px;
    padding:4px 28px 4px 10px;cursor:pointer;max-width:200px;
    background-image:url("data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='12' height='12' viewBox='0 0 12 12'%3E%3Cpath fill='%23898781' d='M2 4l4 4 4-4'/%3E%3C/svg%3E");
    background-repeat:no-repeat;background-position:right 8px center;
  }}
</style>
</head>
<body>
<h1>Utah EV Registrations
  <small>Utah State Tax Commission · Annual snapshots 2015–2025 (as of ~mid-Feb each year)</small>
</h1>

<div class="controls">
  <div style="display:flex;align-items:center;gap:6px">
    <span class="ctrl-label">View by</span>
    <div class="pill-group">
      <button class="pill on" id="btn-county" onclick="setLevel('county')">County</button>
      <button class="pill"    id="btn-city"   onclick="setLevel('city')">City</button>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:6px">
    <span class="ctrl-label">Color</span>
    <div class="pill-group">
      <button class="pill on" id="btn-share" onclick="setMetric('share')">EV share %</button>
      <button class="pill"    id="btn-count" onclick="setMetric('count')">EV count</button>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:6px">
    <span class="ctrl-label">Place</span>
    <select id="place-sel" onchange="onPlaceSelect(this.value)"></select>
  </div>
</div>

<div class="layout">
  <div class="card" id="map-card">
    <div class="card-title" id="map-title">EV adoption by county</div>
    <div id="map-embed"></div>
    <p class="note" id="city-note" style="display:none">
      City coverage is partial — only select cities are included by the Tax Commission.
      City counts do not sum to the state total.
    </p>
  </div>
  <div id="detail-panel">
    <div class="card">
      <div class="card-title">Plug-in EV share over time (BEV + PHEV · excludes conventional hybrid)</div>
      <div id="sel-name">Select a place on the map</div>
      <div id="share-embed"></div>
    </div>
    <div class="card">
      <div class="card-title">Fuel composition over time (normalized)</div>
      <div id="fuel-embed"></div>
      <p class="note">⚠ Fuel categories were restructured in 2020; "Converted" and
        "Flexible Fuel" were discontinued and likely folded into Gasoline.</p>
    </div>
  </div>
</div>
{unmapped_html}

<script>
// ── Inlined data ──────────────────────────────────────────────────────────
const COUNTY_PIVOT  = {J(county_pivot)};
const COUNTY_FUEL   = {J(county_fuel)};
const COUNTY_PF     = {J(county_pf)};
const CITY_PIVOT    = {J(city_pivot)};
const CITY_FUEL     = {J(city_fuel)};
const CITY_PF       = {J(city_pf)};
const STATE_SHARE   = {J(state_share)};
const COUNTY_TOPO   = {J(county_topo)};
const PLACES_TOPO   = {J(places_topo)};
const PLACES_FEAT   = {J(places_feat_key)};
const COUNTY_YEARS  = {J(COUNTY_YEARS)};
const CITY_YEARS    = {J(CITY_YEARS)};
const FUEL_ORDER    = {J(FUEL_ORDER)};
const FUEL_COLORS   = {J(FUEL_COLORS_LIGHT)};
// Map: fuel domain → color (same order as FUEL_ORDER)
const FUEL_SCALE    = {{domain: FUEL_ORDER, range: FUEL_COLORS}};

// ── Dashboard state ───────────────────────────────────────────────────────
let level    = 'county';
let metric   = 'share';
let selPlace = 'SALT LAKE';
let curYear  = 2025;
let mapView  = null;

// ── Vega-Lite shared theme ────────────────────────────────────────────────
const VL_CONFIG = {{
  background: 'transparent',
  view: {{stroke: null}},
  axis: {{
    domainColor:'#c3c2b7', tickColor:'#c3c2b7',
    gridColor:'#e1e0d9',   gridWidth:1,
    labelColor:'#898781',  titleColor:'#898781',
    labelFontSize:11,      titleFontSize:11,
  }},
  legend: {{
    labelColor:'#52514e', titleColor:'#52514e',
    labelFontSize:11,     titleFontSize:11,
    symbolSize:80,        rowPadding:3,
  }},
  mark: {{tooltip: true}},
}};
const EMBED_OPT = {{actions: false, renderer: 'svg',
  patch: s => ({{...s, config: {{...VL_CONFIG, ...(s.config||{{}})}}}})}};

// ── Map spec ──────────────────────────────────────────────────────────────
function mapSpec() {{
  const isCo   = level === 'county';
  const topo   = isCo ? COUNTY_TOPO : PLACES_TOPO;
  const feat   = isCo ? 'counties' : PLACES_FEAT;
  const pivot  = isCo ? COUNTY_PIVOT : CITY_PIVOT;
  const pf     = isCo ? COUNTY_PF : CITY_PF;
  const lkpKey = isCo ? 'fips' : 'census_name';
  const topoF  = isCo ? 'id' : 'properties.NAME';
  const years  = isCo ? COUNTY_YEARS : CITY_YEARS;
  const minYr  = years[0], maxYr = years[years.length-1];

  const colorEnc = metric === 'share' ? {{
    field: 'ev_share', type: 'quantitative',
    scale: {{scheme: 'blues', domain: [0, isCo ? 0.12 : 0.22]}},
    legend: {{title: 'EV share', format: '.1%'}},
  }} : {{
    field: 'ev_count', type: 'quantitative',
    scale: {{scheme: 'blues', type: 'sqrt', domainMin: 0}},
    legend: {{title: 'EV registrations', format: ',.0f'}},
  }};

  // Build the calculate transforms referencing year_param signal
  const calcXforms = [
    {{calculate: "datum['ev_share_' + year_param]", as: 'ev_share'}},
    {{calculate: "datum['ev_count_' + year_param]", as: 'ev_count'}},
    {{calculate: "datum['total_'    + year_param]", as: 'total'}},
  ];

  const choroplethLayer = {{
    data: {{values: topo, format: {{type:'topojson', feature: feat}}}},
    transform: [
      {{lookup: topoF,
        from: {{data:{{values: pivot}}, key: lkpKey, fields: pf}},
        as: pf}},
      ...calcXforms,
    ],
    params: [{{
      name: 'place_sel',
      select: {{type: 'point', fields: ['place'], on: 'click'}},
      value: [{{place: selPlace}}],
    }}],
    mark: {{
      type: 'geoshape',
      stroke: '#fcfcfb', strokeWidth: 0.8,
      cursor: 'pointer',
    }},
    encoding: {{
      color: {{
        condition: {{param: 'place_sel', empty: false, value: '#f59e42'}},
        ...colorEnc,
      }},
      tooltip: [
        {{field: 'place', title: isCo ? 'County' : 'City'}},
        {{field: 'ev_share', title: 'EV share', format: '.1%'}},
        {{field: 'ev_count', title: 'EV registrations', format: ','}},
        {{field: 'total',    title: 'Total registrations', format: ','}},
      ],
    }},
  }};

  // Selected place outline layer (orange border on clicked shape)
  const outlineLayer = {{
    data: {{values: topo, format: {{type:'topojson', feature: feat}}}},
    transform: [
      {{lookup: topoF,
        from: {{data:{{values: pivot}}, key: lkpKey, fields: ['place']}},
        as: ['place']}},
      {{filter: `datum.place === '${{selPlace}}'`}},
    ],
    mark: {{type:'geoshape', fill: null, stroke:'#f59e42', strokeWidth:2}},
  }};

  const layers = [];
  // County outline base layer when in city mode (geographic context)
  if (!isCo) {{
    layers.push({{
      data: {{values: COUNTY_TOPO, format: {{type:'topojson', feature:'counties'}}}},
      mark: {{type:'geoshape', fill:'transparent', stroke:'#c3c2b7', strokeWidth:0.5}},
    }});
  }}
  layers.push(choroplethLayer, outlineLayer);

  return {{
    $schema: 'https://vega.github.io/schema/vega-lite/v5.json',
    width: 'container', height: 420,
    projection: {{type: 'mercator'}},
    params: [{{
      name: 'year_param',
      value: curYear,
      bind: {{input:'range', min:minYr, max:maxYr, step:1, name:'Year:  '}},
    }}],
    layer: layers,
  }};
}}

// ── Detail specs (rebuilt on selection change) ────────────────────────────
function shareSpec() {{
  const share = level === 'county' ? COUNTY_PIVOT : CITY_PIVOT;
  const placeRow = share.find(d => d.place === selPlace);
  if (!placeRow) return null;
  const years = level === 'county' ? COUNTY_YEARS : CITY_YEARS;

  // Build long data: selected place + statewide
  const placeData = years.map(y => ({{
    year: y, ev_share: placeRow[`ev_share_${{y}}`] || 0, series: selPlace,
  }}));
  const stateData = STATE_SHARE
    .filter(d => years.includes(d.year))
    .map(d => ({{year: d.year, ev_share: d.ev_share, series:'Utah statewide'}}));

  return {{
    $schema: 'https://vega.github.io/schema/vega-lite/v5.json',
    width: 'container', height: 170,
    data: {{values: [...placeData, ...stateData]}},
    mark: {{type:'line', point:{{filled:true, size:36}}, strokeWidth:2}},
    encoding: {{
      x: {{field:'year', type:'ordinal', axis:{{title:null, labelAngle:0}}}},
      y: {{field:'ev_share', type:'quantitative',
           axis:{{title:'EV share (BEV+PHEV)', format:'.1%'}},
           scale:{{domainMin:0}}}},
      color: {{
        field:'series', type:'nominal',
        scale:{{domain:[selPlace,'Utah statewide'], range:['#2a78d6','#eb6834']}},
        legend:{{title:null}},
      }},
      strokeDash: {{
        field:'series', type:'nominal',
        scale:{{domain:[selPlace,'Utah statewide'], range:[[1,0],[4,2]]}},
        legend:{{title:null}},
      }},
      tooltip: [
        {{field:'series', title:'Series'}},
        {{field:'year',   title:'Year'}},
        {{field:'ev_share',title:'EV share', format:'.2%'}},
      ],
    }},
  }};
}}

function fuelSpec() {{
  const fuelData = level === 'county' ? COUNTY_FUEL : CITY_FUEL;
  const rows = fuelData.filter(d => d.place === selPlace);
  if (!rows.length) return null;

  return {{
    $schema: 'https://vega.github.io/schema/vega-lite/v5.json',
    width: 'container', height: 190,
    data: {{values: rows}},
    mark: {{type:'area', strokeWidth:1.5, stroke:'#fcfcfb'}},
    encoding: {{
      x: {{field:'year', type:'ordinal', axis:{{title:null, labelAngle:0}}}},
      y: {{field:'count', type:'quantitative', stack:'normalize',
           axis:{{title:'Share of registrations', format:'.0%'}}}},
      color: {{
        field:'fuel', type:'nominal',
        sort: FUEL_ORDER,
        scale: FUEL_SCALE,
        legend:{{title:'Fuel type', orient:'right'}},
      }},
      order: {{field:'fuel', type:'nominal', sort: FUEL_ORDER}},
      tooltip: [
        {{field:'fuel',  title:'Fuel'}},
        {{field:'year',  title:'Year'}},
        {{field:'count', title:'Registrations', format:','}},
      ],
    }},
  }};
}}

// ── Place select ──────────────────────────────────────────────────────────
function populatePlaceSelect() {{
  const pivot = level === 'county' ? COUNTY_PIVOT : CITY_PIVOT;
  const places = [...new Set(pivot.map(d => d.place))].sort();
  const sel = document.getElementById('place-sel');
  sel.innerHTML = places
    .map(p => `<option value="${{p}}"${{p === selPlace ? ' selected' : ''}}>${{p}}</option>`)
    .join('');
}}

function onPlaceSelect(val) {{
  selPlace = val;
  document.getElementById('sel-name').textContent = selPlace;
  renderDetails();
  renderMap();
}}

// ── Render helpers ────────────────────────────────────────────────────────
async function renderMap() {{
  if (mapView) {{ try {{ mapView.finalize(); }} catch(_) {{}} }}
  const {{view}} = await vegaEmbed('#map-embed', mapSpec(), EMBED_OPT);
  mapView = view;

  // Track slider year changes (smooth within Vega — no re-embed)
  view.addSignalListener('year_param', (_n, y) => {{
    curYear = Math.round(y);
  }});

  // Click → update selection, rebuild detail charts
  view.addEventListener('click', (_e, item) => {{
    if (item && item.datum && item.datum.place) {{
      selPlace = item.datum.place;
      document.getElementById('sel-name').textContent = selPlace;
      document.getElementById('place-sel').value = selPlace;
      renderDetails();
      // Rebuild map to update the orange outline on the newly selected shape
      renderMap();
    }}
  }});
}}

async function renderDetails() {{
  const ss = shareSpec(), fs = fuelSpec();
  if (ss) await vegaEmbed('#share-embed', ss, EMBED_OPT);
  if (fs) await vegaEmbed('#fuel-embed',  fs, EMBED_OPT);
}}

async function boot() {{
  populatePlaceSelect();
  document.getElementById('sel-name').textContent = selPlace;
  await renderMap();
  await renderDetails();
}}

// ── Public controls ───────────────────────────────────────────────────────
function setActive(ids, on) {{
  ids.forEach(id => document.getElementById(id).classList.toggle('on', id === on));
}}

function setLevel(l) {{
  level = l;
  selPlace = l === 'county' ? 'SALT LAKE' : 'SALT LAKE CITY';
  curYear  = l === 'county' ? 2025 : 2025;
  setActive(['btn-county','btn-city'], `btn-${{l}}`);
  document.getElementById('map-title').textContent =
    `EV adoption by ${{l}}`;
  document.getElementById('city-note').style.display = l === 'city' ? '' : 'none';
  populatePlaceSelect();
  document.getElementById('sel-name').textContent = selPlace;
  renderMap().then(() => renderDetails());
}}

function setMetric(m) {{
  metric = m;
  setActive(['btn-share','btn-count'], `btn-${{m}}`);
  renderMap();  // metric only affects the map color — detail charts unchanged
}}

// ── Boot ──────────────────────────────────────────────────────────────────
boot();
</script>
</body>
</html>"""


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
node -e "
  const fs=require('fs');
  // Strip all properties except NAME
  const raw=fs.readFileSync('/tmp/cb_2023_49_place_500k.shp');
  console.log('Use mapshaper to convert + strip');
"
# Simpler via mapshaper directly:
npx --yes mapshaper /tmp/cb_2023_49_place_500k.zip \\
  -each 'this.properties={NAME:this.properties.NAME}' \\
  -simplify 5% \\
  -o format=topojson geo/ut_places.topo.json
"""


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    print("Loading geo …")
    county_topo = json.loads((GEO / "ut_counties.topo.json").read_text())
    places_topo = json.loads((GEO / "ut_places.topo.json").read_text())

    print("Aggregating county data …")
    county_share, county_fuel, state_share = county_aggregates()

    print("Aggregating city data …")
    city_share, city_fuel, unmapped = city_aggregates(places_topo)

    print(f"  County share rows: {len(county_share)}  fuel rows: {len(county_fuel)}")
    print(f"  City   share rows: {len(city_share)}  fuel rows: {len(city_fuel)}")
    print(f"  Statewide years: {len(state_share)}  Unmapped cities: {len(unmapped)}")

    print("Building HTML …")
    html = build_html(
        county_share, county_fuel, state_share,
        city_share,   city_fuel,   unmapped,
        county_topo,  places_topo,
    )

    out = ROOT / "index.html"
    out.write_text(html, encoding="utf-8")
    kb = out.stat().st_size // 1024
    print(f"Wrote {out}  ({kb} KB)")

    geo_sh = GEO / "prepare_geo.sh"
    geo_sh.write_text(GEO_SCRIPT)
    geo_sh.chmod(0o755)
    print(f"Wrote {geo_sh}")


if __name__ == "__main__":
    main()
