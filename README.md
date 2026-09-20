# Utah EV Registration Study

Curated dataset of Utah on-highway vehicle registrations by fuel type, 2015–2025, extracted
from the Utah State Tax Commission annual workbooks.

## Outputs (`data/`)

| file | granularity | years | rows |
|---|---|---|---|
| `registrations_by_county.csv / .json` | county × year × vehicle type × fuel | 2015–2025 | — |
| `registrations_by_city.csv / .json` | city × year × vehicle type × fuel | **2018–2025 only** | — |
| `meta.json` | provenance, fuel vocabulary by year, caveats | — | — |

**City coverage starts in 2018** because city-level fuel tables do not exist in the 2015–2017 workbooks.

## Schema

County files:

| column | type | notes |
|---|---|---|
| `year` | int | snapshot year |
| `county` | string | normalized name (prefix `NN - ` stripped) |
| `county_code` | int \| null | 1–29, 99 = out of state |
| `vehicle_type` | string | Passenger - Standard, Light Truck, Heavy Truck, Motorcycle - Standard, Passenger - Low Speed |
| `fuel_raw` | string | verbatim label from source, stripped |
| `fuel` | string | canonical bucket (BEV, PHEV, Hybrid, Gasoline, Diesel, …) |
| `ev_class` | string | BEV, PHEV, Hybrid, or Non-electrified |
| `count` | int | registration count; zero rows omitted |

City files: same but `city` replaces `county`/`county_code`.

## Regenerating

```bash
pip install -r requirements.txt
python extract.py
```

The script validates its own output by checking row sums against the `TOTAL` column in each
source sheet, then asserts cross-year sanity (county/city set sizes, rising BEV trend, spot
checks of known values). It will fail loudly if anything looks wrong rather than emitting
quietly-wrong data.

## Known caveats

1. **City coverage is partial.** The source states: *"City is determined by address as reported
   by the vehicle owner. Only select cities are included."* City counts do **not** sum to the
   state total — using city data to compute share-of-Utah would be wrong.

2. **2020 vocabulary break.** The fuel-type labels were completely restructured between 2019 and
   2020 — `Converted`, `Flexible`, and `Natural Gas` vanished and were most likely folded into
   `Gasoline`. This creates a visible step in those canonical series. Annotate it in any time-
   series chart rather than smoothing it.

3. **Accuracy caveats from the source:** Hybrid, Electric, and CNG counts are only as accurate
   as manufacturer-provided data; Converted and Propane counts only as accurate as owner-
   reported data.

4. **`Passenger - Low Speed`** vehicle type disappears from the source starting in 2023.

5. **`HEBER`** city is absent in the 2025 workbook (not renamed — no `HEBER CITY` row exists).

6. **These are snapshots, not flows.** Each file represents registrations active as of
   approximately mid-February of that year. Year-over-year deltas are net fleet change, not new
   registrations.

## Source

Utah State Tax Commission, Economics and Statistical Unit — *Utah Current Registrations*,
annual workbooks 2015–2025.
