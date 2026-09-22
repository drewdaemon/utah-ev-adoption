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
npx --yes mapshaper /tmp/cb_2023_49_place_500k.zip \
  -each 'this.properties={NAME:this.properties.NAME}' \
  -simplify 5% \
  -o format=topojson geo/ut_places.topo.json
