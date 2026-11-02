# Astana left-bank map

This map defines the project's full left-bank coverage as the union of the OpenStreetMap administrative relations for Nura and Esil districts. The administrative union is reproducible and avoids an arbitrary hand-drawn rectangle.

## Build

```bash
python simulation/maps/astana_left_bank/import_map.py
python simulation/maps/astana_left_bank/validate_network.py
sumo-gui -c simulation/maps/astana_left_bank/astana_left_bank.sumocfg
```

The first build is road-focused. It includes roads, lane metadata supplied by OSM, bridges, tunnels, grade information and generated traffic-light controllers. Full-area buildings are intentionally deferred because they do not affect routing and would make the initial import much heavier.

Validation generates `infrastructure_inventory.json`, a deduplicated coordinate register of OSM traffic-signal nodes and grade-separated ways. This register is the queue for visual inspection in 2GIS and later manual SUMO overrides.

Reviewed corrections are stored in `osm_overrides.json`. `import_map.py` applies them to generated copies of the OSM snapshots before invoking `netconvert`; the downloaded raw files are never edited. Every override records its location, evidence date and review status.

## Accuracy policy

The entire area receives automated structural validation. Primary roads, bridges and interchanges receive visual audit. Emergency corridors receive detailed lane-level correction. Public maps may be used as visual references, but proprietary map tiles and 3D assets are not copied into the repository.
