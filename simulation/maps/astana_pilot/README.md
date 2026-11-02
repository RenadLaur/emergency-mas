# Astana pilot map

The pilot covers the central Ishim corridor in Astana. It is deliberately smaller than the future city-wide map so that road topology, traffic lights and grade separation can be audited before the import process is scaled up.

## Build

Activate the project environment and run:

```bash
python simulation/maps/astana_pilot/import_map.py
python simulation/maps/astana_pilot/validate_network.py
sumo-gui -c simulation/maps/astana_pilot/astana_pilot.sumocfg
```

The raw OpenStreetMap download and generated SUMO files are ignored by Git because they are large and derived. `import_manifest.json` records the source area and timestamp. The generated network contains road infrastructure. Buildings, water and parks are generated as a separate polygon layer for SUMO-GUI when the OSM data supplies those geometries.

## Validation boundary

The structural report confirms that the import created a non-empty network and inventories traffic signals, bridges, tunnels and non-zero OSM layers. `traffic_light_inventory.json` contains every generated controller, its phase count, cycle duration and number of controlled links. It is not a claim that every ordinary traffic light has a correct phase plan or every bridge connection is correct. Manual review in SUMO-GUI or NetEdit remains required for all emergency corridors and for the regular traffic-light inventory.

The `.sumocfg` file loads both the routable network and the generated polygon layer, so buildings, water and parks can be inspected together with lanes and junctions. Road markings and exact physical signal-head placement are not reliably available from OSM and must be added only for the selected detailed corridors.
